"""Machine-to-machine endpoints used by the sub2api account coordinator."""

from __future__ import annotations

from urllib.parse import urlparse

from flask import jsonify, request

from config import codex as codex_config
from core import db, registration_service
from core.email_provider import normalize_email_source
from core.registration_limits import MAX_REGISTRATION_TASKS
from core.registration_service import submit_codex_retry_for_account
from webui import registration_jobs_api

_PROVISION_FIELDS = frozenset(
    {"request_id", "count", "workers", "email_source", "callback_url"}
)
_REAUTHORIZE_FIELDS = frozenset({"request_id", "account_id", "email", "callback_url"})
_REAUTH_CALLBACK_PATH = (
    "/api/v1/integrations/openai/auto-provision/reauthorization/callback"
)


def _json_object() -> dict | None:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else None


def _reject_unknown_fields(
    payload: dict, allowed: frozenset[str]
) -> tuple[dict, int] | None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        return {"ok": False, "error": f"unknown fields: {', '.join(unknown)}"}, 400
    return None


def _required_text(
    payload: dict, key: str, *, max_length: int = 256
) -> tuple[str | None, tuple[dict, int] | None]:
    value = payload.get(key)
    if not isinstance(value, str):
        return None, ({"ok": False, "error": f"{key} must be a string"}, 400)
    value = value.strip()
    if not value or len(value) > max_length or any(ord(char) < 32 for char in value):
        return None, ({"ok": False, "error": f"{key} is invalid"}, 400)
    return value, None


def _optional_text(
    payload: dict, key: str, *, max_length: int = 256
) -> tuple[str | None, tuple[dict, int] | None]:
    if key not in payload or payload[key] is None:
        return None, None
    return _required_text(payload, key, max_length=max_length)


def _positive_int(
    payload: dict, key: str, *, minimum: int, maximum: int
) -> tuple[int | None, tuple[dict, int] | None]:
    value = payload.get(key)
    if isinstance(value, bool):
        return None, ({"ok": False, "error": f"{key} must be an integer"}, 400)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, ({"ok": False, "error": f"{key} must be an integer"}, 400)
    if not minimum <= parsed <= maximum:
        return None, (
            {"ok": False, "error": f"{key} must be between {minimum} and {maximum}"},
            400,
        )
    return parsed, None


def _validate_callback_url(value: str) -> tuple[str | None, tuple[dict, int] | None]:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None, (
            {
                "ok": False,
                "error": (
                    "callback_url must be an absolute HTTP(S) URL without "
                    "credentials or query"
                ),
            },
            400,
        )
    return value, None


def _error_response(error: tuple[dict, int] | None):
    return jsonify(error[0]), error[1] if error else 400


def register_sub2api_automation_routes(app) -> None:
    @app.post("/api/automation/provision")
    def api_sub2api_automation_provision():
        payload = _json_object()
        if payload is None:
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        unknown = _reject_unknown_fields(payload, _PROVISION_FIELDS)
        if unknown:
            return jsonify(unknown[0]), unknown[1]
        request_id, error = _required_text(payload, "request_id", max_length=128)
        if error:
            return _error_response(error)
        count, error = _positive_int(
            payload, "count", minimum=1, maximum=MAX_REGISTRATION_TASKS
        )
        if error:
            return _error_response(error)
        workers, error = _positive_int(payload, "workers", minimum=1, maximum=16)
        if error:
            return _error_response(error)
        email_source, error = _optional_text(payload, "email_source", max_length=64)
        if error:
            return _error_response(error)
        callback_url, error = _required_text(payload, "callback_url", max_length=2048)
        if error:
            return _error_response(error)
        callback_url, error = _validate_callback_url(callback_url)
        if error:
            return _error_response(error)
        if email_source and normalize_email_source(email_source) == "local_test":
            return jsonify(
                {"ok": False, "error": "automation registration cannot use local_test"}
            ), 400

        existing = db.list_jobs_for_automation_request(request_id)
        if existing:
            return jsonify(
                {
                    "ok": True,
                    "accepted": True,
                    "request_id": request_id,
                    "submitted": len(existing),
                    "idempotent_replay": True,
                }
            ), 202

        result, status_code = registration_jobs_api.create_registration_jobs(
            {
                "request_id": request_id,
                "count": count,
                "workers": workers,
                "email_source": email_source,
            },
            service=registration_service,
            database=db,
            automation_context={
                "sub2api_automation_request_id": request_id,
                "sub2api_automation_kind": "registration",
                "sub2api_callback_url": callback_url,
            },
        )
        if status_code != 200:
            return jsonify(result), status_code
        return jsonify(
            {
                "ok": True,
                "accepted": True,
                "request_id": request_id,
                "submitted": int(result.get("submitted") or 0),
            }
        ), 202

    @app.post("/api/automation/reauthorize")
    def api_sub2api_automation_reauthorize():
        payload = _json_object()
        if payload is None:
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        unknown = _reject_unknown_fields(payload, _REAUTHORIZE_FIELDS)
        if unknown:
            return jsonify(unknown[0]), unknown[1]
        request_id, error = _required_text(payload, "request_id", max_length=128)
        if error:
            return _error_response(error)
        account_id, error = _positive_int(
            payload, "account_id", minimum=1, maximum=2**63 - 1
        )
        if error:
            return _error_response(error)
        email, error = _required_text(payload, "email", max_length=320)
        if error:
            return _error_response(error)
        callback_url, error = _required_text(payload, "callback_url", max_length=2048)
        if error:
            return _error_response(error)
        callback_url, error = _validate_callback_url(callback_url)
        if error:
            return _error_response(error)
        if (
            str(getattr(codex_config, "CODEX_AUTH_URL_SOURCE", "") or "")
            .strip()
            .lower()
            != "sub2"
        ):
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "CODEX_AUTH_URL_SOURCE must be sub2 for automation "
                        "reauthorization"
                    ),
                }
            ), 409

        account = db.get_account_by_email(email)
        if account is None:
            return jsonify({"ok": False, "error": "account email not found"}), 404
        if int(account.get("id") or 0) != account_id:
            return jsonify(
                {
                    "ok": False,
                    "error": "account_id and email do not identify the same account",
                }
            ), 409
        account_email = str(account.get("email") or "").strip()
        if not account_email or account_email.casefold() != email.casefold():
            return jsonify({"ok": False, "error": "account email mismatch"}), 409
        access_token = str(account.get("access_token") or "").strip()
        if not access_token:
            return jsonify({"ok": False, "error": "account has no access token"}), 409

        existing = db.list_jobs_for_automation_request(request_id)
        if existing:
            return jsonify(
                {
                    "ok": True,
                    "accepted": True,
                    "request_id": request_id,
                    "job_id": int(existing[0].get("id") or 0),
                    "account_id": account_id,
                    "idempotent_replay": True,
                }
            ), 202

        result = submit_codex_retry_for_account(
            account_id=int(account["id"]),
            email=account_email,
            access_token=access_token,
            trigger="sub2api_auto_reauthorize",
            automation_context={
                "sub2api_automation_request_id": request_id,
                "sub2api_automation_kind": "reauthorization",
                "sub2api_callback_url": callback_url,
                "sub2api_callback_path": _REAUTH_CALLBACK_PATH,
                "sub2api_callback_event_id": (
                    f"{request_id}:reauthorization:oauth-callback"
                ),
                "sub2api_account_id": str(account_id),
                "sub2api_automation_email": email,
            },
        )
        if not result.get("accepted"):
            status_code = 409 if result.get("busy") or result.get("reason") else 400
            return jsonify(
                {
                    "ok": False,
                    **{key: value for key, value in result.items() if key != "error"},
                    "error": result.get("error")
                    or result.get("reason")
                    or "reauthorization was not accepted",
                }
            ), status_code
        return jsonify(
            {
                "ok": True,
                "accepted": True,
                "request_id": request_id,
                "job_id": result.get("job_id"),
                "account_id": account_id,
            }
        ), 202
