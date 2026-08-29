# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from flask import jsonify


def nordvpn_status_payload(
    nordvpn_cli: Any | None = None,
    registration_service: Any | None = None,
) -> dict:
    """Return NordVPN runtime and registration-gate state for WebUI display."""
    from config import nordvpn as nordvpn_config
    from config import nordvpn_wireguard as wireguard_config

    if nordvpn_cli is None:
        from core import nordvpn_cli as nordvpn_cli
    if registration_service is None:
        from core import registration_service as registration_service

    configured = bool(getattr(nordvpn_config, "NORDVPN_ENABLED", False))
    service_running = bool(nordvpn_cli.is_service_running()) if configured else False
    connected = bool(nordvpn_cli.is_connected()) if configured else False
    ready = bool(configured and service_running and connected)
    rotation = registration_service.nordvpn_rotation_status()
    from core.nordvpn_wireguard import list_active_leases

    wireguard_leases = list_active_leases()
    rotation_pending = bool(rotation.get("rotation_pending"))
    rotation_blocked = bool(
        rotation_pending and rotation.get("gate_state") == "awaiting_confirmation"
    )
    if rotation_blocked:
        message = "NordVPN 自动轮换失败，注册队列已暂停"
    elif not configured:
        message = "NordVPN 未启用"
    elif ready:
        message = "NordVPN 已连接，NordLynx 可用"
    elif not service_running:
        message = "NordVPN 已启用，但本地服务未运行"
    elif not connected:
        message = "NordVPN 已启用，但 NordLynx 尚未连接"
    else:
        message = "NordVPN 状态未就绪"
    return {
        "ok": True,
        "configured": configured,
        "service_running": service_running,
        "connected": connected,
        "ready": ready,
        "rotation_pending": rotation_pending,
        "rotation_blocked": rotation_blocked,
        "rotation_in_progress": bool(rotation.get("rotation_in_progress")),
        "rotation_error": rotation.get("rotation_error"),
        "rotation_detail": rotation.get("rotation_detail"),
        "gate_state": rotation.get("gate_state"),
        "waiting_jobs": rotation.get("waiting_jobs", 0),
        "wireguard_enabled": bool(getattr(wireguard_config, "NORDVPN_WG_ENABLED", False)),
        "wireguard_active_count": len(wireguard_leases),
        "wireguard_leases": wireguard_leases,
        "message": message,
    }


def register_nordvpn_status_routes(app) -> None:
    @app.get("/api/nordvpn/status")
    def api_nordvpn_status():
        return jsonify(nordvpn_status_payload())
