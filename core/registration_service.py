"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import codex_retry_service, db
from core.openai_auth import account_unusable_message
from core.registration_maintenance_barrier import RegistrationMaintenanceBarrier

logger = logging.getLogger(__name__)


def _local_now() -> datetime:
    """Return local wall time without changing the persisted naive timestamp format."""
    return datetime.now(timezone.utc).astimezone().replace(tzinfo=None)

# 全局线程池，最大并发数（WebUI 每次提交时可按最新 workers 重建）
_DEFAULT_MAX_WORKERS = 4
_MIN_MAX_WORKERS = 1
_MAX_MAX_WORKERS = 16
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()
_JOB_EMAIL_INPUTS: dict[int, dict[str, Any]] = {}
_JOB_EMAIL_INPUTS_LOCK = threading.Lock()

_MAINTENANCE_BARRIER = RegistrationMaintenanceBarrier()
_rotation_pending = False
_ROTATION_LOCK = threading.Lock()
_SUPPORTED_BROWSER_TWOFA_DRIVERS = frozenset({"roxy", "cloak", "browser_use", "skyvern"})


class StopRequested(RuntimeError):
    """用户手动停止注册任务。"""


def _set_job_email_inputs(
    job_id: int,
    gmail_cdks: list[str] | None,
    *,
    paymesh_cdks: list[str] | None = None,
    gmail_routed_domains: list[str] | None = None,
    gmail_batch_id: str | None = None,
    email_source: str | None = None,
    paymesh_inventory_id: str | None = None,
    paymesh_routed_domains: list[str] | None = None,
    gmail_api_url_batch_id: str | None = None,
    qan8_gmail_api_batch_id: str | None = None,
    qan8_gmail_api_lane_id: int | None = None,
) -> None:
    with _JOB_EMAIL_INPUTS_LOCK:
        _JOB_EMAIL_INPUTS[int(job_id)] = {
            "email_source": str(email_source).strip() if email_source else None,
            "gmail_cdks": list(gmail_cdks or []),
            "gmail_routed_domains": list(gmail_routed_domains or []),
            "gmail_batch_id": str(gmail_batch_id or "").strip() or None,
            "gmail_api_url_batch_id": str(gmail_api_url_batch_id or "").strip() or None,
            "qan8_gmail_api_batch_id": str(qan8_gmail_api_batch_id or "").strip() or None,
            "qan8_gmail_api_lane_id": (
                int(qan8_gmail_api_lane_id) if qan8_gmail_api_lane_id is not None else None
            ),
            "paymesh_cdks": list(paymesh_cdks or []),
            "paymesh_inventory_id": str(paymesh_inventory_id or "").strip() or None,
            "paymesh_routed_domains": list(paymesh_routed_domains or []),
        }


def _get_job_email_inputs(job_id: int | None) -> dict[str, Any]:
    empty = {
        "email_source": None,
        "gmail_cdks": [],
        "gmail_routed_domains": [],
        "gmail_batch_id": None,
        "gmail_api_url_batch_id": None,
        "paymesh_cdks": [],
        "paymesh_routed_domains": [],
    }
    if job_id is None:
        return empty
    with _JOB_EMAIL_INPUTS_LOCK:
        context = _JOB_EMAIL_INPUTS.get(int(job_id))
        if context is not None:
            result = {
                "email_source": context.get("email_source"),
                "gmail_cdks": list(context.get("gmail_cdks") or []),
                "gmail_routed_domains": list(context.get("gmail_routed_domains") or []),
                "gmail_batch_id": context.get("gmail_batch_id"),
                "gmail_api_url_batch_id": context.get("gmail_api_url_batch_id"),
                "paymesh_cdks": list(context.get("paymesh_cdks") or []),
                "paymesh_routed_domains": list(context.get("paymesh_routed_domains") or []),
            }
            if context.get("qan8_gmail_api_batch_id"):
                result["qan8_gmail_api_batch_id"] = context["qan8_gmail_api_batch_id"]
                result["qan8_gmail_api_lane_id"] = context.get("qan8_gmail_api_lane_id")
            if context.get("paymesh_inventory_id"):
                result["paymesh_inventory_id"] = context["paymesh_inventory_id"]
            return result
    job = db.get_job(int(job_id))
    persisted = job.get("provider_context") if job else {}
    if not isinstance(persisted, dict):
        persisted = {}
    result = {
        "email_source": job.get("email_source") if job else None,
        "gmail_cdks": [],
        "gmail_routed_domains": list(persisted.get("gmail_routed_domains") or []),
        "gmail_batch_id": str(persisted.get("gmail_batch_id") or "").strip() or None,
        "gmail_api_url_batch_id": str(persisted.get("gmail_api_url_batch_id") or "").strip() or None,
        "paymesh_cdks": [],
        "paymesh_routed_domains": list(persisted.get("paymesh_routed_domains") or []),
    }
    qan8_batch_id = str(persisted.get("qan8_gmail_api_batch_id") or "").strip()
    if qan8_batch_id:
        result["qan8_gmail_api_batch_id"] = qan8_batch_id
        result["qan8_gmail_api_lane_id"] = persisted.get("qan8_gmail_api_lane_id")
    paymesh_inventory_id = str(persisted.get("paymesh_inventory_id") or "").strip()
    if paymesh_inventory_id:
        result["paymesh_inventory_id"] = paymesh_inventory_id
    return result


def _clear_job_email_inputs(job_id: int) -> None:
    with _JOB_EMAIL_INPUTS_LOCK:
        _JOB_EMAIL_INPUTS.pop(int(job_id), None)


def _activate_job(job_id: int) -> bool:
    """Mark job as active, but check barrier first. Returns False if blocked."""
    allowed = _MAINTENANCE_BARRIER.wait_before_start(
        int(job_id),
        lambda: is_stop_requested(int(job_id)),
    )
    if not allowed:
        return False
    _THREAD_CTX.job_id = int(job_id)
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))
    return True


def _deactivate_job(job_id: int) -> None:
    _MAINTENANCE_BARRIER.notify_job_finished(int(job_id))
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))
    _clear_job_email_inputs(job_id)
    try:
        delattr(_THREAD_CTX, "job_id")
    except AttributeError:
        logger.debug("[Service] job thread context was already cleared")
    # Deferred NordVPN rotation: close the gate immediately when a
    # rotation is pending so replacement workers cannot start.
    # deferred_rotation drains remaining workers, rotates, and reopens.
    global _rotation_pending
    if _rotation_pending:
        if not _ROTATION_LOCK.acquire(blocking=False):
            return
        try:
            _append_job_log(job_id, "NordVPN auto-rotation started", level="INFO", marker="auto-rotation")
            try:
                from core.nordvpn_cli import execute_rotation
                rotated = _MAINTENANCE_BARRIER.deferred_rotation(
                    rotation_callback=execute_rotation,
                    reason="NordVPN IP rotation (auto)",
                )
            except Exception:
                rotated = False
                _append_job_log(
                    job_id,
                    "NordVPN auto-rotation raised an exception; pending retry",
                    marker="auto-rotation",
                )
                logger.exception("[Service] deferred NordVPN rotation 失败")
            if rotated:
                _rotation_pending = False
                from core.nordvpn_cli import rotation_status_detail

                rotation_detail = rotation_status_detail().get("detail")
                _append_job_log(
                    job_id,
                    "NordVPN auto-rotation success"
                    + (f": {rotation_detail}" if rotation_detail else ""),
                    level="INFO",
                    marker="auto-rotation",
                )
            else:
                from core.nordvpn_cli import rotation_status_detail

                rotation_error = (
                    rotation_status_detail().get("error")
                    or "rotation callback returned false without a diagnostic reason"
                )
                _append_job_log(
                    job_id,
                    f"NordVPN auto-rotation failed: {rotation_error}; start gate remains closed",
                    marker="auto-rotation",
                )
        finally:
            _ROTATION_LOCK.release()


def is_job_active(job_id: int) -> bool:
    """Return whether this process still owns a live registration worker."""
    with _STOP_LOCK:
        return int(job_id) in _ACTIVE_JOBS


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    job_id = getattr(_THREAD_CTX, "job_id", None)
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def _append_job_log(
    job_id: int,
    message: str,
    *,
    level: str = "WARNING",
    marker: str = "service",
) -> None:
    try:
        job = db.get_job(job_id)
        log_file = job.get("log_file") if job else None
        if not log_file:
            return
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        ts = _local_now().strftime("%H:%M:%S")
        normalized_level = str(level or "INFO").strip().upper()
        normalized_marker = str(marker or "service").strip() or "service"
        with Path(log_file).open("a", encoding="utf-8") as f:
            f.write(f"{ts} [{normalized_level}] [{normalized_marker}] {message}\n")
    except Exception as exc:
        logger.debug("[Service] append job log failed: %s", exc, exc_info=True)


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    from core.name_samples import random_display_name

    return random_display_name()


def _prepare_registration_args(job_id: int | None = None) -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import email as _e
    from config import register as _r
    from core.email_provider import acquire_email
    from core.profile_utils import generate_random_birthday

    email = str(getattr(_r, "REGISTER_EMAIL", "") or "").strip()
    name = str(getattr(_r, "REGISTER_NAME", "") or "").strip()
    # WebUI/配置里有时会把空值存成 "-"，这不是合法 OpenAI 显示名，按空处理并自动生成
    if name in {"-", "—", "无", "空", "none", "None", "null", "NULL"}:
        name = ""

    if not name:
        # 手动模式也自动生成显示名，减少配置负担
        name = _random_display_name()

    birthday = generate_random_birthday()

    # 邮箱领取会把池状态置为 used，因此放在所有其他准备逻辑之后。
    if not email:
        if _e.USE_EMAIL_SERVICE:
            email_inputs = _get_job_email_inputs(job_id)
            acquire_kwargs = {"job_id": job_id}
            source = email_inputs["email_source"]
            if source == "gmail_123452026":
                if email_inputs["gmail_batch_id"]:
                    acquire_kwargs["gmail_batch_id"] = email_inputs["gmail_batch_id"]
                else:
                    acquire_kwargs["gmail_cdks"] = email_inputs["gmail_cdks"]
                if email_inputs["gmail_routed_domains"]:
                    acquire_kwargs["gmail_routed_domains"] = email_inputs["gmail_routed_domains"]
            elif source == "gmail_api_url":
                # Multi-alias batch: mọi alias share code_url của email gốc.
                if email_inputs.get("gmail_api_url_batch_id"):
                    acquire_kwargs["gmail_api_url_batch_id"] = email_inputs["gmail_api_url_batch_id"]
            elif source == "qan8_gmail_api":
                acquire_kwargs["qan8_gmail_api_batch_id"] = email_inputs["qan8_gmail_api_batch_id"]
                acquire_kwargs["qan8_gmail_api_lane_id"] = email_inputs["qan8_gmail_api_lane_id"]
            elif source == "paymesh":
                if email_inputs.get("paymesh_inventory_id"):
                    acquire_kwargs["paymesh_inventory_id"] = email_inputs["paymesh_inventory_id"]
                else:
                    acquire_kwargs["paymesh_cdks"] = email_inputs["paymesh_cdks"]
                if email_inputs["paymesh_routed_domains"]:
                    acquire_kwargs["paymesh_routed_domains"] = email_inputs["paymesh_routed_domains"]
            else:
                if email_inputs["gmail_batch_id"]:
                    acquire_kwargs["gmail_batch_id"] = email_inputs["gmail_batch_id"]
                    if email_inputs["gmail_routed_domains"]:
                        acquire_kwargs["gmail_routed_domains"] = email_inputs["gmail_routed_domains"]
                else:
                    acquire_kwargs["gmail_cdks"] = email_inputs["gmail_cdks"]
                if email_inputs.get("paymesh_inventory_id"):
                    acquire_kwargs["paymesh_inventory_id"] = email_inputs["paymesh_inventory_id"]
                else:
                    acquire_kwargs["paymesh_cdks"] = email_inputs["paymesh_cdks"]
            if source is not None:
                acquire_kwargs["email_source"] = source
            email = acquire_email(**acquire_kwargs)
        else:
            raise RuntimeError(
                "手动模式未配置邮箱。请在 WebUI 配置页设置 REGISTER_EMAIL，"
                "或开启 USE_EMAIL_SERVICE 并从邮箱池领取。"
            )

    return email, name, birthday


def _release_unconsumed_job_email(
    email: str | None,
    reason: str,
    *,
    discard_on_failure: bool = False,
) -> bool:
    """任务结束时回收邮箱；真正注册失败时废弃 Gmail API/QAN8 alias。"""
    if not email:
        return False
    try:
        from core.email_provider import release_email_if_unconsumed

        return bool(release_email_if_unconsumed(
            email,
            note=f"任务未消耗，已自动回收: {reason[:180]}",
            discard_on_failure=discard_on_failure,
        ))
    except Exception:
        logger.exception("[Service] 回收未消耗邮箱失败: %s", email)
        return False


def quarantine_provider_lane(
    *,
    job_id: int,
    source: str,
    code_url: str,
    provider_batch_id: str | None = None,
    provider_lane_id: int | None = None,
    reason: str,
) -> dict[str, int | str | None]:
    """Quarantine a failed URL lane and stop every job assigned to that lane."""
    current = db.get_job(int(job_id)) or {}
    context = current.get("provider_context") if isinstance(current, dict) else {}
    if not isinstance(context, dict):
        context = {}

    normalized_source = str(source or "").strip().lower()
    batch_id = str(
        provider_batch_id
        or context.get(
            "gmail_api_url_batch_id"
            if normalized_source == "gmail_api_url"
            else "qan8_gmail_api_batch_id"
        )
        or ""
    ).strip()
    if normalized_source == "gmail_api_url":
        lane_value = context.get("proxy_lane_id")
    else:
        lane_value = provider_lane_id
        if lane_value is None:
            lane_value = context.get("qan8_gmail_api_lane_id")
    try:
        lane_id = int(lane_value) if lane_value is not None else None
    except (TypeError, ValueError):
        lane_id = None

    provider_items = 0
    if normalized_source == "gmail_api_url" and batch_id and str(code_url or "").strip():
        from core.gmail_api_url_client import quarantine_code_url

        provider_items = int(
            quarantine_code_url(batch_id, str(code_url).strip(), reason=str(reason or ""))
            or 0
        )
    elif normalized_source == "qan8_gmail_api" and batch_id and lane_id is not None:
        from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator

        provider_items = int(
            Qan8GmailApiAllocator().quarantine_lane(
                batch_id,
                lane_id,
                reason=str(reason or ""),
            )
            or 0
        )
    else:
        logger.warning(
            "[Service] Không thể quarantine email lane: source=%s batch=%s lane=%s job=%s",
            normalized_source,
            batch_id or "-",
            lane_id if lane_id is not None else "-",
            job_id,
        )

    # Gmail API URL batches do not have provider lanes. `proxy_lane_id` is a
    # network lane and must never be used to cancel unrelated jobs. The
    # provider quarantine above already exhausts every alias for this URL;
    # the current job will enter its normal failure path.
    if normalized_source == "gmail_api_url":
        logger.warning(
            "[Service] Quarantined Gmail API URL: batch=%s code_url=%s provider_items=%s reason=%s",
            batch_id or "-",
            str(code_url or "")[:180],
            provider_items,
            str(reason or "")[:180],
        )
        return {
            "source": normalized_source,
            "batch_id": batch_id or None,
            "lane_id": None,
            "provider_items": provider_items,
            "cancelled": 0,
            "stopping": 0,
        }

    lane_error = (
        f"Email provider code=602; lane {lane_id if lane_id is not None else '-'} "
        "đã bị vô hiệu hóa và các job cùng lane đã được hủy"
    )
    cancelled = 0
    stopping = 0
    now_iso = _local_now().isoformat(timespec="seconds")
    jobs = db.list_jobs(limit=100000)
    for job in jobs:
        candidate_id = int(job.get("id") or 0)
        if not candidate_id:
            continue
        candidate_context = job.get("provider_context")
        if not isinstance(candidate_context, dict):
            candidate_context = {}
        if str(job.get("email_source") or "").strip().lower() != normalized_source:
            continue
        candidate_batch = str(
            candidate_context.get(
                "gmail_api_url_batch_id"
                if normalized_source == "gmail_api_url"
                else "qan8_gmail_api_batch_id"
            )
            or ""
        ).strip()
        if batch_id:
            if candidate_batch != batch_id:
                continue
        elif candidate_batch:
            continue
        candidate_lane_value = candidate_context.get(
            "proxy_lane_id"
            if normalized_source == "gmail_api_url"
            else "qan8_gmail_api_lane_id"
        )
        try:
            candidate_lane = int(candidate_lane_value)
        except (TypeError, ValueError):
            continue
        if lane_id is None or candidate_lane != lane_id:
            continue

        status = str(job.get("status") or "").strip().lower()
        if status == "pending":
            db.update_job(
                candidate_id,
                status="cancelled",
                completed_at=now_iso,
                error=lane_error,
            )
            cancelled += 1
        elif status in {"running", "stopping"}:
            with _STOP_LOCK:
                event = _STOP_EVENTS.get(candidate_id)
                if candidate_id in _ACTIVE_JOBS and event is not None:
                    event.set()
                    is_live = True
                else:
                    is_live = False
            if is_live:
                db.update_job(candidate_id, status="stopping", error=lane_error)
                stopping += 1
            else:
                db.update_job(
                    candidate_id,
                    status="cancelled",
                    completed_at=now_iso,
                    error=lane_error,
                )
                cancelled += 1

    logger.warning(
        "[Service] Quarantined email lane: source=%s batch=%s lane=%s provider_items=%s "
        "cancelled=%s stopping=%s reason=%s",
        normalized_source,
        batch_id or "-",
        lane_id if lane_id is not None else "-",
        provider_items,
        cancelled,
        stopping,
        str(reason or "")[:180],
    )
    return {
        "source": normalized_source,
        "batch_id": batch_id or None,
        "lane_id": lane_id,
        "provider_items": provider_items,
        "cancelled": cancelled,
        "stopping": stopping,
    }


def _consume_recoverable_twofa_assignment(email: str | None, reason: str) -> bool:
    """消费已创建账号的邮箱 alias，同时释放其 provider lane。"""
    if not email:
        return False
    try:
        from core.email_provider import mark_email_consumed, resolve_email_source

        source = resolve_email_source(email)
        if source not in {"qan8_gmail_api", "gmail_api_url"}:
            return False
        changed = bool(mark_email_consumed(email))
        if changed:
            logger.info(
                "[Service] 已消费 2FA 可恢复账号的邮箱 assignment: source=%s email=%s reason=%s",
                source,
                email,
                reason[:180],
            )
        else:
            logger.warning(
                "[Service] 消费 2FA 可恢复账号邮箱 assignment 未发生状态变化: source=%s email=%s",
                source,
                email,
            )
        return changed
    except Exception:
        logger.exception("[Service] 消费 2FA 可恢复账号邮箱 assignment 失败: %s", email)
        return False




def _is_final_session_access_token_timeout(error: object) -> bool:
    """
    识别注册最后一步已经返回 /api/auth/session 200 但没有 accessToken 的失败。
    这种邮箱后续继续注册通常会卡在同一状态，按要求直接停用邮箱池条目。
    """
    text = str(error or "")
    if not text:
        return False
    return (
        "等待 /api/auth/session accessToken 超时" in text
        and "WARNING_BANNER" in text
        and "'_http_status': 200" in text
    )


def _should_disable_failed_registration_email(error: object) -> bool:
    """需要直接停用邮箱的注册失败类型。"""
    text = str(error or "")
    if not text:
        return False
    unsupported_email = "about-you 提交失败" in text and any(marker in text.lower() for marker in (
        "this email is not supported",
        "email is not supported",
        "email address is not supported",
        "email domain is not supported",
        "unsupported email",
        "email not supported",
        "email is unsupported",
        "email isn't supported",
    ))
    return (
        unsupported_email
        or "AccountUnusableError" in text
        or "account_deactivated" in text
        or "account_deleted" in text
        or "account_banned" in text
        or _is_final_session_access_token_timeout(text)
        or "邮箱提交后进入登录密码页" in text
        or "auth.openai.com/log-in/password" in text
        or "/log-in/password" in text
    )


def _disable_job_email(email: str | None, reason: str) -> bool:
    """把本次任务邮箱停用，避免后续再次领取。"""
    if not email:
        return False
    try:
        from core.email_provider import release_email

        source = release_email(email, status="disabled", note=f"自动停用: {reason[:180]}")
        logger.warning("[Service] 已自动停用邮箱: source=%s email=%s reason=%s", source, email, reason[:220])
        return True
    except Exception:
        logger.exception("[Service] 自动停用邮箱失败: %s", email)
        return False


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_WORKERS
    return max(_MIN_MAX_WORKERS, min(_MAX_MAX_WORKERS, value))


def effective_registration_workers(max_workers: int | None) -> int:
    """Serialize registration when system-wide NordVPN rotation is enabled."""
    requested = _normalize_workers(max_workers)
    from config import nordvpn as _nordvpn_cfg

    try:
        rotate_interval = int(
            getattr(_nordvpn_cfg, "NORDVPN_AUTO_ROTATE_INTERVAL", 0) or 0
        )
    except (TypeError, ValueError):
        rotate_interval = 0
    rotation_enabled = (
        bool(getattr(_nordvpn_cfg, "NORDVPN_ENABLED", False))
        and bool(getattr(_nordvpn_cfg, "NORDVPN_AUTO_ROTATE_ENABLED", False))
        and rotate_interval > 0
    )
    from core.nordvpn_wireguard import is_per_profile_proxy_enabled

    if rotation_enabled and is_per_profile_proxy_enabled():
        rotation_enabled = False
    if rotation_enabled:
        if requested != 1:
            logger.info(
                "[Service] NordVPN auto-rotation 使用系统全局 IP，workers 从 %s 调整为 1",
                requested,
            )
        return 1
    return requested


def qan8_batch_status(batch_id: str) -> dict:
    """Return non-secret operational counters for a QAN8 registration batch."""
    from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator

    return Qan8GmailApiAllocator().status(str(batch_id or "").strip())


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回注册线程池。

    旧逻辑只在首次创建线程池时使用 max_workers，后续 WebUI 改线程数再提交仍会复用
    上一次的池。这里改成：每次传入的 max_workers 和当前池不一致时，立即创建新池供
    新提交任务使用；旧池不接收新任务，但会继续把已经排队/运行的任务跑完。
    """
    global _executor, _executor_workers, _executor_generation
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _executor_workers
    with _executor_lock:
        if _executor is None or requested_workers != _executor_workers:
            old_executor = _executor
            if old_executor is not None:
                # 不取消旧池里已提交的任务，只是不再往旧池追加新任务。
                old_executor.shutdown(wait=False, cancel_futures=False)
                _retired_executors.append(old_executor)
                logger.info(
                    "[Service] 注册线程池 workers 从 %s 切换为 %s；旧池继续处理已排队任务",
                    _executor_workers,
                    requested_workers,
                )
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
    return _executor


def get_executor_workers() -> int:
    """当前新提交注册任务会使用的线程数。"""
    with _executor_lock:
        return _executor_workers


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for ex in executors:
        ex.shutdown(wait=wait, cancel_futures=False)


def nordvpn_rotation_status() -> dict:
    """Return the current automatic-rotation and registration-gate state."""
    from core.nordvpn_cli import rotation_status_detail

    barrier = _MAINTENANCE_BARRIER.status()
    detail = rotation_status_detail()
    return {
        "rotation_pending": bool(_rotation_pending),
        "rotation_in_progress": _ROTATION_LOCK.locked(),
        "rotation_error": detail.get("error"),
        "rotation_detail": detail.get("detail"),
        "gate_state": barrier["state"],
        "waiting_jobs": barrier["waiting_count"],
        "active_jobs": barrier["active_count"],
        "last_outcome": barrier["last_outcome"],
    }


def retry_pending_nordvpn_rotation() -> dict:
    """Retry a failed automatic rotation and reopen the registration gate on success."""
    global _rotation_pending
    if not _rotation_pending:
        return {"ok": False, "error": "当前没有待重试的 NordVPN 自动轮换", "status": 409}
    if not _ROTATION_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "NordVPN 自动轮换正在执行，请稍候", "status": 409}
    try:
        barrier_status = _MAINTENANCE_BARRIER.status()
        if barrier_status["active_count"]:
            return {"ok": False, "error": "仍有注册任务运行，暂不能重试 NordVPN 轮换", "status": 409}
        from core.nordvpn_cli import execute_rotation, rotation_status_detail
        if not _MAINTENANCE_BARRIER.retry_rotation(execute_rotation):
            reason = (
                rotation_status_detail().get("error")
                or "rotation callback returned false without a diagnostic reason"
            )
            return {
                "ok": False,
                "error": f"NordVPN 轮换失败：{reason}；注册队列继续保持暂停",
                "status": 503,
            }
        _rotation_pending = False
        return {"ok": True, "message": "NordVPN 轮换成功，注册队列已恢复"}
    except Exception as exc:
        logger.exception("[Service] retry NordVPN rotation 失败")
        return {
            "ok": False,
            "error": f"NordVPN 轮换重试异常：{type(exc).__name__}: {exc}",
            "status": 500,
        }
    finally:
        _ROTATION_LOCK.release()


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)

        # Ensure root logger is configured and at INFO level
        root_logger = logging.getLogger()
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)

        root_logger.addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.flush()
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _registration_auto_retry_limit() -> int:
    """Read the live retry limit while keeping a hard operational bound."""
    from config import register as _register_cfg

    try:
        configured = int(getattr(_register_cfg, "REGISTRATION_AUTO_RETRY_ATTEMPTS", 1) or 0)
    except (TypeError, ValueError):
        configured = 1
    return min(3, max(0, configured))


def _queue_transient_registration_retry(job_id: int, error: object) -> dict | None:
    """Queue a fresh registration job only after a terminal failed job is durable."""
    if is_stop_requested(job_id):
        return None
    source = db.get_job(job_id)
    if source is None or source.get("status") != "failed" or _account_for_job(source):
        return None

    from core.registration_retry_policy import should_auto_retry_registration_failure

    if not should_auto_retry_registration_failure(
        error,
        retry_attempt=int(source.get("retry_attempt") or 0),
        max_attempts=_registration_auto_retry_limit(),
    ):
        return None

    result = retry_job(job_id)
    if result.get("ok"):
        next_job = (result.get("job") or {}).get("id")
        logger.warning(
            "[Job %s] 临时注册失败，已创建新任务 #%s: %s",
            job_id,
            next_job,
            str(error)[:180],
        )
    else:
        logger.warning(
            "[Job %s] 临时注册失败，但自动重试未创建: %s",
            job_id,
            str(result.get("error") or "unknown")[:180],
        )
    return result


def _registration_cleanup_allows_retry(job_id: int, cleanup_succeeded: bool) -> bool:
    """Confirm the old QAN8 assignment is terminal before creating a new job."""
    if cleanup_succeeded:
        return True
    source = db.get_job(job_id) or {}
    if str(source.get("email_source") or "").strip().lower() != "qan8_gmail_api":
        return False
    try:
        from core.qan8_gmail_api_store import Qan8GmailApiStore

        assignment = Qan8GmailApiStore().get_assignment(job_id)
    except Exception:
        logger.exception("[Job %s] 无法确认 QAN8 assignment 是否已终态", job_id)
        return False
    return assignment is None or str(assignment.get("state") or "").lower() != "active"


def _run_one_job(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)
    if not _activate_job(job_id):
        log_logger.info(f"[Job {job_id}] 被维护屏障阻止，跳过执行")
        _notify_sub2api_automation_job(job_id)
        return


    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.get_job(job_id)
    if not current:
        log_logger.info(f"[Job {job_id}] 任务记录已删除，跳过执行")
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)
        return
    if current.get("status") == "cancelled":
        log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)
        return
    if current.get("job_type") == "local_test":
        db.update_job(
            job_id,
            status="cancelled",
            error="本地测试功能已移除",
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
        log_logger.info(f"[Job {job_id}] 已取消已移除的本地测试任务")
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=_local_now().isoformat(timespec="seconds"))

    email: str | None = None
    try:
        with _JobLogContext(log_file):
            from main import run_registration
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            email, name, birthday = _prepare_registration_args(job_id=job_id)
            db.update_job(job_id, email=email)
            check_stop_requested()
            provider_context = current.get("provider_context") or {}
            proxy_lane_id = provider_context.get("proxy_lane_id")
            result = run_registration(
                email=email,
                name=name,
                birthday=birthday,
                proxy_lane_id=proxy_lane_id,
                lease_owner_id=f"registration-job:{job_id}",
            )
            if is_stop_requested(job_id):
                result_dict = result if isinstance(result, dict) else {}
                recoverable_twofa = bool(
                    result_dict.get("account_id")
                    and str(result_dict.get("twofa_status") or "").lower() in {"pending", "failed"}
                )
                if recoverable_twofa:
                    _consume_recoverable_twofa_assignment(
                        email,
                        str(result_dict.get("error") or "用户手动停止"),
                    )
                else:
                    _release_unconsumed_job_email(email, "用户手动停止")
                db.update_job(
                    job_id,
                    status="stopped",
                    email=result_dict.get("email") or email,
                    account_id=result_dict.get("account_id") if recoverable_twofa else None,
                    error="用户手动停止",
                    completed_at=_local_now().isoformat(timespec="seconds"),
                )
                log_logger.warning(f"[Job {job_id}] 已按用户请求停止")
                return
            if isinstance(result, dict) and result.get("success"):
                try:
                    from core.email_provider import (
                        mark_email_consumed,
                        resolve_email_source,
                    )

                    if resolve_email_source(email or "") == "qan8_gmail_api":
                        mark_email_consumed(email or "")
                except Exception:
                    logger.exception("[Job %s] consume QAN8 alias after success failed", job_id)
                db.update_job(
                    job_id,
                    status="success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    network_identity=result.get("network_identity"),
                    completed_at=_local_now().isoformat(timespec="seconds"),
                )
                log_logger.info(f"[Job {job_id}] 成功: {result.get('email')}")
                # NordVPN auto IP rotation counter
                try:
                    from core.nordvpn_cli import notify_registration_success
                    if notify_registration_success():
                        global _rotation_pending
                        _rotation_pending = True
                        log_logger.info(
                            "[Job %d] 已标记 rotation pending；当前任务退出后关闭新任务 gate",
                            job_id,
                        )
                except Exception:
                    log_logger.debug(
                        "[Job %d] NordVPN 轮换检查跳过",
                        job_id, exc_info=True,
                    )
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                result_email = (result or {}).get("email") if isinstance(result, dict) else None
                db.update_job(
                    job_id,
                    status="failed",
                    email=result_email,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    network_identity=(result or {}).get("network_identity") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=_local_now().isoformat(timespec="seconds"),
                )
                email_to_handle = str(result_email or email or "").strip()
                recoverable_twofa = bool(
                    (result or {}).get("account_id")
                    and str((result or {}).get("twofa_status") or "").lower() in {"pending", "failed"}
                ) if isinstance(result, dict) else False
                alias_discarded = False
                if not recoverable_twofa and _should_disable_failed_registration_email(err):
                    _disable_job_email(email_to_handle, str(err))
                elif not recoverable_twofa:
                    alias_discarded = _release_unconsumed_job_email(
                        email_to_handle,
                        str(err),
                        discard_on_failure=True,
                    )
                elif email_to_handle:
                    _consume_recoverable_twofa_assignment(email_to_handle, str(err))
                    log_logger.info(
                        "[Job %s] 账号已持久化为 2FA 可恢复状态，已消费本次 alias 并释放 lane: account_id=%s",
                        job_id,
                        (result or {}).get("account_id"),
                    )
                if (
                    not recoverable_twofa
                    and _registration_cleanup_allows_retry(job_id, alias_discarded)
                ):
                    _queue_transient_registration_retry(job_id, err)
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except StopRequested as exc:
        linked_account = _account_for_job(db.get_job(job_id) or {})
        recoverable_twofa = bool(
            linked_account
            and str(linked_account.get("twofa_status") or "").lower() in {"pending", "failed"}
        )
        if not recoverable_twofa:
            _release_unconsumed_job_email(email, str(exc))
        elif email:
            _consume_recoverable_twofa_assignment(email, str(exc))
        log_logger.warning(f"[Job {job_id}] 已停止: {exc}")
        db.update_job(
            job_id,
            status="stopped",
            email=email,
            account_id=(linked_account or {}).get("id") if recoverable_twofa else None,
            error="用户手动停止",
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        linked_account = _account_for_job(db.get_job(job_id) or {})
        recoverable_twofa = bool(
            linked_account
            and str(linked_account.get("twofa_status") or "").lower() in {"pending", "failed"}
        )
        alias_discarded = False
        if not recoverable_twofa and _should_disable_failed_registration_email(err_text):
            _disable_job_email(email, err_text)
        elif not recoverable_twofa:
            alias_discarded = _release_unconsumed_job_email(
                email,
                err_text,
                discard_on_failure=not is_stop_requested(job_id),
            )
        elif email:
            _consume_recoverable_twofa_assignment(email, err_text)
        if is_stop_requested(job_id):
            log_logger.warning(f"[Job {job_id}] 停止中捕获异常，按停止处理: {type(exc).__name__}: {exc}")
            db.update_job(
                job_id,
                status="stopped",
                email=email,
                account_id=(linked_account or {}).get("id") if recoverable_twofa else None,
                error="用户手动停止",
                completed_at=_local_now().isoformat(timespec="seconds"),
            )
            return
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            email=email,
            account_id=(linked_account or {}).get("id") if recoverable_twofa else None,
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
        if (
            not recoverable_twofa
            and _registration_cleanup_allows_retry(job_id, alias_discarded)
        ):
            _queue_transient_registration_retry(job_id, err_text)
    finally:
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)


def _notify_sub2api_automation_job(job_id: int) -> None:
    """Notify sub2api after an automation-owned job reaches a terminal state."""
    try:
        from core.sub2api_automation import notify_job_completion

        notify_job_completion(job_id)
    except Exception:
        logger.exception("[Job %s] sub2api automation callback failed", job_id)


def _run_twofa_retry_job(
    job_id: int,
    log_file: str,
    email: str,
    account_id: int,
    proxy_lane_id: int | None = None,
) -> None:
    """重新登录已有账号并补做 2FA，不进入完整注册流程。"""
    if not _activate_job(job_id):
        return
    current = db.get_job(job_id)
    if not current or current.get("status") == "cancelled":
        _deactivate_job(job_id)
        return
    db.update_job(job_id, status="running", started_at=_local_now().isoformat(timespec="seconds"))
    try:
        account = db.get_account(account_id)
        if not account:
            raise RuntimeError("目标账号不存在，无法补做 2FA")
        with _JobLogContext(log_file):
            result = _run_configured_twofa_retry(account, proxy_lane_id=proxy_lane_id)
        now_iso = _local_now().isoformat(timespec="seconds")
        if is_stop_requested(job_id):
            db.update_job(
                job_id,
                status="stopped",
                email=email,
                account_id=account_id,
                error="用户手动停止",
                completed_at=now_iso,
            )
        elif result.get("ok"):
            db.update_job(
                job_id,
                status="success",
                email=email,
                account_id=account_id,
                completed_at=now_iso,
            )
        else:
            db.update_job(
                job_id,
                status="failed",
                email=email,
                account_id=account_id,
                error=str(result.get("message") or "2FA 补做失败")[:500],
                completed_at=now_iso,
            )
    except Exception as exc:
        db.update_job(
            job_id,
            status="failed",
            email=email,
            account_id=account_id,
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
        logger.exception("[Job %s] 2FA 补做异常", job_id)
    finally:
        _deactivate_job(job_id)


def _run_codex_retry_job(
    job_id: int,
    log_file: str,
    email: str,
    account_id: int,
    proxy_lane_id: int | None = None,
) -> None:
    """把 Codex 补跑作为标准任务执行，并复用任务状态、日志和停止入口。"""
    if not _activate_job(job_id):
        codex_retry_service.release(email)
        _notify_sub2api_automation_job(job_id)
        return
    current = db.get_job(job_id)
    if not current or current.get("status") == "cancelled":
        codex_retry_service.release(email)
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)
        return

    provider_context = current.get("provider_context") or {}
    trigger = str(provider_context.get("trigger") or "").strip()
    if trigger == "registration_auto_free":
        from core.registration_auto_codex import (
            account_registration_driver,
            registration_driver_uses_live_browser,
        )

        account = db.get_account(account_id) or {}
        registration_driver = str(
            provider_context.get("registration_driver")
            or account_registration_driver(account)
        ).strip().lower()
        if registration_driver_uses_live_browser(registration_driver):
            reason = (
                f"旧的 registration_auto_free 自动 Codex OAuth 任务来自 {registration_driver} 注册，"
                "没有注册 browser 可复用，已跳过，禁止另起浏览器；"
                "新注册必须由 registration worker 同步执行"
            )
            db.update_account_codex_status(email, "skipped", reason)
            db.update_job(
                job_id,
                status="cancelled",
                email=email,
                account_id=account_id,
                error=reason,
                completed_at=_local_now().isoformat(timespec="seconds"),
            )
            logger.warning("[Job %s] %s: %s", job_id, reason, email)
            codex_retry_service.release(email)
            _deactivate_job(job_id)
            _notify_sub2api_automation_job(job_id)
            return

    db.update_job(job_id, status="running", started_at=_local_now().isoformat(timespec="seconds"))
    try:
        sub2_callback_context = None
        if provider_context.get("sub2api_automation_kind") == "reauthorization":
            sub2_callback_context = {
                "path": provider_context.get("sub2api_callback_path"),
                "request_id": provider_context.get("sub2api_automation_request_id"),
                "event_id": provider_context.get("sub2api_callback_event_id"),
                "account_id": provider_context.get("sub2api_account_id"),
                "email": provider_context.get("sub2api_automation_email"),
            }
        result = codex_retry_service.run_worker(
            email,
            clear_log=False,
            target_log_path=log_file,
            proxy_lane_id=proxy_lane_id,
            lease_owner_id=f"codex-retry-job:{job_id}",
            sub2_callback_context=sub2_callback_context,
        )
        now_iso = _local_now().isoformat(timespec="seconds")
        if is_stop_requested(job_id) or result.get("status") == "stopped":
            db.update_job(job_id, status="stopped", email=email, account_id=account_id, error=str(result.get("message") or "用户手动停止")[:500], completed_at=now_iso)
        elif result.get("ok"):
            db.update_job(
                job_id,
                status="success",
                email=email,
                account_id=account_id,
                completed_at=now_iso,
            )
        else:
            db.update_job(
                job_id,
                status="failed",
                email=email,
                account_id=account_id,
                error=str(result.get("message") or "Codex 补跑失败")[:500],
                completed_at=now_iso,
            )
    except Exception as exc:
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
        codex_retry_service.release(email)
        logger.exception("[Job %s] Codex 补跑异常", job_id)
    finally:
        _deactivate_job(job_id)
        _notify_sub2api_automation_job(job_id)


def submit_codex_retry_for_account(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    workers: int | None = None,
    registration_driver: str | None = None,
    automation_context: dict | None = None,
) -> dict:
    """Create and queue a standalone Codex retry for an existing account."""
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email or not str(access_token or "").strip():
        return {"accepted": False, "busy": False, "error": "账号缺少 email 或 access_token"}
    account = db.get_account(account_id) or {}
    current_status = str(account.get("codex_status") or "").strip().lower()
    automation_reauthorization = (
        isinstance(automation_context, dict)
        and automation_context.get("sub2api_automation_kind") == "reauthorization"
    )
    if current_status == "success" and not automation_reauthorization:
        return {"accepted": False, "busy": False, "reason": "already_success"}
    if current_status == "deactivated" and not automation_reauthorization:
        return {"accepted": False, "busy": False, "reason": "deactivated"}
    if not codex_retry_service.reserve(email):
        return {"accepted": False, "busy": True, "error": "该账号正在补跑 Codex，请稍候"}

    provider_context = {
        "trigger": str(trigger or "manual"),
        **(
            {"registration_driver": str(registration_driver).strip().lower()}
            if str(registration_driver or "").strip()
            else {}
        ),
    }
    if isinstance(automation_context, dict):
        for key in (
            "sub2api_automation_request_id",
            "sub2api_automation_kind",
            "sub2api_callback_url",
            "sub2api_callback_path",
            "sub2api_callback_event_id",
            "sub2api_account_id",
            "sub2api_automation_email",
        ):
            value = str(automation_context.get(key) or "").strip()
            if value:
                provider_context[key] = value

    job = None
    try:
        job = db.create_job(
            email_source=str(account.get("email_source") or "unknown"),
            job_type="codex_retry",
            email=email,
            account_id=account_id,
            provider_context=provider_context,
        )
        db.update_account_codex_status(email, "retrying", None)
        effective_workers = workers if workers is not None else get_executor_workers()
        proxy_lane_id = (int(job["id"]) - 1) % max(1, int(effective_workers))
        from core.rotating_proxy_runtime import (
            CODEX_RETRY_PROXY_SCOPE,
            prepare_rotating_proxy_lanes,
        )

        prepare_rotating_proxy_lanes(1, scope=CODEX_RETRY_PROXY_SCOPE)
        with _executor_lock:
            executor = get_executor(max_workers=effective_workers)
            executor.submit(
                _run_codex_retry_job,
                job["id"],
                job["log_file"],
                email,
                account_id,
                proxy_lane_id,
            )
    except Exception as exc:
        codex_retry_service.release(email)
        if job is not None:
            db.update_job(
                int(job["id"]),
                status="failed",
                error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
                completed_at=_local_now().isoformat(timespec="seconds"),
            )
        db.update_account_codex_status(email, "failed", f"队列提交失败：{type(exc).__name__}: {exc}"[:500])
        logger.exception("[Service] 自动 Codex 补跑任务提交失败: %s", email)
        return {"accepted": False, "busy": False, "error": f"队列提交失败：{type(exc).__name__}: {exc}"[:500]}

    return {
        "accepted": True,
        "busy": False,
        "job_id": int(job["id"]),
        "account_id": account_id,
        "email": email,
        "trigger": str(trigger or "manual"),
    }


# ============================================================
# 公共接口
# ============================================================

def submit_registration(
    count: int = 1,
    email_source: str | None = None,
    workers: int | None = None,
    gmail_cdks: list[str] | None = None,
    paymesh_cdks: list[str] | None = None,
    gmail_routed_domains: list[str] | None = None,
    gmail_batch_id: str | None = None,
    paymesh_routed_domains: list[str] | None = None,
    gmail_api_url_aliases_per_email: int | None = None,
    qan8_aliases_per_source: int | None = None,
    automation_context: dict | None = None,
) -> list[dict]:
    """
    创建 N 个注册任务并提交到线程池。
    email_source 仅记录到 DB；实际邮箱来源固定为 Outlook 账号池。

    Returns:
        N 个新创建的 job dict
    """
    if email_source is None:
        from config import email as _email_cfg
        email_source = _email_cfg.EMAIL_SOURCE
    if gmail_cdks and not gmail_batch_id:
        from core.gmail_123452026_client import create_registration_batch

        gmail_batch_id = create_registration_batch(
            gmail_cdks,
            routed_domains=gmail_routed_domains or [],
        )

    # Gmail API URL multi-alias batch: 1 email gốc → nhiều alias share code_url.
    # Chỉ kích hoạt khi có nhiều alias mỗi email (aliases_per_email > 1);
    # nếu = 1 thì mỗi job claim 1 email gốc như luồng thường (không cần batch).
    gmail_api_url_batch_id: str | None = None
    aliases_per_email = int(gmail_api_url_aliases_per_email or 1)
    if email_source == "gmail_api_url" and aliases_per_email > 1:
        from core.gmail_api_url_client import (
            create_registration_batch as create_gmail_api_url_batch,
        )

        gmail_api_url_batch_id = create_gmail_api_url_batch(
            count, aliases_per_email=aliases_per_email
        )
        logger.info(
            "Đã tạo Gmail API URL batch %s: %d alias, mỗi email ≤%d",
            gmail_api_url_batch_id, count, aliases_per_email,
        )
    paymesh_assignments: list[str] = []
    if paymesh_cdks:
        from config import email as _email_cfg
        from core.paymesh_batch_assignment import create_paymesh_job_assignments

        paymesh_assignments = create_paymesh_job_assignments(
            paymesh_cdks,
            count=count,
            configured_limit=int(
                getattr(_email_cfg, "PAYMESH_ACCOUNTS_PER_CDK", 6) or 6
            ),
        )
    provider_context = {}
    if gmail_batch_id:
        provider_context = {
            "gmail_batch_id": gmail_batch_id,
            "gmail_routed_domains": list(gmail_routed_domains or []),
        }
    if gmail_api_url_batch_id:
        provider_context["gmail_api_url_batch_id"] = gmail_api_url_batch_id
    if paymesh_routed_domains:
        provider_context["paymesh_routed_domains"] = list(paymesh_routed_domains)
    if isinstance(automation_context, dict):
        for key in (
            "sub2api_automation_request_id",
            "sub2api_automation_kind",
            "sub2api_callback_url",
        ):
            value = str(automation_context.get(key) or "").strip()
            if value:
                provider_context[key] = value
    if provider_context.get("sub2api_automation_kind") == "registration":
        provider_context["sub2api_automation_requested_count"] = int(count)
    from config import proxy as _proxy_cfg
    rotating_proxy_enabled = bool(
        getattr(_proxy_cfg, "ROTATING_PROXY_ENABLED", False)
    )

    effective_workers = effective_registration_workers(workers)
    if rotating_proxy_enabled:
        from core.rotating_proxy_runtime import (
            REGISTRATION_PROXY_SCOPE,
            prepare_rotating_proxy_lanes,
        )

        prepare_rotating_proxy_lanes(
            min(effective_workers, count),
            scope=REGISTRATION_PROXY_SCOPE,
        )
    # 创建/切换线程池和提交本批任务必须整体串行化：否则另一请求在本批提交中途
    # 切换 workers 并 shutdown 旧池，会导致后续 submit 报 cannot schedule new futures after shutdown。
    with _executor_lock:
        executor = get_executor(max_workers=effective_workers)
        effective_workers = get_executor_workers()
        qan8_batch_id: str | None = None
        from config import email as _email_cfg
        qan8_aliases = int(
            qan8_aliases_per_source
            if qan8_aliases_per_source is not None
            else getattr(_email_cfg, "QAN8_ALIASES_PER_SOURCE", 12)
        )
        if email_source == "qan8_gmail_api":
            if not 1 <= qan8_aliases <= 12:
                raise ValueError("qan8_aliases_per_source must be between 1 and 12")
            from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator

            qan8_batch = Qan8GmailApiAllocator().create_batch(
                count,
                requested_workers=effective_workers,
                aliases_per_source=qan8_aliases,
            )
            qan8_batch_id = str(qan8_batch["batch_id"])
            provider_context["qan8_gmail_api_batch_id"] = qan8_batch_id
            provider_context["qan8_aliases_per_source"] = qan8_aliases
        jobs = []
        for index in range(count):
            job_provider_context = dict(provider_context)
            if rotating_proxy_enabled:
                job_provider_context["proxy_lane_id"] = index % effective_workers
            qan8_lane_id = None
            if qan8_batch_id:
                qan8_lane_id = index % effective_workers
                job_provider_context["qan8_gmail_api_lane_id"] = qan8_lane_id
            paymesh_inventory_id = (
                paymesh_assignments[index] if paymesh_assignments else None
            )
            if paymesh_inventory_id:
                job_provider_context["paymesh_inventory_id"] = paymesh_inventory_id
            job = db.create_job(
                email_source=email_source,
                provider_context=job_provider_context,
            )
            _set_job_email_inputs(
                int(job["id"]),
                gmail_cdks,
                paymesh_cdks=(None if paymesh_inventory_id else paymesh_cdks),
                gmail_routed_domains=gmail_routed_domains,
                gmail_batch_id=gmail_batch_id,
                gmail_api_url_batch_id=gmail_api_url_batch_id,
                qan8_gmail_api_batch_id=qan8_batch_id,
                qan8_gmail_api_lane_id=qan8_lane_id,
                email_source=email_source,
                paymesh_inventory_id=paymesh_inventory_id,
                paymesh_routed_domains=paymesh_routed_domains,
            )
            try:
                executor.submit(_run_one_job, job["id"], job["log_file"])
            except Exception as exc:
                _clear_job_email_inputs(int(job["id"]))
                db.update_job(
                    int(job["id"]),
                    status="failed",
                    error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
                    completed_at=_local_now().isoformat(timespec="seconds"),
                )
                logger.exception("[Service] 注册任务 #%s 提交线程池失败", job["id"])
            jobs.append(db.get_job(int(job["id"])) or job)
    logger.info(f"[Service] 已提交 {count} 个注册任务，源={email_source}，workers={effective_workers}")
    return jobs


def _account_for_job(job: dict) -> dict | None:
    account_id = job.get("account_id")
    if account_id is not None:
        try:
            account = db.get_account(int(account_id))
            if account is not None:
                return account
        except (TypeError, ValueError):
            pass
    email = str(job.get("email") or "").strip()
    return db.get_account_by_email(email) if email else None


def _configured_twofa_retry_driver() -> str:
    """Return the browser/provider selected in the live WebUI settings."""
    from config import roxybrowser as _driver_cfg

    raw = str(getattr(_driver_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    aliases = {
        "roxybrowser": "roxy",
        "fingerprint": "roxy",
        "browser": "roxy",
        "cloakbrowser": "cloak",
        "browseruse": "browser_use",
        "browser-use": "browser_use",
        "bu": "browser_use",
        "sv": "skyvern",
        "api": "protocol",
        "http": "protocol",
    }
    return aliases.get(raw, raw)


def _run_configured_twofa_retry(
    account: dict,
    *,
    proxy_lane_id: int | None = None,
) -> dict:
    """Run the shared reactive 2FA flow for a supported browser provider."""
    driver = _configured_twofa_retry_driver()
    if driver in _SUPPORTED_BROWSER_TWOFA_DRIVERS:
        from core.browser_twofa_retry import run_twofa_retry

        if proxy_lane_id is None:
            return run_twofa_retry(account)
        return run_twofa_retry(
            account,
            proxy_lane_id=proxy_lane_id,
            lease_owner_id=f"twofa-retry-job:{account.get('id')}",
        )
    return {
        "ok": False,
        "status": "failed",
        "message": (
            f"当前注册驱动 {driver!r} 暂不支持账号列表 reactive 2FA；"
            "请切换 Settings → 注册驱动 为受支持的浏览器，或使用该驱动对应的登录流程"
        ),
    }


def _account_supports_twofa_retry(account: dict) -> bool:
    """判断账号是否具备当前设置的 provider 所需的登录密码。"""
    if not str(account.get("registration_password") or "").strip():
        return False
    return _configured_twofa_retry_driver() in _SUPPORTED_BROWSER_TWOFA_DRIVERS


def get_retry_info(job: dict) -> dict:
    """返回给 API/UI 的重试能力描述，不依赖前端猜测错误阶段。"""
    status = str(job.get("status") or "")
    info = {
        "retryable": False,
        "retry_action": None,
        "retry_label": None,
        "retry_reason": None,
        "display_status": status,
    }
    if status not in ("failed", "stopped", "cancelled"):
        return info
    successful_retry = db.get_successful_retry_for_job(int(job.get("id") or 0))
    if successful_retry is not None:
        info["retry_reason"] = f"后续重试任务 #{successful_retry.get('id')} 已成功"
        info["successful_retry_job_id"] = successful_retry.get("id")
        return info

    account = _account_for_job(job)
    if account and job.get("account_id") is not None and status in ("failed", "stopped"):
        info["display_status"] = "success" if (account.get("codex_status") or "") == "success" else "partial_success"

    if account:
        twofa_status = str(account.get("twofa_status") or "").strip().lower()
        if twofa_status in {"pending", "failed"}:
            if not _account_supports_twofa_retry(account):
                info["retry_reason"] = "账号缺少当前注册驱动所需的 OpenAI 密码，或该驱动不支持 reactive 2FA，不能自动补做 2FA"
                return info
            info.update({
                "retryable": True,
                "retry_action": "2fa",
                "retry_label": "重新登录并补做 2FA",
                "retry_reason": account.get("twofa_error") or "账号已创建但 2FA 尚未完成",
            })
            return info
        codex_status = str(account.get("codex_status") or "")
        if codex_status == "deactivated":
            info["retry_reason"] = f"{account_unusable_message('account_deactivated')}, không thể chạy bù Codex"
            return info
        if codex_status == "success":
            info["retry_reason"] = "账号和 Codex 授权均已完成"
            return info
        info.update({
            "retryable": True,
            "retry_action": "codex",
            "retry_label": "补跑 Codex",
        })
        return info

    info.update({
        "retryable": True,
        "retry_action": "registration",
        "retry_label": "重试",
    })
    return info


def retry_job(
    job_id: int,
    workers: int | None = None,
    *,
    allow_success_twofa: bool = False,
) -> dict:
    """智能重试终态任务：未生成账号则重新注册，已有账号则仅补跑 Codex。"""
    source = db.get_job(job_id)
    if source is None:
        return {"ok": False, "error": "任务不存在", "status": 404}

    retry_info = get_retry_info(source)
    if allow_success_twofa and source.get("status") == "success":
        account_for_twofa = _account_for_job(source)
        twofa_status = str((account_for_twofa or {}).get("twofa_status") or "").strip().lower()
        if twofa_status in {"disabled", "pending", "failed"}:
            if not account_for_twofa or not _account_supports_twofa_retry(account_for_twofa):
                retry_info = {
                    "retryable": False,
                    "retry_action": "2fa",
                    "retry_reason": "账号缺少当前注册驱动所需的 OpenAI 密码，或该驱动不支持 reactive 2FA，不能自动补做 2FA",
                }
            else:
                retry_info = {
                    "retryable": True,
                    "retry_action": "2fa",
                    "retry_reason": account_for_twofa.get("twofa_error") or "账号尚未完成 2FA",
                }
    if not retry_info["retryable"]:
        reason = retry_info.get("retry_reason") or f"当前状态不支持重试：{source.get('status')}"
        return {"ok": False, "error": reason, "status": 409}

    action = str(retry_info["retry_action"])
    account = _account_for_job(source)
    email = str((account or {}).get("email") or source.get("email") or "").strip()
    account_id = int(account["id"]) if account and account.get("id") is not None else None
    reserved_codex = False
    if action in {"codex", "2fa"} and (not email or account_id is None):
        return {"ok": False, "error": "已注册账号信息不完整，无法执行账号恢复操作", "status": 409}
    if action == "codex":
        if not codex_retry_service.reserve(email):
            return {"ok": False, "error": "该账号正在补跑 Codex，请稍候", "status": 409}
        reserved_codex = True

    try:
        job, created = db.create_retry_job(
            int(job_id),
            job_type=("codex_retry" if action == "codex" else "twofa_retry" if action == "2fa" else "registration"),
            email_source=str(source.get("email_source") or "outlook"),
            email=email if action in {"codex", "2fa"} else None,
            account_id=account_id if action in {"codex", "2fa"} else None,
            allow_success_for_twofa=allow_success_twofa and action == "2fa",
        )
    except LookupError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 404}
    except ValueError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 409}

    if not created:
        if reserved_codex:
            codex_retry_service.release(email)
        return {
            "ok": True,
            "created": False,
            "reused": True,
            "message": f"已有重试任务 #{job['id']} 在排队或运行中",
            "source_job_id": int(job_id),
            "retry_action": action,
            "job": job,
        }

    try:
        if action == "codex":
            db.update_account_codex_status(email, "retrying", None)
        effective_workers = (
            workers if action in {"codex", "2fa"} else effective_registration_workers(workers)
        )
        proxy_lane_id = (int(job["id"]) - 1) % max(1, int(effective_workers))
        from core.rotating_proxy_runtime import (
            CODEX_RETRY_PROXY_SCOPE,
            REGISTRATION_PROXY_SCOPE,
            TWOFA_RETRY_PROXY_SCOPE,
            prepare_rotating_proxy_lanes,
        )

        retry_scope = {
            "codex": CODEX_RETRY_PROXY_SCOPE,
            "2fa": TWOFA_RETRY_PROXY_SCOPE,
        }.get(action, REGISTRATION_PROXY_SCOPE)
        prepare_rotating_proxy_lanes(1, scope=retry_scope)
        with _executor_lock:
            executor = get_executor(max_workers=effective_workers)
            if action == "codex":
                executor.submit(
                    _run_codex_retry_job,
                    job["id"],
                    job["log_file"],
                    email,
                    int(account_id),
                    proxy_lane_id,
                )
            elif action == "2fa":
                executor.submit(
                    _run_twofa_retry_job,
                    job["id"],
                    job["log_file"],
                    email,
                    int(account_id),
                    proxy_lane_id,
                )
            else:
                executor.submit(_run_one_job, job["id"], job["log_file"])
    except Exception as exc:
        if reserved_codex:
            codex_retry_service.release(email)
            db.update_account_codex_status(email, "failed", f"队列提交失败：{type(exc).__name__}: {exc}"[:500])
        db.update_job(
            int(job["id"]),
            status="failed",
            error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
            completed_at=_local_now().isoformat(timespec="seconds"),
        )
        logger.exception("[Service] 重试任务 #%s 提交线程池失败", job["id"])
        return {"ok": False, "error": "重试任务创建成功，但提交执行失败", "status": 500, "job": db.get_job(int(job["id"]))}

    return {
        "ok": True,
        "created": True,
        "reused": False,
        "message": f"已创建重试任务 #{job['id']}（{'Codex 补跑' if action == 'codex' else '重新登录并补做 2FA' if action == '2fa' else '完整注册'}）",
        "source_job_id": int(job_id),
        "retry_action": action,
        "job": job,
    }


def retry_account_twofa(account_id: int, workers: int | None = None) -> dict:
    """从账号列表直接触发已有账号的 2FA 补做动作。"""
    try:
        account = db.get_account(int(account_id))
    except (TypeError, ValueError):
        account = None
    if account is None:
        return {"ok": False, "error": "账号不存在", "status": 404}
    status = str(account.get("twofa_status") or ("active" if account.get("totp_secret") else "disabled")).strip().lower()
    if status == "active":
        return {"ok": False, "error": "该账号的 2FA 已启用", "status": 409}
    if not _account_supports_twofa_retry(account):
        return {
            "ok": False,
            "error": (
                "账号缺少当前注册驱动所需的 OpenAI 登录密码，"
                "或当前 Settings 注册驱动不支持 reactive 2FA，不能自动补做 2FA"
            ),
            "status": 409,
        }
    source = db.get_latest_job_for_account(int(account_id)) or db.get_latest_job_for_email(account.get("email"))
    if source is None:
        return {"ok": False, "error": "找不到该账号对应的注册任务，无法执行 2FA 重试", "status": 409}
    return retry_job(int(source["id"]), workers=workers, allow_success_twofa=True)


def retry_accounts_twofa(account_ids: list[object], workers: int | None = None) -> dict:
    """从账号列表批量触发 2FA 补做，并逐项返回跳过原因。"""
    started = []
    reused = []
    skipped = []
    seen: set[int] = set()
    for raw_account_id in account_ids:
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError):
            skipped.append({"account_id": raw_account_id, "reason": "账号 ID 非法"})
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        result = retry_account_twofa(account_id, workers=workers)
        if not result.get("ok"):
            skipped.append({"account_id": account_id, "reason": result.get("error") or "无法补做 2FA"})
            continue
        item = {
            "account_id": account_id,
            "job_id": (result.get("job") or {}).get("id"),
            "message": result.get("message"),
        }
        (reused if result.get("reused") else started).append(item)

    response = {
        "ok": bool(started or reused),
        "started": started,
        "started_count": len(started),
        "reused": reused,
        "reused_count": len(reused),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "workers": workers,
    }
    if not response["ok"]:
        response.update({"error": "没有可补做 2FA 的账号", "status": 409})
    else:
        response["message"] = f"已启动 {len(started)} 个 Reactive 2FA，复用 {len(reused)} 个排队任务"
    return response


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = _local_now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            db.update_job(
                int(job["id"]),
                status="cancelled",
                completed_at=now_iso,
                error="用户手动取消",
            )
            cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def request_stop_job(job_id: int) -> dict:
    """手动停止单个注册任务。pending 直接取消；running 设置停止标记，运行线程会在检查点退出。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在", "status": 404}
    status = job.get("status")
    now_iso = _local_now().isoformat(timespec="seconds")
    if status == "pending":
        db.update_job(job_id, status="cancelled", completed_at=now_iso, error="用户手动停止/取消排队")
        _append_job_log(
            job_id,
            "用户手动停止：任务尚未运行，已取消排队。",
            marker="manual-stop",
        )
        return {"ok": True, "message": "排队任务已取消", "job_id": job_id, "state": "cancelled"}
    if status in ("success", "failed", "cancelled", "stopped"):
        return {"ok": True, "message": f"任务已结束：{status}", "job_id": job_id, "state": status}
    if status in ("running", "stopping"):
        with _STOP_LOCK:
            active = int(job_id) in _ACTIVE_JOBS
            ev = _STOP_EVENTS.get(int(job_id)) if active else None
            if ev is not None:
                ev.set()
        if not active or ev is None:
            # Web 服务重启、线程异常退出、历史残留 stopping，或之前手动停止时只创建了 stop event
            # 但没有真实线程实例：直接落为 stopped，避免永远卡在“停止中”。
            with _STOP_LOCK:
                _STOP_EVENTS.pop(int(job_id), None)
                _ACTIVE_JOBS.discard(int(job_id))
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="用户手动停止（任务实例不存在）",
            )
            _release_unconsumed_job_email(
                str(job.get("email") or "").strip() or None,
                "任务实例不存在，确认未继续执行",
            )
            _append_job_log(
                job_id,
                "用户手动停止：未找到运行中的任务实例，已直接标记为已停止。",
                marker="manual-stop",
            )
            logger.warning("[Service] 用户停止任务 #%s：任务实例不存在，已直接标记 stopped", job_id)
            return {"ok": True, "message": "任务实例不存在，已直接标记为已停止", "job_id": job_id, "state": "stopped"}
        db.update_job(job_id, status="stopping", error="用户手动停止中")
        _append_job_log(
            job_id,
            "用户手动停止：已发送停止信号，任务会在当前步骤检查点退出。",
            marker="manual-stop",
        )
        logger.warning("[Service] 用户请求停止任务 #%s", job_id)
        return {"ok": True, "message": "已发送停止信号", "job_id": job_id, "state": "stopping"}
    return {"ok": False, "error": f"当前状态不支持停止：{status}", "status": 409}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
