"""Authenticated API for credential-driven personal-information changes."""
from __future__ import annotations

import secrets
import uuid

from flask import Response, jsonify, request

from core import db
from core.account_security import parse_twofa_change_inputs, redact_twofa_result
from core.browser_email_change import run_email_change_batch
from core.browser_twofa_change import run_twofa_change_batch
from core.email_change import parse_email_change_inputs
from webui.auth import code_is_valid

_MAX_JSON_BYTES = 512 * 1024
_MAX_ITEMS = 50


def _result_succeeded(result: dict) -> bool:
    return bool(
        result.get("ok")
        and result.get("persisted", True)
        and result.get("access_token_saved", True)
    )


def _public_result(result: dict) -> dict:
    safe = redact_twofa_result(result)
    if _result_succeeded(safe):
        safe["change_status"] = "success"
    elif safe.get("remote_disabled"):
        safe["change_status"] = "partial_failure"
    else:
        safe["change_status"] = "failed"
    return safe


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


def _same_origin_mutation() -> bool:
    origin = request.headers.get("Origin")
    if origin:
        expected = f"{request.scheme}://{request.host}"
        return secrets.compare_digest(origin.rstrip("/"), expected.rstrip("/"))
    referer = request.headers.get("Referer", "")
    if referer:
        return referer.startswith(f"{request.scheme}://{request.host}/")
    header_code = request.headers.get("X-Auth-Code") or request.headers.get("X-Authorization-Code")
    if header_code:
        return code_is_valid(header_code.strip())
    authorization = request.headers.get("Authorization", "")
    return authorization.lower().startswith("bearer ") and code_is_valid(authorization[7:].strip())


def register_email_change_routes(app) -> None:
    @app.post("/api/accounts/change-email")
    def api_accounts_change_email():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        try:
            items = parse_email_change_inputs(
                str(data.get("credentials") or ""),
                str(data.get("gmail_api") or ""),
                quota=data.get("quota", 1),
            )
            if len(items) > _MAX_ITEMS:
                raise ValueError(f"maximum {_MAX_ITEMS} accounts per request")
            workers = max(1, min(4, int(data.get("workers", 1) or 1)))
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        results = run_email_change_batch(items, workers=workers)
        succeeded = sum(1 for result in results if _result_succeeded(result))
        return _change_response("email", results, len(items), succeeded)

    @app.post("/api/accounts/change-twofa")
    def api_accounts_change_twofa():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        try:
            items = parse_twofa_change_inputs(str(data.get("credentials") or ""))
            if len(items) > _MAX_ITEMS:
                raise ValueError(f"maximum {_MAX_ITEMS} accounts per request")
            workers = max(1, min(4, int(data.get("workers", 1) or 1)))
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        results = run_twofa_change_batch(items, workers=workers)
        succeeded = sum(1 for result in results if _result_succeeded(result))
        return _change_response("twofa", results, len(items), succeeded)

    @app.post("/api/accounts/personal-info/export")
    @app.post("/api/accounts/change-email/export")
    def api_accounts_change_email_export():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        batch_id = str(data.get("batch_id") or "").strip() or None
        batch = db.get_personal_info_change_batch(batch_id)
        if batch is None:
            return jsonify({"ok": False, "error": "没有可导出的变更记录"}), 404
        exportable_count = int(batch.get("exportable_count") or 0)
        if exportable_count <= 0:
            return jsonify({"ok": False, "error": "没有可导出的已更新账号"}), 400
        if exportable_count > _MAX_ITEMS:
            return jsonify({"ok": False, "error": f"maximum {_MAX_ITEMS} accounts per export"}), 400

        rows = db.get_personal_info_change_export_rows(batch["batch_id"])
        if len(rows) != exportable_count:
            return jsonify({"ok": False, "error": "变更记录中的账号数据不完整，请刷新后重试"}), 409
        content = "".join(f"{db.account_line(row, 'modern')}\n" for row in rows)
        return Response(
            content,
            mimetype="text/plain",
            headers={
                "Content-Disposition": 'attachment; filename="personal-info-updated-accounts.txt"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
