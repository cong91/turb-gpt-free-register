"""Request validation for personal-information change endpoints."""
from __future__ import annotations

import secrets

from flask import request

from webui.auth import code_is_valid

_MAX_JSON_BYTES = 512 * 1024
_MAX_ITEMS = 50


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


def _strict_bool(data: dict, key: str, *, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} phải là boolean")
    return value
