"""Authenticated WebUI routes for the independent Roxy profile manager."""
from __future__ import annotations

import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from flask import jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from config import roxy_profile_manager as _manager_cfg
from core.roxy_profile_manager import (
    RoxyProfileManager,
    RoxyProfileManagerError,
    RoxyProfileManagerNotFound,
    RoxyProfileManagerStateError,
    RoxyProfileManagerUpstreamError,
)
from core.roxy_profile_store import RoxyProfileConflict, RoxyProfileStoreError
from webui.auth import code_is_valid

_MAX_JSON_BYTES = 64 * 1024
_CREATE_FIELDS = {
    "name", "os", "osVersion", "coreType", "coreVersion", "projectId",
    "proxyInfo", "fingerInfo", "defaultOpenUrl", "windowPlatformList", "randomFingerprint",
    "syncBookmark", "syncHistory", "syncTab", "syncCookie", "syncExtensions",
    "syncPassword", "syncIndexedDb", "syncLocalStorage",
}
_UPDATE_FIELDS = _CREATE_FIELDS - {"projectId"}


def _payload() -> tuple[dict, tuple[dict, int] | None]:
    if request.content_length and request.content_length > _MAX_JSON_BYTES:
        return {}, ({"ok": False, "error": "请求体过大"}, 400)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}, ({"ok": False, "error": "请求数据必须是对象"}, 400)
    unknown = sorted(set(data) - _CREATE_FIELDS)
    if unknown:
        return {}, ({"ok": False, "error": "包含不支持的字段"}, 400)
    return data, None


def _save_upload_limited(upload, handle, *, max_bytes: int) -> int:
    written = 0
    while True:
        chunk = upload.stream.read(1024 * 1024)
        if not chunk:
            return written
        written += len(chunk)
        if written > max_bytes:
            raise RequestEntityTooLarge()
        handle.write(chunk)


def _same_origin_mutation() -> bool:
    origin = request.headers.get("Origin")
    if origin:
        expected = f"{request.scheme}://{request.host}"
        return secrets.compare_digest(origin.rstrip("/"), expected.rstrip("/"))
    referer = request.headers.get("Referer", "")
    if referer:
        return referer.startswith(f"{request.scheme}://{request.host}/")
    if request.headers.get("X-Auth-Code") or request.headers.get("X-Authorization-Code"):
        return True
    auth = request.headers.get("Authorization", "")
    return auth.lower().startswith("bearer ") and code_is_valid(auth[7:].strip())


def _error_response(exc: Exception):
    if isinstance(exc, RoxyProfileManagerNotFound):
        return jsonify({"ok": False, "error": str(exc)}), 404
    if isinstance(exc, (RoxyProfileManagerStateError, RoxyProfileConflict)):
        return jsonify({"ok": False, "error": str(exc)}), 409
    if isinstance(exc, RoxyProfileManagerUpstreamError):
        return jsonify({"ok": False, "error": "RoxyBrowser 操作失败，请刷新状态后重试"}), 502
    if isinstance(exc, (RoxyProfileManagerError, RoxyProfileStoreError)):
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": False, "error": "Trình quản lý profile Roxy hiện không khả dụng"}), 503


def register_roxy_profile_routes(app) -> None:
    def manager() -> RoxyProfileManager:
        return RoxyProfileManager()

    @app.get("/api/roxy/profiles")
    def api_roxy_profiles():
        try:
            reconcile = request.args.get("reconcile", "0").lower() in {"1", "true", "yes"}
            try:
                page = max(1, int(request.args.get("page", "1")))
                page_size = max(1, min(200, int(request.args.get("page_size", "50"))))
            except ValueError:
                return jsonify({"ok": False, "error": "分页参数无效"}), 400
            search = request.args.get("search", "")
            state = request.args.get("state", "")
            current_manager = manager()
            profiles = current_manager.list_profiles(
                reconcile=reconcile,
                search=search,
                state=state,
                page=page,
                page_size=page_size,
            )
            total = current_manager.count_profiles(search=search, state=state)
            return jsonify({
                "ok": True,
                "profiles": profiles,
                "status": current_manager.status(include_remote=reconcile),
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": page * page_size < total,
            })
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles")
    def api_roxy_profile_create():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        data, error = _payload()
        if error:
            return jsonify(error[0]), error[1]
        try:
            result = manager().create_managed_profile(
                data,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
            return jsonify({"ok": True, "profile": result}), 201
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.get("/api/roxy/profiles/<local_id>")
    def api_roxy_profile_get(local_id: str):
        try:
            return jsonify({"ok": True, "profile": manager().get_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.patch("/api/roxy/profiles/<local_id>")
    def api_roxy_profile_update(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        data, error = _payload()
        if error:
            return jsonify(error[0]), error[1]
        if set(data) - _UPDATE_FIELDS:
            return jsonify({"ok": False, "error": "包含不支持的更新字段"}), 400
        try:
            return jsonify({"ok": True, "profile": manager().update_managed_profile(local_id, data)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/open")
    def api_roxy_profile_open(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, **manager().open_managed_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/close")
    def api_roxy_profile_close(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, "profile": manager().close_managed_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/export")
    def api_roxy_profile_export(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, **manager().export_managed_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/export-full")
    def api_roxy_profile_export_full(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, **manager().export_full_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/open-local")
    def api_roxy_profile_open_local(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, **manager().open_offline_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/<local_id>/close-local")
    def api_roxy_profile_close_local(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, **manager().close_offline_profile(local_id)})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/import")
    def api_roxy_profile_import():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        max_bytes = int(_manager_cfg.ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES)
        request.max_content_length = max_bytes + 1024 * 1024
        if request.content_length and request.content_length > request.max_content_length:
            return jsonify({"ok": False, "error": "Archive quá lớn"}), 413
        try:
            upload = request.files.get("archive")
            name = (request.form.get("name") or "").strip()
        except RequestEntityTooLarge:
            return jsonify({"ok": False, "error": "Archive quá lớn"}), 413
        if upload is None or not name:
            return jsonify({"ok": False, "error": "Cần archive và tên profile"}), 400
        temporary = None
        try:
            with NamedTemporaryFile(prefix="roxy-import-", suffix=".rpa2", delete=False) as handle:
                temporary = Path(handle.name)
                _save_upload_limited(upload, handle, max_bytes=max_bytes)
            result = manager().import_full_profile(temporary, display_name=name)
            return jsonify({"ok": True, **result}), 201
        except RequestEntityTooLarge:
            return jsonify({"ok": False, "error": "Archive quá lớn"}), 413
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @app.post("/api/roxy/profiles/<local_id>/archive")
    def api_roxy_profile_archive(local_id: str):
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({
                "ok": True,
                "profile": manager().archive_managed_profile(
                    local_id,
                    idempotency_key=request.headers.get("Idempotency-Key"),
                ),
            })
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/bulk")
    def api_roxy_profiles_bulk():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        if request.content_length and request.content_length > _MAX_JSON_BYTES:
            return jsonify({"ok": False, "error": "请求体过大"}), 400
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("local_ids"), list):
            return jsonify({"ok": False, "error": "批量请求格式无效"}), 400
        try:
            result = manager().bulk_action(data["local_ids"], str(data.get("action") or ""))
            return jsonify({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/roxy/profiles/reconcile")
    def api_roxy_profiles_reconcile():
        if not _same_origin_mutation():
            return jsonify({"ok": False, "error": "请求来源不受信任"}), 403
        try:
            return jsonify({"ok": True, "result": manager().reconcile_profiles()})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.get("/api/roxy/profiles/<local_id>/local-status")
    def api_roxy_profile_local_status(local_id: str):
        try:
            profile = manager().get_profile(local_id)
            return jsonify({
                "ok": True,
                "profile": profile,
                "connection": profile.get("launch"),
                "capability": "browser_state_only",
                "fingerprint_status": (profile.get("launch") or {}).get("fingerprint_status", "unknown"),
            })
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.get("/api/roxy/profiles/<local_id>/archive/download")
    def api_roxy_profile_archive_download(local_id: str):
        try:
            path = manager().archive_path(local_id)
            return send_file(
                path,
                as_attachment=True,
                download_name=f"roxy-profile-{local_id}.rpa2" if path.suffix == ".rpa2" else f"roxy-profile-{local_id}.rpa",
                mimetype="application/octet-stream",
                max_age=0,
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
