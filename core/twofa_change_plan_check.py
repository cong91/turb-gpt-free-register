"""Plan checking after a persisted 2FA browser change."""
from __future__ import annotations

from core.account_security import TwofaChangeInput


def run_twofa_plan_check(
    account_id: int,
    item: TwofaChangeInput,
    access_token: str,
    profile,
    result: dict[str, object],
) -> None:
    """Check the plan using the browser session owned by the 2FA worker."""
    if not access_token:
        result["plan_check"] = {"accepted": False, "error": "access token is missing"}
        return
    try:
        from core.account_export import BrowserPageTransport
        from core.plan_check_service import run_sync_plan_check

        plan_result = run_sync_plan_check(
            account_id=account_id,
            email=item.email,
            access_token=access_token,
            trigger="twofa_change",
            browser_transport=BrowserPageTransport(profile.driver),
        )
    except Exception as exc:
        plan_result = {"accepted": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    result["plan_check"] = {
        "accepted": bool(plan_result.get("accepted")),
        "ok": bool(plan_result.get("ok")),
        "status": "success" if plan_result.get("ok") else "failed",
        "current_plan_type": plan_result.get("current_plan_type"),
        "plus_trial_eligible": plan_result.get("plus_trial_eligible"),
        "error": str(plan_result.get("error") or "").strip() or None,
    }
    if not plan_result.get("ok"):
        result["warning"] = result.get("warning") or str(
            plan_result.get("error") or "account plan check failed"
        )
