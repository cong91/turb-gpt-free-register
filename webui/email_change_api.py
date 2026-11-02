"""Authenticated email-change and export routes."""
from __future__ import annotations

from flask import Response, jsonify, request

from core import db
from core.browser_email_change import run_email_change_batch
from core.email_change import parse_email_change_inputs
from webui.personal_info_progress import _progress_lock
from webui.personal_info_request import _MAX_ITEMS, _MAX_JSON_BYTES, _same_origin_mutation, _strict_bool
from webui.personal_info_results import _change_response, _result_succeeded


def register_email_change_routes(app) -> None:
    from webui.password_change_api import register_password_change_routes
    from webui.twofa_change_api import _twofa_progress, register_twofa_change_routes

    register_twofa_change_routes(app)
    register_password_change_routes(app)

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
            login_codex_after_change = _strict_bool(data, "login_codex_after_change")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        results = run_email_change_batch(
            items,
            workers=workers,
            login_codex_after_change=login_codex_after_change,
        )
        succeeded = sum(1 for result in results if _result_succeeded(result))
        return _change_response("email", results, len(items), succeeded)

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
        with _progress_lock:
            live_progress = _twofa_progress.get(str(batch.get("batch_id") or ""))
            if live_progress and live_progress.get("status") == "running":
                return jsonify({"ok": False, "error": "2FA 仍在处理，请等待该记录完成后再导出"}), 409
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
