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


def _trim_progress_locked(target_size: int = _MAX_PROGRESS_BATCHES) -> None:
    if len(_twofa_progress) <= target_size:
        return
    ordered = sorted(
        _twofa_progress.items(),
        key=lambda pair: str(pair[1].get("created_at") or ""),
    )
    for batch_id, batch in ordered:
        if len(_twofa_progress) <= target_size:
            break
        if batch.get("status") == "running":
            continue
        _twofa_progress.pop(batch_id, None)


def _create_twofa_progress(batch_id: str, items: list) -> bool:
    with _progress_lock:
        _trim_progress_locked(target_size=_MAX_PROGRESS_BATCHES - 1)
        if len(_twofa_progress) >= _MAX_PROGRESS_BATCHES:
            return False
        _twofa_progress[batch_id] = {
            "batch_id": batch_id,
            "status": "running",
            "created_at": uuid.uuid1().time,
            "submitted": len(items),
            "emails": [item.email for item in items],
            "public_results": [None] * len(items),
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
        return True


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
        if status == "queued" and row.get("status") == "running":
            # A one-record manual retry is already dispatched; do not briefly
            # show it as queued when the worker emits its initial callback.
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
            public_results = batch.get("public_results")
            if isinstance(public_results, list) and index < len(public_results):
                public_results[index] = dict(safe)
            for key in ("account_id", "email", "old_email", "new_email", "change_status", "error", "warning", "plan_check", "retryable"):
                if key in safe and safe[key] not in (None, ""):
                    row[key] = safe[key]
            row["status"] = str(safe.get("change_status") or status)
            result_detail = safe.get("error") or safe.get("warning")
            if result_detail:
                row["detail"] = str(result_detail).strip()[:500]
            elif row["status"] == "success":
                plan_check = safe.get("plan_check")
                row["detail"] = (
                    "Đã cập nhật dữ liệu tài khoản. Đang kiểm tra loại gói."
                    if isinstance(plan_check, dict) and plan_check.get("accepted")
                    else "Đã cập nhật dữ liệu tài khoản."
                )


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
    with _progress_lock:
        current = _twofa_progress.get(batch_id)
        public_results = [
            dict(result)
            for result in (current or {}).get("public_results", [])
            if isinstance(result, dict)
        ]
    if len(public_results) != len(results):
        public_results = [_public_result(result) for result in results]
    _persist_twofa_progress_batch(batch_id, public_results)


def _persist_twofa_progress_batch(batch_id: str, public_results: list[dict]) -> None:
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
                current["change_batch_id"] = None
                current["exportable_count"] = 0
        return
    with _progress_lock:
        current = _twofa_progress.get(batch_id)
        if current:
            current["status"] = "completed"
            current["change_batch_id"] = batch["batch_id"]
            current["exportable_count"] = batch["exportable_count"]
            current["succeeded"] = succeeded


def _public_results_for_batch(batch_id: str) -> list[dict] | None:
    with _progress_lock:
        current = _twofa_progress.get(batch_id)
        if not current:
            return None
        rows = current.get("results") or []
        stored = current.get("public_results")
        if not isinstance(stored, list) or len(stored) != len(rows):
            return None
        return [
            dict(value) if isinstance(value, dict) else {
                "email": row.get("email"),
                "change_status": "failed",
                "error": row.get("detail") or "2FA change did not return a result",
            }
            for value, row in zip(stored, rows)
        ]


def _batch_is_terminal(batch_id: str) -> bool:
    with _progress_lock:
        current = _twofa_progress.get(batch_id)
        if not current:
            return False
        rows = current.get("results") or []
        return bool(rows) and all(row.get("status") in _PROGRESS_TERMINAL_STATUSES for row in rows)


def _run_twofa_retry(batch_id: str, index: int, item) -> None:
    try:
        results = run_twofa_change_batch(
            [item],
            workers=1,
            progress_callback=lambda _worker_index, update: _apply_twofa_progress(
                batch_id,
                index,
                update,
            ),
        )
        result = results[0] if results else {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "error": "2FA retry returned no result",
        }
        status = "success" if _result_succeeded(result) else (
            "partial_failure" if result.get("remote_disabled") else "failed"
        )
        _apply_twofa_progress(
            batch_id,
            index,
            {"status": status, "email": item.email, "result": result},
        )
        public_results = _public_results_for_batch(batch_id) if _batch_is_terminal(batch_id) else None
        if public_results is not None:
            _persist_twofa_progress_batch(batch_id, public_results)
    except Exception:
        logger.exception("2FA manual retry failed: batch=%s index=%s", batch_id, index)
        failure = {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "error": "Thử lại 2FA thất bại, hãy thử lại thủ công",
        }
        _apply_twofa_progress(batch_id, index, {"status": "failed", "result": failure})
        with _progress_lock:
            current = _twofa_progress.get(batch_id)
            if current:
                current["status"] = "completed"
                current["batch_error"] = "Thử lại 2FA thất bại"
        public_results = _public_results_for_batch(batch_id)
        if public_results is not None:
            _persist_twofa_progress_batch(batch_id, public_results)


def _start_twofa_retry(batch_id: str, index: int, item) -> None:
    thread = threading.Thread(
        target=_run_twofa_retry,
        args=(batch_id, index, item),
        name=f"twofa-change-retry-{batch_id[:8]}-{index}",
        daemon=True,
    )
    thread.start()


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
        if not _create_twofa_progress(batch_id, items):
            return jsonify({"ok": False, "error": "Đang có quá nhiều batch đổi 2FA, hãy chờ batch cũ hoàn tất"}), 429
        try:
            _start_twofa_progress_batch(batch_id, items, workers)
        except Exception as exc:
            logger.exception("启动 2FA 变更批次失败: batch=%s", batch_id)
            with _progress_lock:
                job = _twofa_progress.get(batch_id)
                if job:
                    job["status"] = "failed"
                    job["batch_error"] = f"无法启动处理线程: {type(exc).__name__}"
                    for row in job.get("results") or []:
                        if row.get("status") not in _PROGRESS_TERMINAL_STATUSES:
                            row["status"] = "failed"
                            row["detail"] = "无法启动批量处理线程，请逐条重试"
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

    @app.post("/api/accounts/change-twofa-retry")
    def api_accounts_change_twofa_retry():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        batch_id = str(data.get("batch_id") or "").strip()
        credentials = str(data.get("credentials") or "").strip()
        try:
            index = int(data.get("index"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "index là bắt buộc"}), 400
        if not batch_id:
            return jsonify({"ok": False, "error": "batch_id là bắt buộc"}), 400
        if not credentials:
            return jsonify({"ok": False, "error": "credentials là bắt buộc để thử lại"}), 400
        try:
            retry_items = parse_twofa_change_inputs(credentials)
            if len(retry_items) > _MAX_ITEMS:
                raise ValueError(f"maximum {_MAX_ITEMS} accounts per request")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        with _progress_lock:
            batch = _twofa_progress.get(batch_id)
            rows = batch.get("results") if batch else None
            emails = batch.get("emails") if batch else None
            if batch is None or not isinstance(rows, list) or not isinstance(emails, list):
                return jsonify({"ok": False, "error": "Không tìm thấy tiến độ đổi 2FA"}), 404
            if len(retry_items) != len(rows) or len(emails) != len(rows):
                return jsonify({"ok": False, "error": "Danh sách credential không khớp batch 2FA"}), 409
            if index < 0 or index >= len(rows) or index >= len(retry_items):
                return jsonify({"ok": False, "error": "index không hợp lệ"}), 400
            row = rows[index]
            if batch.get("status") not in {"completed", "failed"}:
                return jsonify({"ok": False, "error": "Batch vẫn đang xử lý, hãy chờ hoàn tất trước khi thử lại"}), 409
            if row.get("status") not in {"failed", "partial_failure"}:
                return jsonify({"ok": False, "error": "Tài khoản này chưa ở trạng thái lỗi để thử lại"}), 409
            if row.get("retryable") is False:
                return jsonify({"ok": False, "error": "Lỗi này cần đối soát trạng thái tài khoản, không thể tự động thử lại"}), 409
            if str(emails[index]).casefold() != retry_items[index].email.casefold():
                return jsonify({"ok": False, "error": "Credential không khớp tài khoản cần thử lại"}), 409
            item = retry_items[index]
            row["status"] = "running"
            row["detail"] = "Đang đăng nhập và thử lại đổi 2FA"
            batch["status"] = "running"
            batch["batch_error"] = None
        try:
            _start_twofa_retry(batch_id, index, item)
        except Exception as exc:
            logger.exception("启动 2FA 手动重试失败: batch=%s index=%s", batch_id, index)
            with _progress_lock:
                batch = _twofa_progress.get(batch_id)
                if batch:
                    row = (batch.get("results") or [])[index]
                    row["status"] = "failed"
                    row["detail"] = "无法启动手动重试"
                    batch["status"] = "completed"
                    batch["batch_error"] = f"无法启动手动重试: {type(exc).__name__}"
            return jsonify({"ok": False, "error": "无法启动手动重试"}), 500
        return jsonify(_twofa_progress_snapshot(batch_id)), 202

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
