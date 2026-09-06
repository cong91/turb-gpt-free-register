"""Gmail API URL 邮箱池客户端

通过轮询取码URL获取验证码，支持响应码处理：
- code=601: 等待验证码（继续轮询）
- code=602: 邮箱错误/服务商问题（抛出异常，调用方标记为failed）
- code=0 + data.code: 成功获取验证码

格式：email----code_url
示例：user@gmail.com----https://gapi.mailsapi.com/api/get-code?uid=abc123

"""

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import requests

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_api_url_batch_store import (
    GmailApiUrlBatchConflict,  # noqa: F401 - public facade re-export
    GmailApiUrlBatchError,  # noqa: F401 - public facade re-export
    GmailApiUrlBatchStore,
)

logger = logging.getLogger(__name__)
_BEFORE_CODE_UNSET = object()
_PROVIDER_602_RE = re.compile(
    r"(?:\bcode|\bstatus|\bhttp(?:\s+status)?|\berror)\s*[:=]?\s*602\b",
    re.IGNORECASE,
)

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


def _extract_qan8_uid(code_url: str) -> str | None:
    """Extract the UID required by QAN8's 602 after-sales API."""
    try:
        values = parse_qs(urlsplit(str(code_url or "")).query).get("uid", [])
    except (TypeError, ValueError):
        return None
    if len(values) != 1:
        return None
    uid = str(values[0] or "").strip()
    return uid or None


def _is_qan8_purchased_code_url(code_url: str, *, sqlite_path=None) -> bool:
    """Limit after-sales requests to sources delivered by a QAN8 order."""
    try:
        return _runtime_store(sqlite_path).has_purchase_order_for_code_url(code_url)
    except Exception:
        logger.exception("[GmailApiUrl] Could not verify QAN8 purchase provenance")
        return False


def _record_qan8_after_sales_observation(
    code_url: str,
    response_code: int,
    *,
    otp_received: bool = False,
    sqlite_path=None,
) -> None:
    """Persist only QAN8 response history needed for after-sales eligibility."""
    if not _is_qan8_purchased_code_url(code_url, sqlite_path=sqlite_path):
        return
    try:
        _runtime_store(sqlite_path).record_qan8_after_sales_observation(
            code_url,
            response_code,
            otp_received=otp_received,
        )
    except Exception:
        logger.exception("[GmailApiUrl] Could not persist QAN8 after-sales eligibility")


def _is_qan8_after_sales_eligible(code_url: str, *, sqlite_path=None) -> bool:
    """Allow after-sales only when the first provider response was 602."""
    try:
        eligible = _runtime_store(sqlite_path).is_qan8_after_sales_eligible(code_url)
        if not eligible:
            return False
        from core import db

        # Sources used before the observation ledger existed may still expose
        # a persisted OTP in the raw pool; that historical receipt also blocks
        # an automatic refund.
        return not bool(db.get_gmail_api_url_last_otp(code_url))
    except Exception:
        logger.exception("[GmailApiUrl] Could not verify QAN8 after-sales eligibility")
        return False


def _request_qan8_after_sales(uid: str, *, code_url: str, sqlite_path=None) -> None:
    """Request automatic after-sales without masking the original 602 error."""
    try:
        store = _runtime_store(sqlite_path)
        claimed = store.claim_after_sales_uid(uid, code_url)
    except Exception as exc:  # noqa: BLE001 - local persistence must not mask 602.
        logger.warning("[GmailApiUrl] QAN8 after-sales claim failed: %s", type(exc).__name__)
        return
    if not claimed:
        return
    uid_label = f"...{str(uid)[-4:]}"
    try:
        from core.qan8_gmail_api_client import Qan8GmailApiClient

        payload = Qan8GmailApiClient().request_after_sales(uid)
        success = payload.get("success") is True
        try:
            store.finish_after_sales_uid(
                uid,
                success=success,
                message=str(payload.get("message") or "")[:200],
            )
        except Exception as exc:  # noqa: BLE001 - outcome persistence is best effort.
            logger.warning("[GmailApiUrl] QAN8 after-sales outcome persistence failed: %s", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - refund failure must not mask 602.
        error_message = str(exc).replace(str(uid), "<redacted>")[:200]
        try:
            store.finish_after_sales_uid(uid, success=False, message=error_message)
        except Exception as persist_exc:  # noqa: BLE001 - preserve the original 602 flow.
            logger.warning(
                "[GmailApiUrl] QAN8 after-sales failure persistence failed: %s",
                type(persist_exc).__name__,
            )
        logger.warning(
            "[GmailApiUrl] QAN8 after-sales request failed for uid=%s: %s",
            uid_label,
            type(exc).__name__,
        )
        return
    logger.info(
        "[GmailApiUrl] QAN8 after-sales response uid=%s success=%s code=%s message=%s",
        uid_label,
        payload.get("success"),
        payload.get("code"),
        str(payload.get("message") or "")[:200],
    )


def _fetch_code_once(code_url: str, *, sqlite_path=None) -> tuple[int, str | None]:
    """单次调用取码接口，返回 (api_code, otp_or_None)。
    HTTP 错误时返回 (-1, None)；JSON 格式异常时返回 (-2, None)。
    code=602 时直接抛出 GmailApiUrlError（不重试）。
    """
    try:
        resp = requests.get(code_url, timeout=10, allow_redirects=False)
        if resp.status_code == 602:
            _record_qan8_after_sales_observation(code_url, 602, sqlite_path=sqlite_path)
            raise GmailApiUrlError(
                "Provider error code=602: HTTP status 602. Contact provider for refund."
            )
        if resp.status_code >= 400:
            _record_qan8_after_sales_observation(
                code_url,
                int(resp.status_code),
                sqlite_path=sqlite_path,
            )
            return -1, None
        try:
            payload = resp.json()
        except (TypeError, ValueError):
            _record_qan8_after_sales_observation(code_url, -2, sqlite_path=sqlite_path)
            return -2, None
        if not isinstance(payload, dict):
            _record_qan8_after_sales_observation(code_url, -2, sqlite_path=sqlite_path)
            return -2, None
        raw_code = payload.get("code")
        try:
            api_code = int(raw_code) if raw_code is not None else -2
        except (TypeError, ValueError):
            api_code = -2
        if api_code not in (601, 602):
            _record_qan8_after_sales_observation(
                code_url,
                api_code,
                sqlite_path=sqlite_path,
            )
        if api_code == 602:
            _record_qan8_after_sales_observation(code_url, 602, sqlite_path=sqlite_path)
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
            _record_qan8_after_sales_observation(
                code_url,
                0,
                otp_received=True,
                sqlite_path=sqlite_path,
            )
            return 0, otp
        if api_code == 601:
            _record_qan8_after_sales_observation(code_url, 601, sqlite_path=sqlite_path)
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
    uid = _extract_qan8_uid(code_url)
    if (
        uid
        and _is_qan8_purchased_code_url(code_url, sqlite_path=sqlite_path)
        and _is_qan8_after_sales_eligible(code_url, sqlite_path=sqlite_path)
    ):
        _request_qan8_after_sales(uid, code_url=code_url, sqlite_path=sqlite_path)
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
        api_code, otp = _fetch_code_once(account.code_url, sqlite_path=sqlite_path)
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
            api_code, otp = _fetch_code_once(account.code_url, sqlite_path=sqlite_path)

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


# Batch coordination lives in ``gmail_api_url_batch_coordinator``.  These
# imports remain thin public facades for existing registration/provider callers
# while keeping HTTP and OTP behavior in this module.
def _batch_store() -> GmailApiUrlBatchStore:
    from core.gmail_api_url_batch_coordinator import _batch_store as get_store

    return get_store()


def release_account(
    email: str,
    status: str = "available",
    note: str = "",
    *,
    job_id: int | str | None = None,
) -> bool:
    from core.gmail_api_url_batch_coordinator import release_account as release

    return release(email, status, note, job_id=job_id)


def create_registration_batch(
    count: int,
    aliases_per_email: int | None = None,
    *,
    allow_partial: bool = False,
) -> str:
    from core.gmail_api_url_batch_coordinator import create_registration_batch as create

    return create(count, aliases_per_email=aliases_per_email, allow_partial=allow_partial)


def materialize_next_available_source(
    batch_id: str,
    *,
    aliases_per_source: int = 12,
    store: GmailApiUrlBatchStore | None = None,
) -> bool:
    from core.gmail_api_url_batch_coordinator import (
        materialize_next_available_source as materialize,
    )

    return materialize(batch_id, aliases_per_source=aliases_per_source, store=store)


def provision_next_gmail_api_url_source(
    batch_id: str,
    *,
    aliases_per_source: int = 12,
    store: GmailApiUrlBatchStore | None = None,
) -> bool:
    from core.gmail_api_url_batch_coordinator import (
        provision_next_gmail_api_url_source as provision,
    )

    return provision(batch_id, aliases_per_source=aliases_per_source, store=store)


def get_email_from_batch(
    batch_id: str,
    job_id: int | str,
    *,
    wait_timeout: float | None = None,
    poll_interval: float = 1.0,
    aliases_per_source: int = 12,
) -> GmailApiUrlAccount:
    from core.gmail_api_url_batch_coordinator import get_email_from_batch as get_email

    return get_email(
        batch_id,
        job_id,
        wait_timeout=wait_timeout,
        poll_interval=poll_interval,
        aliases_per_source=aliases_per_source,
    )


def get_batch_account_context(
    email: str,
    *,
    job_id: int | str | None = None,
    batch_id: str | None = None,
) -> GmailApiUrlAccount | None:
    from core.gmail_api_url_batch_coordinator import (
        get_batch_account_context as get_context,
    )

    return get_context(email, job_id=job_id, batch_id=batch_id)


def _reconcile_batch_queue(store: GmailApiUrlBatchStore, batch_id: str) -> None:
    from core.gmail_api_url_batch_coordinator import _reconcile_batch_queue as reconcile

    reconcile(store, batch_id)


def has_active_batch_assignment(job_id: int | str) -> bool:
    from core.gmail_api_url_batch_coordinator import (
        has_active_batch_assignment as has_active,
    )

    return has_active(job_id)


def complete_batch_assignment(batch_id: str, job_id: str) -> bool:
    from core.gmail_api_url_batch_coordinator import (
        complete_batch_assignment as complete,
    )

    return complete(batch_id, job_id)


def fail_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    from core.gmail_api_url_batch_coordinator import fail_batch_assignment as fail

    return fail(batch_id, job_id, reason)


def release_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    from core.gmail_api_url_batch_coordinator import release_batch_assignment as release

    return release(batch_id, job_id, reason)


def quarantine_code_url(code_url: str, *, reason: str = "") -> int:
    from core.gmail_api_url_batch_coordinator import quarantine_code_url as quarantine

    return quarantine(code_url, reason=reason)
