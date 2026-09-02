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
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_api_url_batch_store import (
    GmailApiUrlBatchConflict,
    GmailApiUrlBatchError,
    GmailApiUrlBatchStore,
)

logger = logging.getLogger(__name__)
_BEFORE_CODE_UNSET = object()

# Batch store singleton
_BATCH_STORE_PATH = APP_STATE_DB_PATH
_batch_store_instance: GmailApiUrlBatchStore | None = None


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


def _fetch_code_once(code_url: str) -> tuple[int, str | None]:
    """单次调用取码接口，返回 (api_code, otp_or_None)。
    HTTP 错误时返回 (-1, None)；JSON 格式异常时返回 (-2, None)。
    code=602 时直接抛出 GmailApiUrlError（不重试）。
    """
    try:
        resp = requests.get(code_url, timeout=10, allow_redirects=False)
        if resp.status_code >= 400:
            return -1, None
        payload = resp.json()
        api_code = payload.get("code")
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
        return int(api_code) if api_code is not None else -2, None
    except GmailApiUrlError:
        raise
    except requests.RequestException:
        return -1, None
    except (ValueError, KeyError):
        return -2, None


def snapshot_verification_code(account: GmailApiUrlAccount) -> str | None:
    """Return the currently visible code without logging, waiting, or persisting."""
    api_code, otp = _fetch_code_once(account.code_url)
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

        except GmailApiUrlError:
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


def release_account(email: str, status: str = "available", note: str = "") -> bool:
    """释放账户回池
    
    Args:
        email: 邮箱地址
        status: 状态 (available/used/failed/disabled)
        note: 备注
    """
    from . import db
    
    # Finalize batch assignment if email is a batch alias. A failed registration
    # retires that alias so the next job cannot immediately claim it again.
    batch_context = get_batch_account_context(email)
    if batch_context:
        active = _batch_store().find_active_assignment_for_alias(email)
        if active:
            if status in {"used", "consumed"}:
                changed = _batch_store().complete(active.assignment_id)
                logger.info(
                    "Batch assignment %s completed for alias %s",
                    active.assignment_id[:8], email,
                )
            elif status in {"released", "cancelled"}:
                changed = _batch_store().release(active.assignment_id, reason=note[:300])
                logger.info(
                    "Batch assignment %s released for alias %s",
                    active.assignment_id[:8], email,
                )
            else:
                changed = _batch_store().discard(active.assignment_id, reason=note[:300])
                logger.warning(
                    "Batch assignment %s discarded alias %s sau lỗi: %s",
                    active.assignment_id[:8], email, note[:100],
                )
            return bool(changed)

    db.release_gmail_api_url_email(email, status, note)
    return db.get_gmail_api_url_email_by_email(email) is not None


# ============================================================================
# Multi-alias Batch Registration
# ============================================================================
#
# Mô hình: 1 email record (email----code_url) → sinh tối đa 12 alias
# (6 gmail.com + 6 googlemail.com). Mọi alias forward về cùng hộp thư nên
# TẤT CẢ dùng chung code_url của email gốc để lấy OTP.
# Học từ Gmail CDK, nhưng nguồn là kho email----url, KHÔNG dùng CDK.

def create_registration_batch(count: int, aliases_per_email: int | None = None) -> str:
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
        generate_gmail_dual_domain_aliases,
    )

    from . import db

    if count < 1:
        raise GmailApiUrlBatchError("Batch cần ít nhất 1 alias")

    per_email = aliases_per_email or MAX_GMAIL_DUAL_DOMAIN_VARIANTS
    per_email = max(1, min(MAX_GMAIL_DUAL_DOMAIN_VARIANTS, int(per_email)))

    groups: list[dict] = []
    claimed_sources: list[str] = []
    remaining = count
    store = _batch_store()
    try:
        while remaining > 0:
            record = db.claim_next_gmail_api_url_email()
            if not record:
                raise GmailApiUrlBatchError(
                    f"Gmail API URL pool không đủ alias mới cho {count} tài khoản, "
                    f"đã claim {len(claimed_sources)} email gốc, còn thiếu {remaining} alias"
                )
            source_email = record["email"]
            code_url = record["code_url"]
            claimed_sources.append(source_email)

            want = min(per_email, remaining)
            try:
                candidates = generate_gmail_dual_domain_aliases(
                    source_email, limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS
                )
            except GmailAliasError as exc:
                raise GmailApiUrlBatchError(
                    f"Email gốc {source_email} không hợp lệ: {exc}"
                ) from exc
            used_aliases = store.list_aliases_for_code_url(code_url)
            aliases = [
                alias for alias in candidates
                if alias.strip().casefold() not in used_aliases
            ][:want]
            if not aliases:
                db.release_gmail_api_url_email(
                    source_email,
                    "used",
                    "Record đã dùng hết alias Gmail khả dụng",
                )
                claimed_sources.remove(source_email)
                continue
            groups.append({
                "source_email": source_email,
                "code_url": code_url,
                "aliases": aliases,
            })
            remaining -= len(aliases)
            if remaining <= 0:
                break
    except Exception:
        # Rollback: trả tất cả email gốc đã claim về pool
        for source_email in claimed_sources:
            try:
                db.release_gmail_api_url_email(
                    source_email, "available", "Tạo batch thất bại"
                )
            except Exception as exc:  # noqa: BLE001 - rollback must not hide the original failure.
                logger.warning("Không thể release email %s về pool: %s", source_email, exc)
        raise

    try:
        batch_id = store.create_batch_multi(groups)
    except Exception:
        for source_email in claimed_sources:
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
                completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
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


def get_batch_account_context(alias: str) -> GmailApiUrlAccount | None:
    """Tra code_url cho một alias thuộc batch (dùng khi wait_for_otp)."""
    result = _batch_store().find_item_by_alias(alias)
    if not result:
        return None
    found_alias, code_url = result
    return GmailApiUrlAccount(email=found_alias, code_url=code_url)


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


def quarantine_code_url(batch_id: str, code_url: str, *, reason: str = "") -> int:
    """Retire every alias in a batch that shares a broken provider URL."""
    return _batch_store().quarantine_code_url(batch_id, code_url, reason=reason)
