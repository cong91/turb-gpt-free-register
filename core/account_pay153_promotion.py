"""PAY.153 promotion probe for Free accounts in the account workspace."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime, timezone

from core import db, extract_link_service
from core.account_network import required_account_proxy
from core.registration_auto_pay153 import classify_checkout_session_id
from core.rotating_proxy_runtime import EXTRACT_LINK_PROMOTION_PROXY_SCOPE

logger = logging.getLogger(__name__)

PAY153_PROMOTION_LINK_TYPE = "ph_short"
PAY153_PROMOTION_PROXY_COUNTRY = "VN"


def _checked_at() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _is_free_account(plan_result: dict | None) -> bool:
    """Promotion can be probed for any authoritative Free account."""
    return bool(
        isinstance(plan_result, dict)
        and plan_result.get("ok")
        and str(plan_result.get("current_plan_type") or "").strip().lower() == "free"
    )


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


def _persist(account_id: int, result: dict) -> None:
    try:
        db.update_account_pay153_promotion(account_id, result)
    except Exception as exc:  # noqa: BLE001 - preserve the provider result for the caller.
        logger.warning(
            "[PAY.153][Promotion] 保存账号状态失败: account_id=%s error=%s: %s",
            account_id,
            type(exc).__name__,
            str(exc)[:180],
        )


def _base_result(plan_result: dict | None) -> dict:
    return {
        "status": "failed",
        "ok": False,
        "link_type": PAY153_PROMOTION_LINK_TYPE,
        "promotion_proxy_country": PAY153_PROMOTION_PROXY_COUNTRY,
        "checkout_session_id": None,
        "checkout_session_kind": "unknown",
        "amount_verification": None,
        "plus_trial_eligible_before": (
            plan_result.get("plus_trial_eligible")
            if isinstance(plan_result, dict)
            else None
        ),
        "checked_at": _checked_at(),
    }


def run_account_pay153_promotion_probe(
    *,
    account_id: int,
    email: str,
    access_token: str,
    plan_result: dict,
    checkout_proxy: str | None = None,
    promotion_proxy: str | None = None,
    browser_transport=None,
) -> dict:
    """Apply the PAY.153 promotion route and persist a safe account status.

    The standalone Account Workspace action uses one strict VN route from a
    rotating lease or proxy pool. When called while registration still owns a
    live browser, it reuses that browser and its registration proxy instead.
    ``CheckoutExtractor`` verifies that the selected promotion route exits in
    Vietnam before attempting the promotion update.
    """
    result = _base_result(plan_result)
    in_registration_flow = browser_transport is not None
    if in_registration_flow:
        result["promotion_proxy_country"] = "registration"
    if not _is_free_account(plan_result):
        result.update({
            "status": "skipped",
            "ok": True,
            "message": "账号不是 Free，跳过 Promotion VN",
        })
        _persist(account_id, result)
        return result

    token = str(access_token or "").strip()
    if not token:
        result.update({
            "error": "账号缺少 access_token，无法应用 Promotion VN",
            "message": "Promotion VN 未执行",
        })
        _persist(account_id, result)
        return result

    try:
        route_context = (
            nullcontext((checkout_proxy or promotion_proxy, "browser"))
            if in_registration_flow
            else required_account_proxy(
                None,
                rotating_scope=EXTRACT_LINK_PROMOTION_PROXY_SCOPE,
            )
        )
        with route_context as (active_proxy, network_mode):
            raw_result = extract_link_service._run_local_checkout(
                token=token,
                link_type=PAY153_PROMOTION_LINK_TYPE,
                proxy=active_proxy,
                promotion_proxy=promotion_proxy or active_proxy,
                browser_transport=browser_transport,
                checkout_proxy_country=(
                    None if in_registration_flow else PAY153_PROMOTION_PROXY_COUNTRY
                ),
                promotion_proxy_country=(
                    None if in_registration_flow else PAY153_PROMOTION_PROXY_COUNTRY
                ),
                verify_proxy_country=not in_registration_flow,
                apply_promo=True,
                log=lambda message: logger.info(
                    "[PAY.153][Promotion][%s] %s",
                    email,
                    str(message)[:300],
                ),
            )
        raw_result = raw_result if isinstance(raw_result, dict) else {}
        payload = raw_result.get("result") if isinstance(raw_result.get("result"), dict) else {}
        session_id = _session_id(raw_result, payload)
        session_kind = classify_checkout_session_id(session_id)
        amount_verification = str(
            payload.get("amount_verification")
            or raw_result.get("amount_verification")
            or ""
        ).strip().lower() or None
        result.update({
            "status": "success" if bool(raw_result.get("ok")) and str(raw_result.get("status") or "") == "success" else "failed",
            "ok": bool(raw_result.get("ok")) and str(raw_result.get("status") or "") == "success",
            "checkout_session_id": session_id or None,
            "checkout_session_kind": session_kind,
            "amount_verification": amount_verification,
            "promotion_proxy_mode": network_mode,
            "checked_at": raw_result.get("checked_at") or result["checked_at"],
        })
        if session_kind not in {"oaics", "cs_live"}:
            result.update({
                "status": "failed",
                "ok": False,
                "error": "Promotion VN 未返回可识别的 oaics_ 或 cs_live_ checkout session id",
            })
        elif amount_verification != "verified_zero":
            result.update({
                "status": "failed",
                "ok": False,
                "error": "Promotion VN 未确认 zero (0) 金额，未标记为已应用",
            })
        elif not result["ok"]:
            result["error"] = str(
                raw_result.get("error") or "PAY.153 Promotion VN checkout failed"
            )[:240]
        else:
            result["message"] = "Promotion VN 已应用，等待复查 Free Trial 资格"
    except Exception as exc:  # noqa: BLE001 - provider failures are account-visible and retryable.
        result.update({
            "retryable": True,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            "message": "PAY.153 Promotion VN 执行失败",
        })
        logger.warning("[PAY.153][Promotion] %s: %s", email, result["error"])

    _persist(account_id, result)
    return result
