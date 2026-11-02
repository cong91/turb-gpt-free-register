"""Authenticated API and progress lifecycle for password changes."""
from __future__ import annotations

import logging
import threading
import uuid

from flask import jsonify, request

from core import db
from core.browser_password_change import run_password_change_batch
from core.password_change import parse_password_change_inputs
from webui.personal_info_progress import (
    _MAX_PROGRESS_BATCHES, _PASSWORD_APPLY_STATUSES, _PASSWORD_TERMINAL_STATUSES,
    _batch_is_terminal, _force_fail_rows_locked, _progress_lock, _public_results_for_batch,
)
from webui.personal_info_request import _MAX_ITEMS, _MAX_JSON_BYTES, _same_origin_mutation, _strict_bool
from webui.personal_info_results import _codex_public, _plan_check_note

_password_progress: dict[str, dict] = {}
logger = logging.getLogger(__name__)

def _apply_password_progress(batch_id: str, index: int, update: dict) -> None:
    with _progress_lock:
        batch = _password_progress.get(batch_id)
        if not batch:
            return
        rows = batch.get("results") or []
        if not isinstance(index, int) or index < 0 or index >= len(rows):
            return
        row = rows[index]
        status = str(update.get("status") or "running").strip().lower()
        if status not in _PASSWORD_APPLY_STATUSES:
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
        if isinstance(update.get("codex"), dict):
            row["codex"] = _codex_public(update["codex"])
        result = update.get("result")
        if isinstance(result, dict):
            safe = _public_password_result(result)
            for key in ("error", "warning", "plan_check", "retryable", "codex"):
                row.pop(key, None)
            public_results = batch.get("public_results")
            if isinstance(public_results, list) and index < len(public_results):
                public_results[index] = dict(safe)
            if isinstance(safe.get("codex"), dict):
                row["codex"] = _codex_public(safe["codex"])
            for key in ("account_id", "email", "mode", "change_status", "error", "warning", "plan_check", "retryable"):
                if key in safe and safe[key] not in (None, ""):
                    row[key] = safe[key]
            row["status"] = str(safe.get("change_status") or status)
            result_detail = safe.get("error") or safe.get("warning")
            if result_detail:
                row["detail"] = str(result_detail).strip()[:500]
            elif row["status"] == "success":
                plan_note = _plan_check_note(safe.get("plan_check"))
                row["detail"] = f"Đã đổi mật khẩu và lưu vào dữ liệu tài khoản.{plan_note}"


def _create_password_progress(batch_id: str, items: list, login_codex_after_change: bool) -> bool:
    with _progress_lock:
        _trim_password_progress_locked(target_size=_MAX_PROGRESS_BATCHES - 1)
        if len(_password_progress) >= _MAX_PROGRESS_BATCHES:
            return False
        _password_progress[batch_id] = {
            "batch_id": batch_id,
            "status": "running",
            "created_at": uuid.uuid1().time,
            "submitted": len(items),
            "login_codex_after_change": login_codex_after_change,
            # Chỉ giữ email: credential không được lưu lại trong progress dict,
            # retry dựng lại input từ email (mật khẩu cũ/tOTP lấy từ DB).
            "emails": [item.email for item in items],
            "public_results": [None] * len(items),
            "results": [
                {
                    "index": index,
                    "email": item.email,
                    "status": "queued",
                    "detail": "Đang chờ luồng xử lý",
                    **({"codex": {"status": "waiting_change", "ok": False}} if login_codex_after_change else {}),
                }
                for index, item in enumerate(items)
            ],
        }
        _trim_password_progress_locked()
        return True


def _finish_password_progress(batch_id: str, results: list[dict]) -> None:
    for index, result in enumerate(results):
        safe = _public_password_result(result)
        _apply_password_progress(
            batch_id,
            index,
            {
                "status": str(safe.get("change_status") or "failed"),
                "email": result.get("email"),
                "result": result,
            },
        )
    _settle_password_batch(batch_id)
    _persist_password_progress_batch(
        batch_id,
        [_public_password_result(result) for result in results],
    )


def _password_progress_snapshot(batch_id: str) -> dict | None:
    with _progress_lock:
        batch = _password_progress.get(batch_id)
        if not batch:
            return None
        rows = [dict(row) for row in (batch.get("results") or []) if isinstance(row, dict)]
        terminal = sum(1 for row in rows if row.get("status") in _PASSWORD_TERMINAL_STATUSES)
        succeeded = sum(1 for row in rows if row.get("status") == "success")
        failed = sum(1 for row in rows if row.get("status") == "failed")
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
            "batch_error": batch.get("batch_error"),
        }


def _password_row_succeeded(result: dict) -> bool:
    return bool(
        result.get("ok")
        and result.get("persisted", True)
        and result.get("access_token_saved", True)
    )


def _persist_password_progress_batch(batch_id: str, public_results: list[dict]) -> None:
    succeeded = sum(1 for result in public_results if result.get("change_status") == "success")
    try:
        batch = db.save_personal_info_change_batch(batch_id, "password", public_results)
    except Exception as exc:
        logger.exception("保存密码修改批次失败: batch=%s", batch_id)
        with _progress_lock:
            current = _password_progress.get(batch_id)
            if current:
                current["status"] = "failed"
                current["batch_error"] = f"无法保存本次变更记录: {type(exc).__name__}"
                current["change_batch_id"] = None
                current["exportable_count"] = 0
        return
    with _progress_lock:
        current = _password_progress.get(batch_id)
        if current:
            current["status"] = "completed"
            current["change_batch_id"] = batch["batch_id"]
            current["exportable_count"] = batch["exportable_count"]
            current["succeeded"] = succeeded


def _public_password_result(result: dict) -> dict:
    safe = _redact_password_result(result)
    if isinstance(safe.get("codex"), dict):
        safe["codex"] = _codex_public(safe["codex"])
    if safe.get("ok") and safe.get("already_set"):
        # already_set: OpenAI refused the change because a password already
        # exists; the generated value was never applied or persisted.
        safe["change_status"] = "success"
        if not safe.get("warning"):
            safe["warning"] = "Mật khẩu đã tồn tại từ trước"
    elif _password_row_succeeded(safe):
        safe["change_status"] = "success"
    else:
        safe["change_status"] = "failed"
    return safe


def _redact_password_result(result: dict) -> dict:
    """Remove generated/credential secrets before a result crosses the API."""
    safe = dict(result)
    for key in ("new_password", "current_password", "totp_secret", "access_token"):
        safe.pop(key, None)
    return safe


def _run_password_progress_batch(batch_id: str, items: list, workers: int) -> None:
    try:
        with _progress_lock:
            batch = _password_progress.get(batch_id) or {}
            login_codex_after_change = bool(batch.get("login_codex_after_change"))
        results = run_password_change_batch(
            items,
            workers=workers,
            progress_callback=lambda index, update: _apply_password_progress(batch_id, index, update),
            login_codex_after_change=login_codex_after_change,
        )
        _finish_password_progress(batch_id, results)
    except Exception:
        logger.exception("Password change batch failed: batch=%s", batch_id)
        with _progress_lock:
            current = _password_progress.get(batch_id)
            if current:
                current["status"] = "failed"
                current["batch_error"] = "Xử lý batch đổi mật khẩu thất bại, hãy thử lại"
                for row in current.get("results") or []:
                    if row.get("status") not in _PASSWORD_TERMINAL_STATUSES:
                        row["status"] = "failed"
                        row["detail"] = "Xử lý batch đổi mật khẩu thất bại, hãy thử lại"


def _run_password_retry(batch_id: str, index: int, item) -> None:
    try:
        with _progress_lock:
            batch = _password_progress.get(batch_id) or {}
            login_codex_after_change = bool(batch.get("login_codex_after_change"))
        results = run_password_change_batch(
            [item],
            workers=1,
            progress_callback=lambda _worker_index, update: _apply_password_progress(
                batch_id,
                index,
                update,
            ),
            login_codex_after_change=login_codex_after_change,
        )
        result = results[0] if results else {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "error": "Password retry returned no result",
        }
        safe = _public_password_result(result)
        _apply_password_progress(
            batch_id,
            index,
            {
                "status": str(safe.get("change_status") or "failed"),
                "email": item.email,
                "result": result,
            },
        )
    except Exception:
        logger.exception("Password manual retry failed: batch=%s index=%s", batch_id, index)
        failure = {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "error": "Thử lại đổi mật khẩu thất bại, hãy thử lại thủ công",
        }
        _apply_password_progress(batch_id, index, {"status": "failed", "result": failure})
        _settle_password_batch(batch_id)
        if _batch_is_terminal(_password_progress, batch_id, _PASSWORD_TERMINAL_STATUSES):
            current = None
            with _progress_lock:
                current = _password_progress.get(batch_id)
                if current:
                    current["batch_error"] = "Thử lại đổi mật khẩu thất bại"
            public_results = _public_results_for_batch(_password_progress, batch_id, missing_error="Password change did not return a result")
            if public_results is not None:
                _persist_password_progress_batch(batch_id, public_results)
        return
    _settle_password_batch(batch_id)
    public_results = (
        _public_results_for_batch(_password_progress, batch_id, missing_error="Password change did not return a result")
        if _batch_is_terminal(_password_progress, batch_id, _PASSWORD_TERMINAL_STATUSES)
        else None
    )
    if public_results is not None:
        _persist_password_progress_batch(batch_id, public_results)


def _run_password_retry_failed_batch(batch_id: str, row_indexes: list[int], items: list, workers: int) -> None:
    """Re-run every failed password row under the configured worker count."""
    row_by_position = {position: row_index for position, row_index in enumerate(row_indexes)}
    with _progress_lock:
        batch = _password_progress.get(batch_id) or {}
        login_codex_after_change = bool(batch.get("login_codex_after_change"))
    try:
        results = run_password_change_batch(
            items,
            workers=workers,
            progress_callback=lambda position, update: _apply_password_progress(
                batch_id,
                row_by_position.get(position),
                update,
            ),
            login_codex_after_change=login_codex_after_change,
        )
    except Exception:
        logger.exception("Password retry-failed batch crashed: batch=%s", batch_id)
        results = []
    for position, result in enumerate(results):
        safe = _public_password_result(result)
        _apply_password_progress(
            batch_id,
            row_by_position.get(position),
            {
                "status": str(safe.get("change_status") or "failed"),
                "email": result.get("email"),
                "result": result,
            },
        )
    with _progress_lock:
        batch = _password_progress.get(batch_id)
        if batch:
            _force_fail_rows_locked(
                batch.get("results") or [],
                row_indexes,
                "Thử lại đổi mật khẩu thất bại, hãy thử lại thủ công",
            )
    _settle_password_batch(batch_id)
    if _batch_is_terminal(_password_progress, batch_id, _PASSWORD_TERMINAL_STATUSES):
        public_results = _public_results_for_batch(
            _password_progress,
            batch_id,
            missing_error="Password change did not return a result",
        )
        if public_results is not None:
            _persist_password_progress_batch(batch_id, public_results)


def _settle_password_batch(batch_id: str) -> None:
    with _progress_lock:
        current = _password_progress.get(batch_id)
        if not current:
            return
        rows = current.get("results") or []
        if rows and all(row.get("status") in _PASSWORD_TERMINAL_STATUSES for row in rows):
            current["status"] = "completed"


def _start_password_progress_batch(batch_id: str, items: list, workers: int) -> None:
    thread = threading.Thread(
        target=_run_password_progress_batch,
        args=(batch_id, items, workers),
        name=f"password-change-batch-{batch_id[:8]}",
        daemon=True,
    )
    thread.start()


def _start_password_retry(batch_id: str, index: int, item) -> None:
    thread = threading.Thread(
        target=_run_password_retry,
        args=(batch_id, index, item),
        name=f"password-change-retry-{batch_id[:8]}-{index}",
        daemon=True,
    )
    thread.start()


def _start_password_retry_failed(batch_id: str, row_indexes: list[int], items: list, workers: int) -> None:
    thread = threading.Thread(
        target=_run_password_retry_failed_batch,
        args=(batch_id, row_indexes, items, workers),
        name=f"password-change-retry-failed-{batch_id[:8]}",
        daemon=True,
    )
    thread.start()


def _trim_password_progress_locked(target_size: int = _MAX_PROGRESS_BATCHES) -> None:
    if len(_password_progress) <= target_size:
        return
    ordered = sorted(
        _password_progress.items(),
        key=lambda pair: str(pair[1].get("created_at") or ""),
    )
    for batch_id, batch in ordered:
        if len(_password_progress) <= target_size:
            break
        if batch.get("status") == "running":
            continue
        _password_progress.pop(batch_id, None)


def register_password_change_routes(app) -> None:
    @app.post("/api/accounts/change-password")
    def api_accounts_change_password():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        try:
            items = parse_password_change_inputs(str(data.get("credentials") or ""))
            if len(items) > _MAX_ITEMS:
                raise ValueError(f"maximum {_MAX_ITEMS} accounts per request")
            workers = max(1, min(4, int(data.get("workers", 1) or 1)))
            login_codex_after_change = _strict_bool(data, "login_codex_after_change")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        batch_id = uuid.uuid4().hex
        if not _create_password_progress(batch_id, items, login_codex_after_change):
            return jsonify({"ok": False, "error": "Đang có quá nhiều batch đổi mật khẩu, hãy chờ batch cũ hoàn tất"}), 429
        try:
            _start_password_progress_batch(batch_id, items, workers)
        except Exception as exc:
            logger.exception("启动密码修改批次失败: batch=%s", batch_id)
            with _progress_lock:
                job = _password_progress.get(batch_id)
                if job:
                    job["status"] = "failed"
                    job["batch_error"] = f"无法启动处理线程: {type(exc).__name__}"
                    for row in job.get("results") or []:
                        if row.get("status") not in _PASSWORD_TERMINAL_STATUSES:
                            row["status"] = "failed"
                            row["detail"] = "无法启动批量处理线程，请逐条重试"
            return jsonify({"ok": False, "error": "无法启动密码修改任务", "batch_id": batch_id}), 500
        snapshot = _password_progress_snapshot(batch_id) or {
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

    @app.get("/api/accounts/change-password-status")
    def api_accounts_change_password_status():
        batch_id = str(request.args.get("batch_id") or "").strip()
        if not batch_id:
            return jsonify({"ok": False, "error": "batch_id là bắt buộc"}), 400
        snapshot = _password_progress_snapshot(batch_id)
        if snapshot is None:
            return jsonify({"ok": False, "error": "Không tìm thấy tiến độ đổi mật khẩu"}), 404
        return jsonify(snapshot)

    @app.post("/api/accounts/change-password-retry")
    def api_accounts_change_password_retry():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        batch_id = str(data.get("batch_id") or "").strip()
        try:
            index = int(data.get("index"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "index là bắt buộc"}), 400
        if not batch_id:
            return jsonify({"ok": False, "error": "batch_id là bắt buộc"}), 400
        with _progress_lock:
            batch = _password_progress.get(batch_id)
            rows = batch.get("results") if batch else None
            emails = batch.get("emails") if batch else None
            if batch is None or not isinstance(rows, list) or not isinstance(emails, list):
                return jsonify({"ok": False, "error": "Không tìm thấy tiến độ đổi mật khẩu"}), 404
            if len(emails) != len(rows):
                return jsonify({"ok": False, "error": "Batch đổi mật khẩu không nhất quán"}), 409
            if index < 0 or index >= len(rows):
                return jsonify({"ok": False, "error": "index không hợp lệ"}), 400
            row = rows[index]
            if row.get("status") in {"queued", "running"}:
                return jsonify({"ok": False, "error": "Tài khoản này đang xử lý, hãy chờ hoàn tất trước khi thử lại"}), 409
            if row.get("status") not in {"failed"}:
                return jsonify({"ok": False, "error": "Tài khoản này chưa ở trạng thái lỗi để thử lại"}), 409
            if row.get("retryable") is False:
                return jsonify({"ok": False, "error": "Lỗi này cần đối soát trạng thái tài khoản, không thể tự động thử lại"}), 409
            # Credential không được giữ lại trong progress dict: dựng lại input
            # từ email, run_password_change sẽ resolve mật khẩu cũ/tOTP từ DB.
            item = parse_password_change_inputs(str(emails[index]))[0]
            row["status"] = "running"
            row["detail"] = "Đang đăng nhập và thử lại đổi mật khẩu"
            batch["status"] = "running"
            batch["batch_error"] = None
        try:
            _start_password_retry(batch_id, index, item)
        except Exception as exc:
            logger.exception("启动密码修改手动重试失败: batch=%s index=%s", batch_id, index)
            with _progress_lock:
                batch = _password_progress.get(batch_id)
                if batch:
                    row = (batch.get("results") or [])[index]
                    row["status"] = "failed"
                    row["detail"] = "无法启动手动重试"
                    batch["status"] = (
                        "completed" if all(
                            entry.get("status") in _PASSWORD_TERMINAL_STATUSES
                            for entry in batch.get("results") or []
                        ) else "running"
                    )
                    batch["batch_error"] = f"无法启动手动重试: {type(exc).__name__}"
            return jsonify({"ok": False, "error": "无法启动手动重试"}), 500
        return jsonify(_password_progress_snapshot(batch_id)), 202

    @app.post("/api/accounts/change-password-retry-failed")
    def api_accounts_change_password_retry_failed():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求数据必须是对象"}), 400
        batch_id = str(data.get("batch_id") or "").strip()
        try:
            workers = max(1, min(4, int(data.get("workers", 1) or 1)))
        except (TypeError, ValueError):
            workers = 1
        if not batch_id:
            return jsonify({"ok": False, "error": "batch_id là bắt buộc"}), 400
        with _progress_lock:
            batch = _password_progress.get(batch_id)
            rows = batch.get("results") if batch else None
            emails = batch.get("emails") if batch else None
            if batch is None or not isinstance(rows, list) or not isinstance(emails, list):
                return jsonify({"ok": False, "error": "Không tìm thấy tiến độ đổi mật khẩu"}), 404
            if len(emails) != len(rows):
                return jsonify({"ok": False, "error": "Batch đổi mật khẩu không nhất quán"}), 409
            failed: list[tuple[int, object]] = []
            for index, row in enumerate(rows):
                if row.get("status") not in {"failed"}:
                    continue
                if row.get("retryable") is False:
                    continue
                # Credential không được giữ lại trong progress dict: dựng lại input
                # từ email, run_password_change sẽ resolve mật khẩu cũ/tOTP từ DB.
                failed.append((index, parse_password_change_inputs(str(emails[index]))[0]))
            if not failed:
                return jsonify({"ok": False, "error": "Không có tài khoản lỗi đủ điều kiện thử lại"}), 409
            row_indexes = [index for index, _item in failed]
            items = [item for _index, item in failed]
            for index in row_indexes:
                rows[index]["status"] = "running"
                rows[index]["detail"] = "Đang đăng nhập và thử lại đổi mật khẩu"
            batch["status"] = "running"
            batch["batch_error"] = None
        try:
            _start_password_retry_failed(batch_id, row_indexes, items, workers)
        except Exception as exc:
            logger.exception("启动密码重试失败批次: batch=%s", batch_id)
            with _progress_lock:
                batch = _password_progress.get(batch_id)
                if batch:
                    rows = batch.get("results") or []
                    _force_fail_rows_locked(rows, row_indexes, "无法启动手动重试")
                    batch["status"] = (
                        "completed" if all(row.get("status") in _PASSWORD_TERMINAL_STATUSES for row in rows)
                        else "running"
                    )
                    batch["batch_error"] = f"无法启动手动重试: {type(exc).__name__}"
            return jsonify({"ok": False, "error": "无法启动手动重试"}), 500
        return jsonify(_password_progress_snapshot(batch_id)), 202
