# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import jsonify


def register_nordvpn_rotation_routes(app) -> None:
    @app.post("/api/nordvpn/rotation/retry")
    def api_nordvpn_rotation_retry():
        from core import registration_service

        result = registration_service.retry_pending_nordvpn_rotation()
        status = int(result.pop("status", 200 if result.get("ok") else 400))
        return jsonify(result), status
