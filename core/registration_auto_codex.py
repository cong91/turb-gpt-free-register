"""Synchronous plan check and Codex dispatch for a live registration browser."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from config import register as _register_cfg
from core import db
from core.chatgpt_plan import PlanCheckBrowserTransport, check_account_plan

logger = logging.getLogger(__name__)

_LIVE_BROWSER_REGISTRATION_DRIVERS = frozenset({
    "roxy",
    "roxybrowser",
    "fingerprint",
    "browser",
    "cloak",
    "cloakbrowser",
    "browser_use",
    "browseruse",
    "browser-use",
    "bu",
    "skyvern",
    "sv",
})


def configured_registration_driver() -> str:
    """Return the normalized registration driver from the live configuration."""
    from config import roxybrowser as _driver_cfg

    return str(
        getattr(_driver_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol"
    ).strip().lower()


def registration_driver_uses_live_browser(driver: str | None = None) -> bool:
    """Return whether a registration driver owns a browser Codex must reuse."""
    return str(driver or configured_registration_driver()).strip().lower() in _LIVE_BROWSER_REGISTRATION_DRIVERS


def account_registration_driver(account: dict | None) -> str:
    """Read the driver persisted with an account, falling back to live config."""
    if isinstance(account, dict):
        direct = str(account.get("registration_driver") or "").strip().lower()
        if direct:
            return direct
        extra_json = account.get("extra_json")
        if isinstance(extra_json, str) and extra_json:
            import json

            try:
                extra = json.loads(extra_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                extra = {}
            if isinstance(extra, dict):
                persisted = str(extra.get("registration_driver") or "").strip().lower()
                if persisted:
                    return persisted
    return configured_registration_driver()


def _skipped_codex(message: str) -> dict:
    return {"status": "skipped", "ok": True, "message": message}


def _failed_codex(message: str) -> dict:
    return {"status": "failed", "ok": False, "retryable": True, "message": message}


def _registration_plan_recheck_delay() -> float:
    from config import proxy as proxy_cfg

    try:
        value = float(getattr(proxy_cfg, "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0) or 0.0)
    except (TypeError, ValueError):
        value = 2.0
    return max(0.0, min(30.0, value))


def _check_registration_plan(
    access_token: str,
    *,
    proxy: str | None,
    browser_transport: PlanCheckBrowserTransport | None,
    timezone_offset_min: str,
) -> dict:
    try:
        request_kwargs = {
            "proxy": proxy,
            "timezone_offset_min": timezone_offset_min,
        }
        if browser_transport is not None:
            request_kwargs["browser_transport"] = browser_transport
        return check_account_plan(access_token, **request_kwargs)
    except Exception as exc:  # noqa: BLE001 - classify network failures for the retry boundary.
        return {
            "ok": False,
            "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            "retryable": True,
        }


def _check_registration_plan_with_retry(
    access_token: str,
    *,
    proxy: str | None,
    browser_transport: PlanCheckBrowserTransport | None,
    timezone_offset_min: str,
    email: str,
) -> dict:
    plan_result = _check_registration_plan(
        access_token,
        proxy=proxy,
        browser_transport=browser_transport,
        timezone_offset_min=timezone_offset_min,
    )
    if plan_result.get("ok") or not plan_result.get("retryable"):
        return plan_result

    delay = _registration_plan_recheck_delay()
    retry_mode = "复用当前 browser session 重试" if browser_transport is not None else "切换网络重试"
    logger.warning(
        "[Plan][Codex] 套餐查询临时失败，第 1/2 次，%.1fs 后%s：%s: %s",
        delay,
        retry_mode,
        email,
        plan_result.get("error") or "未知错误",
    )
    if delay > 0:
        time.sleep(delay)
    return _check_registration_plan(
        access_token,
        proxy=proxy,
        browser_transport=browser_transport,
        timezone_offset_min=timezone_offset_min,
    )


def run_registration_auto_codex(
    *,
    account_id: int,
    email: str,
    access_token: str,
    run_codex: Callable[[], dict],
    proxy: str | None = None,
    browser_transport: PlanCheckBrowserTransport | None = None,
    timezone_offset_min: str = "-",
    twofa_status: str = "active",
) -> dict:
    """Run the serialized post-registration plan/Codex flow.

    This is intentionally synchronous. The registration adapter still owns the
    browser here, so a background plan worker cannot race the adapter's cleanup
    and open a second browser for the same account.
    """
    from config import codex as _codex_cfg

    auto_plan_enabled = bool(
        getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
    )
    auto_free_codex_enabled = bool(
        getattr(_register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
    )
    generic_codex_enabled = bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False))
    if not (auto_plan_enabled or auto_free_codex_enabled or generic_codex_enabled):
        return {
            "plan": None,
            "codex": _skipped_codex("注册后套餐查询和 Codex 自动流程均已关闭"),
        }

    twofa_state = str(twofa_status or "").strip().lower()
    if twofa_state != "active":
        message = f"2FA 尚未 active（当前状态={twofa_state or 'unknown'}），禁止查询套餐和启动 Codex"
        logger.error("[Plan][Codex] %s: account_id=%s email=%s", message, account_id, email)
        return {
            "plan": None,
            "codex": _failed_codex(message),
        }

    account_id = int(account_id)
    if not db.claim_account_plan_check(acc_id=account_id, trigger="registration_auto"):
        message = "账号套餐查询已被其它任务占用，注册后串行流程终止"
        logger.warning("[Plan][Codex] %s: account_id=%s email=%s", message, account_id, email)
        return {"plan": None, "codex": _failed_codex(message)}
    if not db.mark_account_plan_check_running(account_id):
        message = "账号套餐查询无法进入执行状态，注册后串行流程终止"
        logger.warning("[Plan][Codex] %s: account_id=%s email=%s", message, account_id, email)
        return {"plan": None, "codex": _failed_codex(message)}

    if browser_transport is not None:
        logger.info("[Plan][Codex] 使用当前注册 browser session 查询套餐: %s", email)
    plan_result = _check_registration_plan_with_retry(
        access_token,
        proxy=proxy,
        browser_transport=browser_transport,
        timezone_offset_min=timezone_offset_min,
        email=email,
    )
    db.update_account_plan_check(acc_id=account_id, result=plan_result)

    if not bool(plan_result.get("ok")):
        message = f"套餐查询失败，未启动同浏览器 Codex: {plan_result.get('error') or '未知错误'}"
        logger.warning("[Plan][Codex] %s: %s", email, message)
        return {"plan": plan_result, "codex": _failed_codex(message)}

    plan = str(plan_result.get("current_plan_type") or "").strip().lower()
    if auto_free_codex_enabled:
        if plan != "free":
            message = f"当前套餐为 {plan or 'unknown'}，跳过 Free Codex 补跑"
            logger.info("[Plan][Codex] %s: %s", email, message)
            return {"plan": plan_result, "codex": _skipped_codex(message)}
        if plan_result.get("plus_trial_eligible") is not False:
            message = "Free 账号存在 Plus 试用资格或资格未知，跳过 Codex 补跑"
            logger.info("[Plan][Codex] %s: %s", email, message)
            return {"plan": plan_result, "codex": _skipped_codex(message)}

        logger.info("[Plan][Codex] 已确认 Free 且无 Plus 试用，复用当前注册浏览器: %s", email)
        codex_result = run_codex()
        return {"plan": plan_result, "codex": codex_result}

    if generic_codex_enabled:
        logger.info("[Plan][Codex] 套餐查询完成，启动通用 Codex OAuth: %s", email)
        codex_result = run_codex()
        return {"plan": plan_result, "codex": codex_result}

    return {
        "plan": plan_result,
        "codex": _skipped_codex("套餐查询已完成，Free Codex 自动流程已关闭"),
    }
