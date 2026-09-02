"""Authenticated target preview for bulk Codex login into sub2api."""
from __future__ import annotations

from flask import jsonify

from config import codex as _codex_cfg
from core import codex_retry_service, codex_sub2_free_login, db

_BULK_LIMIT = 500
_SCAN_LIMIT = 5000


def register_codex_sub2_routes(app) -> None:
    @app.get("/api/codex/sub2-free-no-trial-targets")
    def api_codex_sub2_free_no_trial_targets():
        source = str(_codex_cfg.CODEX_AUTH_URL_SOURCE or "").strip().lower()
        if source != "sub2":
            return jsonify({
                "ok": False,
                "error": "请先设置 CODEX_AUTH_URL_SOURCE=sub2，再登录到 sub2api",
            }), 409

        snapshot = db.list_account_plan_check_statuses(limit=_SCAN_LIMIT + 1, archived=False)
        codex_rows = db.list_codex_accounts(archived="all")
        authenticated_emails = {
            str(row.get("email") or "").strip().lower()
            for row in codex_rows
            if str(row.get("email") or "").strip()
        }
        retrying_emails = codex_retry_service.active_retrying_emails()
        result = codex_sub2_free_login.select_target_ids(
            snapshot.get("items") or [],
            authenticated_emails=authenticated_emails,
            retrying_emails=retrying_emails,
        )
        if int(snapshot.get("total") or 0) > _SCAN_LIMIT:
            return jsonify({
                "ok": False,
                "error": "当前未归档账号超过 5000 个，无法保证一次扫描全部账号",
            }), 409
        if result["count"] > _BULK_LIMIT:
            return jsonify({
                "ok": False,
                "error": "符合条件的账号超过 500 个，不能在单次批量任务中全部启动",
            }), 409

        return jsonify({
            "ok": True,
            "oauth_source": "sub2",
            "scanned_count": int(snapshot.get("total") or 0),
            **result,
        })
