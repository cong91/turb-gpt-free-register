"""Public result and history contracts for personal-information changes."""
from __future__ import annotations

import uuid

from flask import jsonify

from core import db
from core.account_security import redact_twofa_result


def _change_response(mode: str, results: list[dict], submitted: int, succeeded: int):
    public_results = [_public_result(result) for result in results]
    try:
        batch = db.save_personal_info_change_batch(
            uuid.uuid4().hex,
            mode,
            public_results,
        )
    except Exception:  # noqa: BLE001
        return jsonify({
            "ok": False,
            "error": "无法保存本次变更记录",
            "submitted": submitted,
            "succeeded": succeeded,
            "failed": submitted - succeeded,
            "results": public_results,
        }), 500
    return jsonify({
        "ok": succeeded == submitted,
        "submitted": submitted,
        "succeeded": succeeded,
        "failed": submitted - succeeded,
        "change_batch_id": batch["batch_id"],
        "exportable_count": batch["exportable_count"],
        "results": public_results,
    })


def _codex_public(result: object) -> dict:
    if not isinstance(result, dict):
        return {"status": "failed", "ok": False, "reason": "Đăng nhập Codex không trả về kết quả hợp lệ"}
    status = str(result.get("status") or "failed").strip().lower()
    if status not in {"waiting_change", "running", "success", "receipt", "failed", "skipped"}:
        status = "failed"
    output = {"status": status, "ok": bool(result.get("ok")) and status in {"success", "receipt"}}
    reason = str(result.get("reason") or "").strip()
    if reason:
        output["reason"] = reason[:300]
    if "credential_saved" in result:
        output["credential_saved"] = bool(result.get("credential_saved"))
    return output


def _plan_check_note(plan_check: object) -> str:
    """Human-readable plan suffix appended to a successful change row detail."""
    if not isinstance(plan_check, dict) or not plan_check.get("accepted"):
        return ""
    if plan_check.get("ok"):
        plan = str(plan_check.get("current_plan_type") or "unknown").strip()
        trial = " (có Plus trial)" if bool(plan_check.get("plus_trial_eligible")) else ""
        return f" Gói: {plan}{trial}."
    error = str(plan_check.get("error") or "").strip()
    return f" Kiểm tra gói thất bại: {error[:120]}." if error else " Kiểm tra gói thất bại."


def _public_result(result: dict) -> dict:
    safe = redact_twofa_result(result)
    if isinstance(safe.get("codex"), dict):
        safe["codex"] = _codex_public(safe["codex"])
    if _result_succeeded(safe):
        safe["change_status"] = "success"
    elif safe.get("remote_disabled"):
        safe["change_status"] = "partial_failure"
    else:
        safe["change_status"] = "failed"
    return safe


def _result_succeeded(result: dict) -> bool:
    return bool(
        result.get("ok")
        and result.get("persisted", True)
        and result.get("access_token_saved", True)
    )
