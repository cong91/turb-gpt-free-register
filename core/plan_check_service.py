# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan
from core.rotating_proxy_runtime import PLAN_CHECK_PROXY_SCOPE, resolve_rotating_proxy

logger = logging.getLogger(__name__)


def _enqueue_auto_codex_for_free_account(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    result: dict,
) -> dict:
    """Queue Codex only after a confirmed Free/no-trial plan result."""
    from config import register as register_cfg

    if not bool(getattr(register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)):
        return {"accepted": False, "reason": "disabled"}
    if str(trigger or "").strip() != "registration_auto":
        return {"accepted": False, "reason": "trigger"}
    if not bool(result.get("ok")):
        return {"accepted": False, "reason": "plan_check_failed"}
    plan = str(result.get("current_plan_type") or "").strip().lower()
    if plan != "free":
        return {"accepted": False, "reason": "not_free"}
    if result.get("plus_trial_eligible") is not False:
        return {"accepted": False, "reason": "free_plus_or_unknown"}

    from core.registration_auto_codex import (
        account_registration_driver,
        registration_driver_uses_live_browser,
    )

    account = db.get_account(int(account_id)) or {}
    registration_driver = account_registration_driver(account)

    if registration_driver_uses_live_browser(registration_driver):
        return {
            "accepted": False,
            "reason": "live_browser_required",
            "registration_driver": registration_driver,
            "message": (
                "浏览器注册必须在注册 worker 内复用当前 browser 执行 Codex，"
                "禁止套餐 worker 另起 Codex browser"
            ),
        }

    from core.registration_service import submit_codex_retry_for_account

    return submit_codex_retry_for_account(
        account_id=int(account_id),
        email=email,
        access_token=access_token,
        trigger="registration_auto_free",
        registration_driver=registration_driver,
    )


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_ACTIVE_TASKS = 0
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _get_executor_locked() -> ThreadPoolExecutor:
    """Create the plan-check executor only while a batch is active."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=_WORKERS,
            thread_name_prefix="plan-check",
        )
    return _EXECUTOR


def _take_idle_executor_locked() -> ThreadPoolExecutor | None:
    """Detach an executor once no submitted task remains."""
    global _EXECUTOR
    if _ACTIVE_TASKS != 0 or _EXECUTOR is None:
        return None
    executor = _EXECUTOR
    _EXECUTOR = None
    return executor


def _on_plan_check_done(_future) -> None:
    """Stop idle plan-check threads after the last submitted task completes."""
    global _ACTIVE_TASKS
    executor_to_shutdown = None
    with _EXECUTOR_LOCK:
        _ACTIVE_TASKS -= 1
        executor_to_shutdown = _take_idle_executor_locked()
    if executor_to_shutdown is not None:
        executor_to_shutdown.shutdown(wait=False, cancel_futures=False)


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    proxy_lane_id: int | None = None,
    lease_owner_id: str | None = None,
) -> dict:
    try:
        if not db.mark_account_plan_check_running(account_id):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}

        selected_proxy = resolve_rotating_proxy(
            proxy,
            scope=PLAN_CHECK_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        if selected_proxy is None:
            from core.nordvpn_wireguard import is_per_profile_proxy_enabled, proxy_for_registration

            if is_per_profile_proxy_enabled():
                proxy_context = (
                    proxy_for_registration(owner_id=lease_owner_id)
                    if lease_owner_id is not None
                    else proxy_for_registration()
                )
            else:
                proxy_context = nullcontext(None)
        else:
            proxy_context = nullcontext(selected_proxy)

        with proxy_context as active_proxy:
            _wait_for_rate_slot()
            result = check_account_plan(
                access_token,
                proxy=active_proxy,
                timezone_offset_min=timezone_offset_min,
            )
            recheck_delay = _registration_recheck_delay()
            transient_failure = not bool(result.get("ok")) and bool(result.get("retryable"))
            free_without_plus_trial = (
                bool(result.get("ok"))
                and str(result.get("current_plan_type") or "").lower() == "free"
                and not bool(result.get("plus_trial_eligible"))
            )
            should_recheck = (
                trigger == "registration_auto"
                and recheck_delay > 0
                and (transient_failure or free_without_plus_trial)
            )
            if should_recheck:
                reason = "查询临时失败" if transient_failure else "暂未发现 Plus 试用资格"
                logger.info("[Plan] 新账号%s，%.1fs 后复查一次: %s", reason, recheck_delay, email)
                time.sleep(recheck_delay)
                _wait_for_rate_slot()
                recheck_result = check_account_plan(
                    access_token,
                    proxy=active_proxy,
                    timezone_offset_min=timezone_offset_min,
                    max_attempts=1,
                )
                if recheck_result.get("ok"):
                    result = recheck_result
                else:
                    logger.warning(
                        "[Plan] 新账号资格复查失败，保留首次查询结果: %s, %s",
                        email,
                        recheck_result.get("error") or "未知错误",
                    )

        db.update_account_plan_check(acc_id=account_id, result=result)
        auto_codex = _enqueue_auto_codex_for_free_account(
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=trigger,
            result=result,
        )
        if auto_codex.get("accepted"):
            logger.info("[Codex] Free 无 Plus 试用账号已自动排队补跑: %s", email)
        elif auto_codex.get("reason") == "live_browser_required":
            logger.info(
                "[Codex] 已跳过独立补跑 worker：registration_driver=%s，"
                "Codex 必须在注册 worker 内复用当前 browser: %s",
                auto_codex.get("registration_driver") or "browser",
                email,
            )
        elif auto_codex.get("reason") not in {
            "disabled", "trigger", "free_plus_or_unknown", "not_free",
            "already_success", "deactivated", "live_browser_required",
        }:
            logger.warning("[Codex] Free 账号自动补跑入队失败: %s, %s", email, auto_codex)
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
    timezone_offset_min: str = "-",
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    global _ACTIVE_TASKS
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    if not db.claim_account_plan_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    task_counted = False
    executor_to_shutdown = None
    try:
        with _EXECUTOR_LOCK:
            executor = _get_executor_locked()
            _ACTIVE_TASKS += 1
            task_counted = True
            future = executor.submit(
                _run_plan_check,
                account_id=account_id,
                email=email,
                access_token=access_token,
                trigger=str(trigger or "manual"),
                proxy=proxy,
                proxy_lane_id=proxy_lane_id,
                lease_owner_id=f"plan-check:{account_id}",
                timezone_offset_min=str(timezone_offset_min or "-"),
            )
    except Exception as exc:
        with _EXECUTOR_LOCK:
            if task_counted:
                _ACTIVE_TASKS -= 1
            executor_to_shutdown = _take_idle_executor_locked()
        if executor_to_shutdown is not None:
            executor_to_shutdown.shutdown(wait=False, cancel_futures=False)
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    future.add_done_callback(_on_plan_check_done)

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
