"""Gmail API URL 邮箱池客户端

通过轮询取码URL获取验证码，支持响应码处理：
- code=601: 等待验证码（继续轮询）
- code=602: 邮箱错误/服务商问题（抛出异常，调用方标记为failed）
- code=0 + data.code: 成功获取验证码

格式：email----code_url
示例：user@gmail.com----https://gapi.mailsapi.com/api/get-code?uid=abc123

Multi-batch registration support:
- create_registration_batch(): tạo batch từ pool
- get_email_from_batch(): lấy email từ batch theo job_id
"""

import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_api_url_batch_store import (
    GmailApiUrlBatchConflict,
    GmailApiUrlBatchError,
    GmailApiUrlBatchStore,
)
from core.time_utils import local_now

logger = logging.getLogger(__name__)
_BEFORE_CODE_UNSET = object()
_PROVIDER_602_RE = re.compile(
    r"(?:\bcode|\bstatus|\bhttp(?:\s+status)?|\berror)\s*[:=]?\s*602\b",
    re.IGNORECASE,
)

# Batch store singleton
_BATCH_STORE_PATH = APP_STATE_DB_PATH
_batch_store_instance: GmailApiUrlBatchStore | None = None
_BATCH_BUILD_LOCK = threading.Lock()


def _batch_store() -> GmailApiUrlBatchStore:
    """Lấy batch store singleton."""
    global _batch_store_instance
    if _batch_store_instance is None:
        _batch_store_instance = GmailApiUrlBatchStore(_BATCH_STORE_PATH)
    return _batch_store_instance


@dataclass
class GmailApiUrlAccount:
    """Gmail API URL 账户信息"""
    email: str
    code_url: str


class GmailApiUrlError(Exception):
    """Gmail API URL 客户端异常"""


def _is_provider_code_602(value: object) -> bool:
    """Recognize terminal provider responses without depending on a caller."""
    return bool(_PROVIDER_602_RE.search(str(value or "")))


def _fetch_code_once(code_url: str) -> tuple[int, str | None]:
    """单次调用取码接口，返回 (api_code, otp_or_None)。
    HTTP 错误时返回 (-1, None)；JSON 格式异常时返回 (-2, None)。
    code=602 时直接抛出 GmailApiUrlError（不重试）。
    """
    try:
        resp = requests.get(code_url, timeout=10, allow_redirects=False)
        if resp.status_code == 602:
            raise GmailApiUrlError(
                "Provider error code=602: HTTP status 602. Contact provider for refund."
            )
        if resp.status_code >= 400:
            return -1, None
        payload = resp.json()
        raw_code = payload.get("code")
        try:
            api_code = int(raw_code) if raw_code is not None else -2
        except (TypeError, ValueError):
            api_code = -2
        if api_code == 602:
            msg = payload.get("message", "Provider error")
            raise GmailApiUrlError(
                f"Provider error code=602: {msg}. Contact provider for refund."
            )
        if api_code == 0:
            data = payload.get("data") or {}
            otp = str(data["code"]).strip() if isinstance(data, dict) and "code" in data else None
            if otp is None:
                raise GmailApiUrlError(f"code=0 but data.code missing: {payload}")
            if not re.fullmatch(r"\d{6}", otp):
                logger.warning("[GmailApiUrl] provider returned malformed OTP; ignoring response")
                return -2, None
            return 0, otp
        return api_code, None
    except GmailApiUrlError:
        raise
    except requests.RequestException:
        return -1, None
    except (ValueError, KeyError):
        return -2, None


def _runtime_store(sqlite_path=None) -> GmailApiUrlBatchStore:
    """Resolve the canonical store, preserving custom fixture/runtime paths."""
    if sqlite_path is not None:
        return GmailApiUrlBatchStore(sqlite_path)
    return _batch_store()


def _runtime_store_path(sqlite_path=None):
    store = _runtime_store(sqlite_path)
    path = getattr(store, "path", None)
    return path if path is not None else APP_STATE_DB_PATH


def _quarantine_provider_code_url(
    account: GmailApiUrlAccount,
    error: Exception,
    *,
    sqlite_path=None,
) -> None:
    """Persist a terminal provider failure for every owner of one code URL.

    The low-level client is also used by email-change and batch-store adapters,
    so 602 quarantine cannot depend on the higher-level email provider.  An
    unknown URL is left alone after the raw-pool lookup; this keeps isolated
    unit tests and untracked provider URLs free of unrelated DB writes.
    """
    if not _is_provider_code_602(error):
        return
    code_url = str(getattr(account, "code_url", "") or "").strip()
    if not code_url:
        return

    from core import db

    runtime_path = _runtime_store_path(sqlite_path)
    try:
        raw_failed = db.fail_gmail_api_url_sources_for_code_url(
            code_url,
            note=str(error),
            sqlite_path=runtime_path,
        )
    except Exception:
        logger.exception(
            "[GmailApiUrl] Failed to mark raw siblings after provider 602: %s",
            code_url,
        )
        raw_failed = 0

    store = _runtime_store(sqlite_path)
    known_canonical = bool(raw_failed)
    if not known_canonical:
        try:
            known_canonical = bool(store.list_batch_ids_for_code_urls({code_url}))
            if not known_canonical:
                connection = store._connect()
                try:
                    q8_row = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'qan8_sources'"
                    ).fetchone()
                    if q8_row is not None:
                        known_canonical = connection.execute(
                            "SELECT 1 FROM qan8_sources WHERE code_url = ? LIMIT 1",
                            (code_url,),
                        ).fetchone() is not None
                finally:
                    connection.close()
        except Exception:
            logger.exception(
                "[GmailApiUrl] Failed to inspect canonical owners after provider 602: %s",
                code_url,
            )
    if not known_canonical:
        return
    try:
        store.quarantine_code_url(code_url, reason=str(error))
    except Exception:
        logger.exception(
            "[GmailApiUrl] Failed to quarantine canonical owners after provider 602: %s",
            code_url,
        )


def _ensure_account_pollable(
    account: GmailApiUrlAccount,
    *,
    sqlite_path=None,
) -> None:
    """Reject disabled roots and quarantined URLs before any provider request."""
    from core import db

    runtime_path = _runtime_store_path(sqlite_path)
    if db.is_gmail_api_url_code_url_failed(
        account.code_url,
        sqlite_path=runtime_path,
    ):
        raise GmailApiUrlError(
            "Provider error code=602: Gmail API URL source is quarantined"
        )
    if db.is_gmail_api_url_account_blocked(
        account.email,
        sqlite_path=runtime_path,
    ):
        raise GmailApiUrlError(
            "Gmail API URL source is disabled or terminally retired"
        )


def snapshot_verification_code(
    account: GmailApiUrlAccount,
    *,
    sqlite_path=None,
) -> str | None:
    """Return the currently visible code without logging, waiting, or persisting."""
    _ensure_account_pollable(account, sqlite_path=sqlite_path)
    try:
        api_code, otp = _fetch_code_once(account.code_url)
    except GmailApiUrlError as exc:
        _quarantine_provider_code_url(account, exc, sqlite_path=sqlite_path)
        raise
    return otp if api_code == 0 and otp else None


def _get_latest_otp(account: GmailApiUrlAccount) -> str | None:
    """Read the accepted OTP persisted for this shared code URL."""
    from core import db

    return db.get_gmail_api_url_last_otp(account.code_url)


def _record_latest_otp(account: GmailApiUrlAccount, otp: str) -> None:
    """Persist a validated OTP without making cache I/O fail the caller."""
    try:
        from core import db

        persisted = db.record_gmail_api_url_otp(account.code_url, otp)
        if not persisted:
            logger.warning(
                "[GmailApiUrl] %s: no canonical mailbox row for validated OTP",
                account.email,
            )
    except Exception as exc:  # noqa: BLE001 - cache persistence must not fail OTP delivery.
        logger.warning("[GmailApiUrl] %s: failed to persist latest OTP: %s", account.email, exc)


def acknowledge_verification_code(account: GmailApiUrlAccount, otp: str) -> None:
    """Persist an OTP after the remote validation step has succeeded."""
    value = str(otp or "").strip()
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("OTP must be a six-digit code")
    _record_latest_otp(account, value)


def poll_verification_code(
    account: GmailApiUrlAccount,
    max_wait: float = 60.0,
    poll_interval: float = 2.0,
    after_ts: float | None = None,
    before_code: str | None | object = _BEFORE_CODE_UNSET,
    *,
    job_id: int | str | None = None,
    stage: str | None = None,
    sqlite_path=None,
) -> str:
    """轮询取码URL获取验证码。

    Args:
        account:       Gmail API URL 账户
        max_wait:      最大等待时间（秒）
        poll_interval: 轮询间隔（秒）
        after_ts:      调用方的请求时间戳。该 API 不返回邮件时间，
                       因此仅用于调用方关联日志，不能单独判断新旧。
        job_id:        可选任务 ID，用于关联并发轮询日志。
        stage:         可选业务阶段，用于关联并发轮询日志。

    Returns:
        str: 验证码

    Raises:
        GmailApiUrlError: 超时 / code=602 / data.code 缺失
    """
    _ensure_account_pollable(account, sqlite_path=sqlite_path)
    log_context = f"job={job_id or '-'} stage={stage or '-'}"
    baseline_source = "explicit"
    # ── 只有调用方没有提供 baseline 时，才回退到该 code_url 的持久化值 ──
    if before_code is _BEFORE_CODE_UNSET:
        before_code = _get_latest_otp(account)
        baseline_source = "persisted" if before_code else "none"
        if before_code:
            logger.info(
                "[GmailApiUrl] %s: using persisted latest OTP as stale baseline (%s)",
                account.email,
                log_context,
            )
    elif not before_code:
        baseline_source = "explicit_empty"

    logger.info(
        "[GmailApiUrl] %s: OTP poll started baseline=%s (%s)",
        account.email,
        baseline_source,
        log_context,
    )

    start_time = time.time()
    last_error: str | None = None

    while time.time() - start_time < max_wait:
        try:
            api_code, otp = _fetch_code_once(account.code_url)

            if api_code == 0 and otp:
                if before_code and otp == before_code:
                    # 还是旧码，继续等
                    remaining = int(max_wait - (time.time() - start_time))
                    logger.info(
                        "[GmailApiUrl] %s: stale OTP still present; waiting for a new code (%ds left; %s)",
                        account.email, remaining,
                        log_context,
                    )
                    time.sleep(poll_interval)
                    continue
                logger.info(
                    "[GmailApiUrl] %s: new OTP received and returned (%s)",
                    account.email,
                    log_context,
                )
                return otp

            if api_code == 601:
                logger.debug("[GmailApiUrl] %s: waiting (601; %s)", account.email, log_context)
            elif api_code in (-1, -2):
                last_error = f"api_code={api_code}"
                logger.warning(
                    "[GmailApiUrl] %s: 请求异常 %s，稍后重试 (%s)",
                    account.email,
                    last_error,
                    log_context,
                )

        except GmailApiUrlError as exc:
            _quarantine_provider_code_url(account, exc, sqlite_path=sqlite_path)
            raise
        except Exception as exc:  # noqa: BLE001 - transient polling errors are retriable.
            last_error = str(exc)
            logger.warning("[GmailApiUrl] %s: 意外异常 %s (%s)", account.email, exc, log_context)

        time.sleep(poll_interval)

    error_msg = f"Timeout after {max_wait}s waiting for new OTP"
    if last_error:
        error_msg += f"; last_error={last_error}"
    raise GmailApiUrlError(error_msg)


def pick_account() -> GmailApiUrlAccount:
    """从池中领取下一个可用账户
    
    Returns:
        GmailApiUrlAccount: 已领取的账户
    
    Raises:
        GmailApiUrlError: 池为空或DB错误
    """
    from . import db
    
    email_record = db.claim_next_gmail_api_url_email()
    if not email_record:
        raise GmailApiUrlError("Gmail API URL pool empty")
    
    return GmailApiUrlAccount(
        email=email_record["email"],
        code_url=email_record["code_url"]
    )


def get_account_context(email: str) -> GmailApiUrlAccount | None:
    """根据邮箱地址获取账户上下文
    
    Args:
        email: 邮箱地址
    
    Returns:
        Optional[GmailApiUrlAccount]: 账户信息，未找到返回None
    """
    from . import db
    
    record = db.get_gmail_api_url_email_by_email(email)
    if not record:
        return None
    
    return GmailApiUrlAccount(
        email=record["email"],
        code_url=record["code_url"]
    )


def release_account(
    email: str,
    status: str = "available",
    note: str = "",
    *,
    job_id: int | str | None = None,
) -> bool:
    """释放账户回池
    
    Args:
        email: 邮箱地址
        status: 状态 (available/used/failed/disabled)
        note: 备注
    """
    from . import db
    
    # Finalize batch assignment if email is a batch alias. A failed registration
    # retires that alias so the next job cannot immediately claim it again.
    store = _batch_store()
    if job_id is None:
        batch_context = get_batch_account_context(email)
        active = store.find_active_assignment_for_alias(email) if batch_context else None
    else:
        # Resolve ownership by job first.  This prevents a stale/global alias
        # lookup from finalizing another worker's assignment.
        active = store.find_active_assignment_for_job(str(job_id))
        if active is not None:
            active_alias = str(getattr(active, "inventory_id", "") or "").split(
                "----", 1
            )[0]
            if active_alias.casefold() != str(email or "").strip().casefold():
                active = None
        batch_context = get_batch_account_context(email, job_id=job_id) if active else None
    if active:
        if status in {"used", "consumed"}:
            changed = store.complete(active.assignment_id)
            logger.info(
                "Batch assignment %s completed for alias %s",
                active.assignment_id[:8], email,
            )
        elif status in {"released", "cancelled"}:
            changed = store.release(active.assignment_id, reason=note[:300])
            logger.info(
                "Batch assignment %s released for alias %s",
                active.assignment_id[:8], email,
            )
        else:
            changed = store.discard(active.assignment_id, reason=note[:300])
            logger.warning(
                "Batch assignment %s discarded alias %s sau lỗi: %s",
                active.assignment_id[:8], email, note[:100],
            )
        return bool(changed)

    scope_kwargs = {}
    if store.path != getattr(db, "_DEFAULT_SQLITE_PATH", store.path):
        scope_kwargs["sqlite_path"] = store.path
    db.release_gmail_api_url_email(email, status, note, **scope_kwargs)
    return db.get_gmail_api_url_email_by_email(email, **scope_kwargs) is not None


# ============================================================================
# Multi-alias Batch Registration
# ============================================================================
#
# Mô hình: 1 email record (email----code_url) → sinh tối đa 12 alias
# (6 gmail.com + 6 googlemail.com). Mọi alias forward về cùng hộp thư nên
# TẤT CẢ dùng chung code_url của email gốc để lấy OTP.
# Học từ Gmail CDK, nhưng nguồn là kho email----url, KHÔNG dùng CDK.

def create_registration_batch(
    count: int,
    aliases_per_email: int | None = None,
    *,
    allow_partial: bool = False,
) -> str:
    """Serialize alias inventory selection and create one registration batch.

    ``allow_partial`` is used by the QAN8 lazy coordinator: available Gmail
    aliases are included immediately, while an empty/partially filled batch
    remains a valid canonical destination for a later purchase.  The default
    remains strict for direct Gmail API callers.
    """
    with _BATCH_BUILD_LOCK:
        return _create_registration_batch(
            count,
            aliases_per_email=aliases_per_email,
            allow_partial=allow_partial,
        )


def _reconcile_source_alias_ownership(
    store: GmailApiUrlBatchStore,
    code_url: str,
) -> None:
    """Release aliases held only by terminal jobs before allocating a new batch."""
    for batch_id in store.list_batch_ids_for_code_urls({str(code_url or "").strip()}):
        _reconcile_batch_queue(store, batch_id)


def _alias_owned_by_other_code_url(
    store: GmailApiUrlBatchStore,
    alias: str,
    code_url: str,
) -> bool:
    """Check exact/root ownership while keeping lightweight test doubles usable."""
    checker = getattr(store, "has_alias_for_other_code_url", None)
    if checker is None:
        return False
    # The concrete store returns ``bool``.  Identity checking deliberately
    # treats an unconfigured MagicMock as false, while still allowing tests and
    # alternate stores to return an explicit True collision result.
    return checker(alias, code_url) is True


def _create_registration_batch(
    count: int,
    aliases_per_email: int | None = None,
    *,
    allow_partial: bool = False,
) -> str:
    """Claim đủ email gốc từ pool để tạo `count` alias, mỗi email sinh tối đa
    `aliases_per_email` alias (share code_url của email đó).

    Mô hình:
        - Mỗi email gốc sinh tối đa 12 alias (6 gmail.com + 6 googlemail.com).
        - Alias đã cấp trước đó của cùng code_url sẽ bị bỏ qua.
        - Nếu một record còn ít alias mới, allocator lấy thêm email gốc khác.
        - Alias cùng một email gốc share code_url của email đó.

    Args:
        count: Tổng số alias (job) cần tạo.
        aliases_per_email: Số alias mỗi email gốc (1..12). None → dùng max 12.

    Returns:
        batch_id

    Raises:
        GmailApiUrlBatchError: pool không đủ email, hoặc tham số sai.
    """
    from core.gmail_aliases import (
        MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
        GmailAliasError,
        canonical_gmail,
        generate_gmail_dual_domain_aliases,
    )

    from . import db

    if count < 1:
        raise GmailApiUrlBatchError("Batch cần ít nhất 1 alias")

    per_email = aliases_per_email or MAX_GMAIL_DUAL_DOMAIN_VARIANTS
    per_email = max(1, min(MAX_GMAIL_DUAL_DOMAIN_VARIANTS, int(per_email)))

    groups: list[dict] = []
    claimed_sources: list[tuple[str, bool]] = []
    excluded_sources: set[str] = set()
    selected_aliases: set[str] = set()
    selected_roots: dict[str, str] = {}
    remaining = count
    store = _batch_store()
    try:
        while remaining > 0:
            record = db.claim_next_gmail_api_url_email(
                include_used=True,
                exclude_emails=excluded_sources,
            )
            if not record:
                if allow_partial:
                    logger.info(
                        "Gmail API URL pool exhausted while building a partial batch "
                        "(created=%d, remaining=%d)",
                        count - remaining,
                        remaining,
                    )
                    break
                raise GmailApiUrlBatchError(
                    f"Gmail API URL pool không đủ alias mới cho {count} tài khoản, "
                    f"đã claim {len(claimed_sources)} email gốc, còn thiếu {remaining} alias"
                )
            source_email = record["email"]
            source_key = str(source_email or "").strip().casefold()
            code_url = record["code_url"]
            claimed_from_available = bool(record.get("_claimed_from_available", True))
            claimed_sources.append((source_email, claimed_from_available))
            excluded_sources.add(source_key)

            want = min(per_email, remaining)
            try:
                candidates = generate_gmail_dual_domain_aliases(
                    source_email, limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS
                )
            except GmailAliasError as exc:
                raise GmailApiUrlBatchError(
                    f"Email gốc {source_email} không hợp lệ: {exc}"
                ) from exc
            _reconcile_source_alias_ownership(store, code_url)
            used_aliases = store.list_allocated_aliases_for_code_url(code_url)
            globally_unavailable = store.list_globally_unavailable_aliases()
            aliases: list[str] = []
            for alias in candidates:
                normalized_alias = alias.strip().casefold()
                if (
                    normalized_alias in used_aliases
                    or normalized_alias in globally_unavailable
                    or normalized_alias in selected_aliases
                    or _alias_owned_by_other_code_url(store, alias, code_url)
                ):
                    continue
                try:
                    root = canonical_gmail(alias)
                except GmailAliasError:
                    root = ""
                selected_url = selected_roots.get(root) if root else None
                if selected_url is not None and selected_url != code_url:
                    continue
                aliases.append(alias)
                if len(aliases) >= want:
                    break
            if not aliases:
                if store.has_pending_alias_for_code_url(code_url) is True:
                    # The source still backs aliases queued or temporarily
                    # reserved by another worker/batch.  Keep the raw row
                    # usable and let the existing canonical queue drain.
                    claimed_sources.remove((source_email, claimed_from_available))
                    continue
                db.release_gmail_api_url_email(
                    source_email,
                    "exhausted",
                    "Record đã dùng hết alias Gmail khả dụng",
                )
                claimed_sources.remove((source_email, claimed_from_available))
                continue
            groups.append({
                "source_email": source_email,
                "code_url": code_url,
                "aliases": aliases,
            })
            selected_aliases.update(alias.strip().casefold() for alias in aliases)
            for alias in aliases:
                try:
                    selected_roots[canonical_gmail(alias)] = code_url
                except GmailAliasError:
                    continue
            remaining -= len(aliases)
            if remaining <= 0:
                break
    except Exception:
        # Rollback: trả tất cả email gốc đã claim về pool
        for source_email, claimed_from_available in claimed_sources:
            if not claimed_from_available:
                continue
            try:
                db.release_gmail_api_url_email(
                    source_email, "available", "Tạo batch thất bại"
                )
            except Exception as exc:  # noqa: BLE001 - rollback must not hide the original failure.
                logger.warning("Không thể release email %s về pool: %s", source_email, exc)
        raise

    try:
        batch_id = (
            store.create_batch_multi(groups)
            if groups
            else store.create_empty_batch()
        )
    except Exception:
        for source_email, claimed_from_available in claimed_sources:
            if not claimed_from_available:
                continue
            try:
                db.release_gmail_api_url_email(
                    source_email, "available", "Tạo batch thất bại"
                )
            except Exception as exc:  # noqa: BLE001 - rollback must not hide the original failure.
                logger.warning("Không thể release email %s về pool: %s", source_email, exc)
        raise

    total_aliases = sum(len(g["aliases"]) for g in groups)
    logger.info(
        "Đã tạo Gmail API URL batch %s từ %d email gốc với %d alias (mỗi email ≤%d)",
        batch_id, len(groups), total_aliases, per_email,
    )
    return batch_id


def materialize_next_available_source(
    batch_id: str,
    *,
    aliases_per_source: int = 12,
    store: GmailApiUrlBatchStore | None = None,
) -> bool:
    """Move one raw Gmail API source into the canonical batch ledger.

    Imported Gmail API records predate the batch tables and can still have
    usable aliases even when no canonical batch item exists.  QAN8 calls this
    lazy bridge before purchasing: one source is claimed from the raw pool,
    all currently unallocated aliases for that source are appended to the
    requested canonical batch, and workers then claim them through the normal
    Gmail assignment table.

    Returns ``True`` when a source was materialized, or when an already
    canonicalized source was observed and should be retried after a concurrent
    transaction.  ``False`` means the raw pool has no source left to bridge.
    """
    normalized_batch = str(batch_id or "").strip()
    if not normalized_batch:
        raise GmailApiUrlBatchError("Gmail API URL batch ID is required")
    limit = max(1, min(12, int(aliases_per_source or 12)))
    target_store = store or _batch_store()

    from core.gmail_aliases import (
        MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
        GmailAliasError,
        generate_gmail_dual_domain_aliases,
    )

    from . import db

    with _BATCH_BUILD_LOCK:
        attempted: set[str] = set()
        while True:
            record = db.claim_next_gmail_api_url_email(
                include_used=True,
                exclude_emails=attempted,
                sqlite_path=target_store.path,
            )
            if not record:
                return False

            source_email = str(record.get("email") or "").strip()
            code_url = str(record.get("code_url") or "").strip()
            source_key = source_email.casefold()
            if source_key:
                attempted.add(source_key)
            if not source_email or not code_url:
                continue
            if db.is_gmail_api_url_code_url_failed(
                code_url,
                sqlite_path=target_store.path,
            ):
                # claim_next_gmail_api_url_email() marks a newly selected
                # available row as used.  A sibling row for an already
                # quarantined URL must not be left in that misleading state.
                db.fail_gmail_api_url_sources_for_code_url(
                    code_url,
                    note="Gmail API URL đã bị quarantine sau lỗi code=602",
                    sqlite_path=target_store.path,
                )
                continue

            try:
                candidates = generate_gmail_dual_domain_aliases(
                    source_email,
                    limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
                )
            except GmailAliasError:
                if bool(record.get("_claimed_from_available")):
                    db.release_gmail_api_url_email(
                        source_email,
                        "failed",
                        "Gmail source email is not a valid alias root",
                        sqlite_path=target_store.path,
                    )
                continue

            _reconcile_source_alias_ownership(target_store, code_url)
            usage = target_store.alias_usage_for_code_urls({code_url}).get(
                code_url,
                {"allocated": set()},
            )
            allocated = {
                str(alias or "").strip().casefold()
                for alias in usage.get("allocated", set())
            }
            consumed = {
                str(alias or "").strip().casefold()
                for alias in usage.get("consumed", set())
            }
            failed = {
                str(alias or "").strip().casefold()
                for alias in usage.get("failed", set())
            }
            reusable = allocated - consumed - failed
            unavailable = target_store.list_globally_unavailable_aliases()
            aliases = [
                alias
                for alias in candidates
                if alias.strip().casefold() not in allocated
                and alias.strip().casefold() not in unavailable
                and not target_store.has_alias_for_other_code_url(
                    alias, code_url
                )
            ][:limit]
            if not aliases:
                # Existing canonical rows may be temporarily locked or may
                # have been appended by another worker.  Leave the root row
                # untouched and let the caller re-check the shared ledger.
                if reusable:
                    return True
                db.release_gmail_api_url_email(
                    source_email,
                    "exhausted",
                    "Record đã dùng hết alias Gmail khả dụng",
                    sqlite_path=target_store.path,
                )
                continue

            target_store.append_source_group(
                normalized_batch,
                source_email,
                code_url,
                aliases,
            )
            return True


def _reconcile_batch_queue(store: GmailApiUrlBatchStore, batch_id: str) -> None:
    """Resolve locks and waiters left by terminal registration jobs."""
    from . import db

    terminal_states = {"success", "failed", "stopped", "cancelled"}
    for assignment in store.list_active_assignments(batch_id):
        try:
            job = db.get_job(int(assignment.job_id))
        except (TypeError, ValueError):
            continue
        job_status = str((job or {}).get("status") or "")
        if job_status in {"running", "stopping"}:
            from .registration_service import is_job_active

            if is_job_active(int(assignment.job_id)):
                continue
            db.update_job(
                int(assignment.job_id),
                status="failed",
                error="Worker registration không còn tồn tại sau khi tiến trình dừng",
                completed_at=local_now().astimezone().isoformat(timespec="seconds"),
            )
            job_status = "failed"
        if job is not None and job_status not in terminal_states:
            continue
        alias = assignment.inventory_id.split("----", 1)[0]
        account_exists = bool((job or {}).get("account_id") or db.get_account_by_email(alias))
        if account_exists:
            store.complete(assignment.assignment_id)
            logger.info(
                "Đã hoàn tất assignment mồ côi %s vì job %s đã lưu account",
                assignment.assignment_id[:8],
                assignment.job_id,
            )
        elif job is None:
            store.discard(
                assignment.assignment_id,
                reason="missing job reconciliation",
            )
            logger.warning(
                "Đã loại bỏ alias mồ côi %s vì job %s không còn tồn tại",
                alias,
                assignment.job_id,
            )
        elif job_status == "failed":
            store.discard(
                assignment.assignment_id,
                reason=(
                    "orphaned running job reconciliation"
                    if str(job.get("status") or "") in {"running", "stopping"}
                    else "failed job reconciliation"
                ),
            )
            logger.warning(
                "Đã loại bỏ alias của job thất bại %s để không cấp lại",
                assignment.job_id,
            )
        else:
            store.release(assignment.assignment_id, reason="terminal job reconciliation")
            logger.info(
                "Đã giải phóng assignment mồ côi %s của job %s",
                assignment.assignment_id[:8],
                assignment.job_id,
            )

    # Older workers released failed assignments before the discard rule was
    # introduced. Retire those aliases once they are observed during queue
    # reconciliation so historical failures cannot poison later jobs.
    for assignment in store.list_reusable_assignments(batch_id):
        try:
            job = db.get_job(int(assignment.job_id))
        except (TypeError, ValueError):
            continue
        if job and str(job.get("status") or "") == "failed":
            store.discard(
                assignment.assignment_id,
                reason="failed job reconciliation",
            )
            logger.warning(
                "Đã loại bỏ alias cũ của job thất bại %s để không cấp lại",
                assignment.job_id,
            )

    for waiting_job_id in store.list_waiting_jobs(batch_id):
        try:
            job = db.get_job(int(waiting_job_id))
        except (TypeError, ValueError):
            continue
        if job and str(job.get("status") or "") in terminal_states:
            store.cancel_waiter(batch_id, waiting_job_id, "terminal job reconciliation")


def get_email_from_batch(
    batch_id: str,
    job_id: str,
    *,
    wait_timeout: float | None = None,
    poll_interval: float = 1.0,
) -> GmailApiUrlAccount:
    """Claim alias tiếp theo trong batch cho job_id, có hàng đợi bền.

    Args:
        batch_id: Batch ID
        job_id: Job ID duy nhất cho registration job
        wait_timeout: Giới hạn tùy chọn cho caller đặc biệt; None nghĩa là chờ đến khi
            được cấp hoặc batch thực sự hết slot.

    Returns:
        GmailApiUrlAccount(email=alias, code_url=code_url gốc)

    Raises:
        GmailApiUrlBatchConflict: batch hết alias available.
        GmailApiUrlBatchError: inventory_id không đúng định dạng.
    """
    deadline = None if wait_timeout is None else time.monotonic() + max(0.0, float(wait_timeout))
    retry_delay = max(0.0, float(poll_interval))
    store = _batch_store()
    last_status = None
    last_log_at = 0.0
    while True:
        try:
            numeric_job_id = int(job_id)
        except (TypeError, ValueError):
            numeric_job_id = None
        if numeric_job_id is not None:
            from core import registration_service

            if registration_service.is_stop_requested(numeric_job_id):
                store.cancel_waiter(batch_id, job_id, "job stopped by email lane quarantine")
                raise registration_service.StopRequested(
                    f"任务 #{job_id} 已因邮箱 lane 被禁用而停止"
                )
        _reconcile_batch_queue(store, batch_id)
        try:
            assignment = store.claim_waiting(batch_id, job_id)
        except GmailApiUrlBatchConflict:
            assignment = None
        if assignment is not None:
            break
        status = store.batch_status(batch_id)
        if status["exhausted_batch"]:
            store.cancel_waiter(batch_id, job_id, "batch exhausted")
            raise GmailApiUrlBatchConflict("No available Gmail account in batch")
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Job %s vẫn đang chờ batch %s sau %.1fs; giữ waiter trong DB để retry tiếp",
                job_id,
                batch_id,
                max(0.0, float(wait_timeout or 0.0)),
            )
            raise GmailApiUrlBatchConflict(
                "Gmail API URL batch đang bận; job đã được lưu vào hàng đợi"
            )
        status_snapshot = (
            status["pending"],
            status["active_assignments"],
            status["waiting_jobs"],
            status["available_code_urls"],
        )
        now = time.monotonic()
        if status_snapshot != last_status or now - last_log_at >= 30:
            logger.info(
                "Job %s chờ code_url rảnh trong Gmail API URL batch %s "
                "(pending=%s active=%s waiting=%s available_code_urls=%s)",
                job_id,
                batch_id,
                status["pending"],
                status["active_assignments"],
                status["waiting_jobs"],
                status["available_code_urls"],
            )
            last_status = status_snapshot
            last_log_at = now
        time.sleep(retry_delay)
    # inventory_id format từ create_batch_multi: "{alias}----{code_url}"
    try:
        alias, code_url = assignment.inventory_id.split("----", 1)
    except ValueError as exc:
        raise GmailApiUrlBatchError(
            f"inventory_id không đúng định dạng alias----code_url: {assignment.inventory_id}"
        ) from exc
    logger.info(
        "Job %s claim alias %s từ batch %s (code_url dùng chung)",
        job_id, alias, batch_id,
    )
    return GmailApiUrlAccount(
        email=alias,
        code_url=code_url,
    )


def get_batch_account_context(
    alias: str,
    *,
    job_id: int | str | None = None,
) -> GmailApiUrlAccount | None:
    """Tra code_url cho một alias thuộc batch (dùng khi wait_for_otp)."""
    store = _batch_store()
    result = (
        store.find_item_by_alias_for_job(alias, str(job_id))
        if job_id is not None
        else store.find_item_by_alias(alias)
    )
    if not result:
        return None
    found_alias, code_url = result
    return GmailApiUrlAccount(email=found_alias, code_url=code_url)


def has_active_batch_assignment(job_id: int | str) -> bool:
    """Return whether a registration job still owns a Gmail API URL alias."""
    return _batch_store().find_active_assignment_for_job(str(job_id)) is not None


def complete_batch_assignment(batch_id: str, job_id: str) -> bool:
    """Đánh dấu alias assignment hoàn thành. Email gốc trong pool để nguyên."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().complete(assignment.assignment_id)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Completed alias %s (assignment %s) cho job %s",
            alias, assignment.assignment_id, job_id,
        )
    return success


def fail_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    """Đánh dấu alias assignment thất bại."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().fail(assignment.assignment_id, reason)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Failed alias %s (assignment %s) cho job %s: %s",
            alias, assignment.assignment_id, job_id, reason,
        )
    return success


def release_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    """Release alias assignment về available (chưa dùng)."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().release(assignment.assignment_id, reason)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Released alias %s (assignment %s) cho job %s: %s",
            alias, assignment.assignment_id, job_id, reason,
        )
    return success


def quarantine_code_url(code_url: str, *, reason: str = "") -> int:
    """Retire every batch alias sharing a broken provider URL."""
    return _batch_store().quarantine_code_url(code_url, reason=reason)
