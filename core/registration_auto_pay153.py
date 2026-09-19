"""Automatic PAY.153 checkout classification for newly registered accounts."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime, timezone

from core import db, extract_link_service
from core.account_network import required_account_proxy
from core.rotating_proxy_runtime import EXTRACT_LINK_PROXY_SCOPE

logger = logging.getLogger(__name__)

AUTO_PAY153_LINK_TYPE = "ph_short"


def enqueue_registration_auto_pay153(
    *,
    account_id: int,
    email: str,
    access_token: str,
    proxy: str | None = None,
) -> dict:
    """Queue plan confirmation and PAY.153 after a failed 2FA setup.

    The account checkpoint already contains a bearer token at this point, so
    the background worker can classify the checkout without keeping a browser
    session alive.  The normal Codex worker remains gated by its own flags.
    """
    from config import register as register_cfg

    if not bool(getattr(register_cfg, "AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", False)):
        return {"accepted": False, "busy": False, "reason": "disabled"}

    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=int(account_id),
            email=email,
            access_token=access_token,
            trigger="registration_auto",
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001 - registration failure must remain recorded.
        logger.warning(
            "[PAY.153][注册后] 2FA 失败后的套餐/PAY 入队异常: account_id=%s error=%s: %s",
            account_id,
            type(exc).__name__,
            str(exc)[:180],
        )
        return {
            "accepted": False,
            "busy": False,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    if not queued.get("accepted") and not queued.get("busy"):
        logger.warning(
            "[PAY.153][注册后] 2FA 失败后的套餐/PAY 入队失败: account_id=%s error=%s",
            account_id,
            queued.get("error") or "未知错误",
        )
    return queued


def enqueue_registration_pay153_retry(
    *,
    account_id: int,
    proxy: str | None = None,
    confirm_ambiguous_checkout: bool = False,
) -> dict:
    """Queue an explicit operator retry after an interrupted checkout."""
    account = db.get_account(int(account_id))
    if not isinstance(account, dict):
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    if bool(account.get("pay153_recovery_required")) and not confirm_ambiguous_checkout:
        return {
            "accepted": False,
            "busy": False,
            "recovery_required": True,
            "error": "上次 PAY.153 checkout 状态不明确，请确认后重试",
        }
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "该账号没有 access_token"}
    try:
        from core.plan_check_service import enqueue_account_plan_check

        return enqueue_account_plan_check(
            account_id=int(account_id),
            email=account.get("email") or "",
            access_token=access_token,
            trigger="manual_pay153_retry",
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001 - report an operator-visible retry error.
        logger.warning(
            "[PAY.153] 手动重试入队异常: account_id=%s error=%s: %s",
            account_id,
            type(exc).__name__,
            str(exc)[:180],
        )
        return {
            "accepted": False,
            "busy": False,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


def classify_checkout_session_id(value: object) -> str:
    """Normalize a PAY.153 checkout id to the transport family we expose."""
    session_id = str(value or "").strip().lower()
    if session_id.startswith("oaics_"):
        return "oaics"
    if session_id.startswith("cs_live_"):
        return "cs_live"
    if session_id.startswith("cs_test_"):
        return "cs_test"
    return "unknown"


def is_free_trial_plan_result(plan_result: dict | None) -> bool:
    """Return True only for an authoritative Free account with trial eligibility."""
    if not isinstance(plan_result, dict) or not bool(plan_result.get("ok")):
        return False
    return (
        str(plan_result.get("current_plan_type") or "").strip().lower() == "free"
        and plan_result.get("plus_trial_eligible") is True
    )


def _checked_at() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _persist(account_id: int, result: dict) -> None:
    try:
        db.update_account_pay153(account_id, result)
    except Exception as exc:  # noqa: BLE001 - persistence must not hide the checkout result.
        logger.warning(
            "[PAY.153][注册后] 保存账号状态失败: account_id=%s error=%s: %s",
            account_id,
            type(exc).__name__,
            str(exc)[:180],
        )


def _skipped_result(message: str) -> dict:
    return {
        "status": "skipped",
        "ok": True,
        "message": message,
        "link_type": AUTO_PAY153_LINK_TYPE,
        "checkout_session_id": None,
        "checkout_session_kind": "unknown",
        "checked_at": _checked_at(),
    }


def _already_completed_result(account_id: int) -> dict | None:
    """Build a stable result when an account already has a successful checkout."""
    try:
        account = db.get_account(account_id)
    except Exception as exc:  # noqa: BLE001 - a read failure must not block a first attempt.
        logger.debug(
            "[PAY.153][注册后] 读取既有状态失败，继续首次 checkout: account_id=%s error=%s",
            account_id,
            type(exc).__name__,
        )
        return None
    if not isinstance(account, dict) or str(account.get("pay153_status") or "").strip().lower() != "success":
        return None
    result = _skipped_result("PAY.153 已成功执行，跳过重复 checkout")
    result["checkout_session_kind"] = account.get("pay153_checkout_session_kind") or "unknown"
    result["checked_at"] = account.get("pay153_checked_at") or result["checked_at"]
    return result


def _session_id(raw_result: dict, payload: dict) -> str:
    candidates: list[str] = []
    for source in (raw_result, payload):
        for key in (
            "checkout_session_id",
            "custom_checkout_session_id",
            "stripe_checkout_session_id",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                candidates.append(value)
        ids = source.get("checkout_session_ids")
        if isinstance(ids, dict):
            for key in ("custom", "stripe", "oaics", "cs"):
                value = str(ids.get(key) or "").strip()
                if value:
                    candidates.append(value)
    for candidate in candidates:
        if classify_checkout_session_id(candidate) != "unknown":
            return candidate
    return candidates[0] if candidates else ""


def run_registration_auto_pay153(
    *,
    account_id: int,
    email: str,
    access_token: str,
    proxy: str | None = None,
    browser_transport=None,
    plan_result: dict,
    allow_recovery: bool = False,
) -> dict:
    """Run PAY.153 after plan confirmation and persist its session family.

    The operation is deliberately best-effort for registration: a checkout
    failure is recorded on the account and returned to the caller, but it does
    not erase the successfully registered account.
    """
    if not is_free_trial_plan_result(plan_result):
        result = _skipped_result("账号不是已确认的 Free Trial，跳过 PAY.153")
        _persist(account_id, result)
        return result

    try:
        claim_status = db.claim_account_pay153(account_id, allow_recovery=allow_recovery)
    except Exception as exc:  # noqa: BLE001 - surface database failures without calling checkout.
        result = {
            "status": "failed",
            "ok": False,
            "retryable": True,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            "message": "PAY.153 无法锁定账号任务",
            "link_type": AUTO_PAY153_LINK_TYPE,
            "checkout_session_id": None,
            "checkout_session_kind": "unknown",
            "checked_at": _checked_at(),
        }
        logger.warning("[PAY.153][注册后] %s: %s", email, result["error"])
        _persist(account_id, result)
        return result
    if claim_status == "completed":
        completed = _already_completed_result(account_id)
        if completed is not None:
            return completed
        return _skipped_result("PAY.153 已成功执行，跳过重复 checkout")
    elif claim_status == "busy":
        return _skipped_result("PAY.153 已有任务正在执行，跳过重复 checkout")
    elif claim_status == "recovery_required":
        return {
            "status": "failed",
            "ok": False,
            "retryable": False,
            "error": "PAY.153 上次 checkout 被中断，状态不明确；请确认后手动重试",
            "message": "PAY.153 上次 checkout 被中断，未自动重复执行",
            "link_type": AUTO_PAY153_LINK_TYPE,
            "checkout_session_id": None,
            "checkout_session_kind": "unknown",
            "checked_at": _checked_at(),
        }
    elif claim_status != "claimed":
        result = {
            "status": "failed",
            "ok": False,
            "retryable": False,
            "error": "PAY.153 账号记录不存在，无法锁定 checkout 任务",
            "message": "PAY.153 注册后自动流程未找到账号记录",
            "link_type": AUTO_PAY153_LINK_TYPE,
            "checkout_session_id": None,
            "checkout_session_kind": "unknown",
            "checked_at": _checked_at(),
        }
        _persist(account_id, result)
        return result

    try:
        route_context = (
            nullcontext((proxy, "browser"))
            if browser_transport is not None
            else required_account_proxy(None, rotating_scope=EXTRACT_LINK_PROXY_SCOPE)
        )
        with route_context as (active_proxy, network_mode):
            raw_result = extract_link_service._run_local_checkout(
                token=access_token,
                link_type=AUTO_PAY153_LINK_TYPE,
                proxy=active_proxy,
                browser_transport=browser_transport,
                verify_proxy_country=False if browser_transport is not None else None,
                log=lambda message: logger.info("[PAY.153][注册后] %s", str(message)[:300]),
            )
        raw_result = raw_result if isinstance(raw_result, dict) else {}
        payload = raw_result.get("result") if isinstance(raw_result.get("result"), dict) else {}
        session_id = _session_id(raw_result, payload)
        session_kind = classify_checkout_session_id(session_id)
        result = dict(raw_result)
        result.update({
            "checked_at": raw_result.get("checked_at") or _checked_at(),
            "checkout_session_id": session_id or None,
            "checkout_session_kind": session_kind,
            "link_type": raw_result.get("link_type") or AUTO_PAY153_LINK_TYPE,
            "pay153_proxy_mode": network_mode,
        })
        if session_kind == "unknown":
            result.update({
                "status": "failed",
                "ok": False,
                "error": "PAY.153 未返回可识别的 oaics_ 或 cs_live_ checkout session id",
            })
        elif not bool(result.get("ok")) or str(result.get("status") or "") != "success":
            result["status"] = "failed"
            result["ok"] = False
        _persist(account_id, result)
        return result
    except Exception as exc:  # noqa: BLE001 - convert provider failures into account state.
        result = {
            "status": "failed",
            "ok": False,
            "retryable": True,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            "message": "PAY.153 注册后自动提链失败",
            "link_type": AUTO_PAY153_LINK_TYPE,
            "checkout_session_id": None,
            "checkout_session_kind": "unknown",
            "checked_at": _checked_at(),
        }
        logger.warning("[PAY.153][注册后] %s: %s", email, result["error"])
        _persist(account_id, result)
        return result
