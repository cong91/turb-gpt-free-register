# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import jsonify, request

from core.reserved_test_aliases import (
    ReservedTestAliasError,
    generate_reserved_test_aliases,
)


def reserved_test_alias_preview_payload(data: dict) -> tuple[dict, int]:
    if not isinstance(data, dict):
        return {"ok": False, "error": "Dữ liệu yêu cầu không hợp lệ"}, 400

    domains = data.get("domains")
    if not isinstance(domains, list):
        return {"ok": False, "error": "Tên miền kiểm thử phải được gửi dưới dạng danh sách"}, 400

    try:
        aliases = generate_reserved_test_aliases(
            data.get("base"),
            domains,
            limit=data.get("limit", 6),
        )
    except ReservedTestAliasError as exc:
        return {"ok": False, "error": str(exc)}, 400

    return {
        "ok": True,
        "aliases": aliases,
        "count": len(aliases),
    }, 200


def register_reserved_test_alias_routes(app) -> None:
    @app.post("/api/tools/reserved-test-aliases/preview")
    def api_reserved_test_alias_preview():
        payload, status = reserved_test_alias_preview_payload(
            request.get_json(silent=True)
        )
        return jsonify(payload), status
