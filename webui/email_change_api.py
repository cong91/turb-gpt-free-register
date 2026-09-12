"""Authenticated API for credential-driven personal-information changes."""
from __future__ import annotations

import logging
import secrets
import threading
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
_MAX_PROGRESS_BATCHES = 24
_PROGRESS_TERMINAL_STATUSES = frozenset({"success", "partial_failure", "failed"})
_progress_lock = threading.RLock()
_twofa_progress: dict[str, dict] = {}
logger = logging.getLogger(__name__)


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


def _trim_progress_locked() -> None:
    if len(_twofa_progress) <= _MAX_PROGRESS_BATCHES:
        return
    ordered = sorted(
        _twofa_progress.items(),
        key=lambda pair: str(pair[1].get("created_at") or ""),
        reverse=True,
    )
    _twofa_progress.clear()
    _twofa_progress.update(ordered[:_MAX_PROGRESS_BATCHES])


def _create_twofa_progress(batch_id: str, items: list) -> None:
    with _progress_lock:
        _twofa_progress[batch_id] = {
            "batch_id": batch_id,
            "status": "running",
            "created_at": uuid.uuid1().time,
            "submitted": len(items),
            "results": [
                {
                    "index": index,
                    "email": item.email,
                    "status": "queued",
                    "detail": "Đang chờ luồng xử lý",
                }
                for index, item in enumerate(items)
            ],
        }
        _trim_progress_locked()


def _apply_twofa_progress(batch_id: str, index: int, update: dict) -> None:
    with _progress_lock:
        batch = _twofa_progress.get(batch_id)
        if not batch:
            return
        rows = batch.get("results") or []
        if not isinstance(index, int) or index < 0 or index >= len(rows):
            return
        row = rows[index]
        status = str(update.get("status") or "running").strip().lower()
        if status not in {"queued", "running", "success", "partial_failure", "failed"}:
            status = "running"
        row["status"] = status
        if update.get("email"):
            row["email"] = str(update["email"]).strip()
        detail = str(update.get("detail") or "").strip()
        if detail:
            row["detail"] = detail[:500]
        result = update.get("result")
        if isinstance(result, dict):
            safe = _public_result(result)
            for key in ("account_id", "email", "old_email", "new_email", "change_status", "error", "warning", "plan_check"):
                if key in safe and safe[key] not in (None, ""):
                    row[key] = safe[key]
            row["status"] = str(safe.get("change_status") or status)
            row["detail"] = str(safe.get("error") or safe.get("warning") or row.get("detail") or "").strip()[:500]


def _twofa_progress_snapshot(batch_id: str) -> dict | None:
    with _progress_lock:
        batch = _twofa_progress.get(batch_id)
        if not batch:
            return None
        rows = [dict(row) for row in (batch.get("results") or []) if isinstance(row, dict)]
        terminal = sum(1 for row in rows if row.get("status") in _PROGRESS_TERMINAL_STATUSES)
        succeeded = sum(1 for row in rows if row.get("status") == "success")
        failed = sum(1 for row in rows if row.get("status") in {"failed", "partial_failure"})
        pending = sum(1 for row in rows if row.get("status") == "queued")
        running = sum(1 for row in rows if row.get("status") == "running")
        status = batch.get("status") or ("completed" if terminal == len(rows) else "running")
        return {
            "ok": status == "running" or (status == "completed" and failed == 0),
            "batch_id": batch_id,
            "status": status,
            "submitted": len(rows),
            "succeeded": succeeded,
            "failed": failed,
            "pending": pending,
            "running": running,
            "completed": terminal,
            "results": rows,
            "change_batch_id": batch.get("change_batch_id"),
            "exportable_count": int(batch.get("exportable_count") or 0),
            "batch_error": batch.get("batch_error"),
        }


def _finish_twofa_progress(batch_id: str, results: list[dict]) -> None:
    for index, result in enumerate(results):
        status = "success" if _result_succeeded(result) else (
            "partial_failure" if result.get("remote_disabled") else "failed"
        )
        _apply_twofa_progress(
            batch_id,
            index,
            {
                "status": status,
                "email": result.get("email"),
                "result": result,
            },
        )
    public_results = [_public_result(result) for result in results]
    succeeded = sum(1 for result in public_results if result.get("change_status") == "success")
    try:
        batch = db.save_personal_info_change_batch(batch_id, "twofa", public_results)
    except Exception as exc:
        logger.exception("保存 2FA 变更批次失败: batch=%s", batch_id)
        with _progress_lock:
            current = _twofa_progress.get(batch_id)
            if current:
                current["status"] = "failed"
                current["batch_error"] = f"无法保存本次变更记录: {type(exc).__name__}"
        return
    with _progress_lock:
        current = _twofa_progress.get(batch_id)
        if current:
            current["status"] = "completed"
            current["change_batch_id"] = batch["batch_id"]
            current["exportable_count"] = batch["exportable_count"]
            current["succeeded"] = succeeded


def _run_twofa_progress_batch(batch_id: str, items: list, workers: int) -> None:
    try:
        results = run_twofa_change_batch(
            items,
            workers=workers,
            progress_callback=lambda index, update: _apply_twofa_progress(batch_id, index, update),
        )
        _finish_twofa_progress(batch_id, results)
    except Exception:
        logger.exception("2FA 变更批次异常: batch=%s", batch_id)
        with _progress_lock:
            current = _twofa_progress.get(batch_id)
            if current:
                current["status"] = "failed"
                current["batch_error"] = "批量处理线程失败，请重试"
                for row in current.get("results") or []:
                    if row.get("status") not in _PROGRESS_TERMINAL_STATUSES:
                        row["status"] = "failed"
                        row["detail"] = "批量处理线程失败，请重试"


def _start_twofa_progress_batch(batch_id: str, items: list, workers: int) -> None:
    thread = threading.Thread(
        target=_run_twofa_progress_batch,
        args=(batch_id, items, workers),
        name=f"twofa-change-batch-{batch_id[:8]}",
        daemon=True,
    )
    thread.start()


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
        batch_id = uuid.uuid4().hex
        _create_twofa_progress(batch_id, items)
        try:
            _start_twofa_progress_batch(batch_id, items, workers)
        except Exception as exc:
            logger.exception("启动 2FA 变更批次失败: batch=%s", batch_id)
            with _progress_lock:
                job = _twofa_progress.get(batch_id)
                if job:
                    job["status"] = "failed"
                    job["batch_error"] = f"无法启动处理线程: {type(exc).__name__}"
            return jsonify({"ok": False, "error": "无法启动 2FA 处理任务", "batch_id": batch_id}), 500
        snapshot = _twofa_progress_snapshot(batch_id) or {
            "ok": True,
            "batch_id": batch_id,
            "status": "running",
            "submitted": len(items),
            "pending": len(items),
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "completed": 0,
            "results": [],
        }
        return jsonify(snapshot), 202

    @app.get("/api/accounts/change-twofa-status")
    def api_accounts_change_twofa_status():
        batch_id = str(request.args.get("batch_id") or "").strip()
        if not batch_id:
            return jsonify({"ok": False, "error": "batch_id là bắt buộc"}), 400
        snapshot = _twofa_progress_snapshot(batch_id)
        if snapshot is None:
            return jsonify({"ok": False, "error": "Không tìm thấy tiến độ đổi 2FA"}), 404
        return jsonify(snapshot)

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
