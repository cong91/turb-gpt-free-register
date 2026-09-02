"""WebUI endpoints for rotating-proxy inventory and lane leases."""
from __future__ import annotations

import logging

from flask import jsonify

from core.rotating_proxy_manager import get_rotating_proxy_manager

logger = logging.getLogger(__name__)


def register_rotating_proxy_routes(app) -> None:
    """Register non-secret status and explicit key-refresh endpoints."""

    @app.get("/api/proxy/rotating")
    def api_rotating_proxy_status():
        return jsonify({"ok": True, **get_rotating_proxy_manager().status()})

    @app.post("/api/proxy/rotating/refresh")
    def api_rotating_proxy_refresh():
        try:
            status = get_rotating_proxy_manager().refresh_keys()
        except Exception as exc:
            logger.exception("刷新 proxy.vn keyxoay 列表失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        return jsonify({"ok": True, **status})

