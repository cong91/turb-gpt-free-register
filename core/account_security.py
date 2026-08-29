"""Account security workflows shared by the personal-information tools."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from core.account_export import BrowserPageTransport, fetch_session, setup_2fa_in_page
from core.codex_login_credentials import CodexLoginCredentials
from core.email_change import _parse_credential_lines_strict

logger = logging.getLogger(__name__)
_MFA_INFO_URL = "https://chatgpt.com/backend-api/accounts/mfa_info"
_MFA_DISABLE_URL = "https://chatgpt.com/backend-api/accounts/mfa/user/disable_in_house"
_BASE32_SECRET_RE = re.compile(r"\b[A-Z2-7]{16,}\b", re.IGNORECASE)
_LOGIN_ATTEMPTS = 2
_MFA_FACTOR_ID_KEYS = frozenset(
    {"factor_id", "factorid", "mfa_factor_id", "mfafactorid", "native_default_factor_id"}
)


@dataclass(frozen=True, slots=True)
class TwofaChangeInput:
    email: str
    password: str = dataclass_field(repr=False)
    current_totp_secret: str = dataclass_field(repr=False)

    def credentials(self) -> CodexLoginCredentials:
        return CodexLoginCredentials(
            email=self.email,
            password=self.password,
            totp_secret=self.current_totp_secret,
        )


def parse_twofa_change_inputs(text: str) -> list[TwofaChangeInput]:
    """Parse ``email|password|current_totp_secret`` lines for a batch."""
    records = _parse_credential_lines_strict(text)
    if not records:
        raise ValueError("2FA credential input is required")

    result: list[TwofaChangeInput] = []
    seen_emails: set[str] = set()
    for email, password, secret in records:
        key = email.casefold()
        if key in seen_emails:
            raise ValueError("2FA source email must be unique")
        seen_emails.add(key)
        result.append(TwofaChangeInput(email, password, secret))
    return result


def _response_detail(response) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return "no response body"
    text = re.sub(r"\s+", " ", text)
    return _BASE32_SECRET_RE.sub("[redacted-secret]", text)[:240]


def _redacted_error(message: object, item: TwofaChangeInput) -> str:
    output = str(message or "")[:400]
    for secret in (item.password, item.current_totp_secret):
        if secret:
            output = output.replace(secret, "[redacted]")
    output = _BASE32_SECRET_RE.sub("[redacted-secret]", output)
    return re.sub(r"\b\d{6,8}\b", "[redacted-code]", output)


def _extract_mfa_factor_id(payload: object) -> str | None:
    """Extract the TOTP factor ID without selecting a passkey or SMS ID."""

    def factor_id_from_entry(entry: object) -> str | None:
        if not isinstance(entry, dict):
            return None
        for key in ("id", "factor_id", "factorId"):
            factor_id = str(entry.get(key) or "").strip()
            if factor_id and not bool(entry.get("is_recovery")):
                return factor_id
        return None

    def factor_id_from_totp_entries(entries: object) -> str | None:
        if isinstance(entries, dict):
            factor_id = factor_id_from_entry(entries)
            if factor_id:
                return factor_id
            entries = list(entries.values())
        if isinstance(entries, list):
            for entry in entries:
                factor_id = factor_id_from_entry(entry)
                if factor_id:
                    return factor_id
        return None

    if not isinstance(payload, dict):
        return None

    factors = payload.get("factors")
    if isinstance(factors, dict):
        factor_id = factor_id_from_totp_entries(factors.get("totp"))
        if factor_id:
            return factor_id
    elif isinstance(factors, list):
        factor_id = factor_id_from_totp_entries(
            [entry for entry in factors if isinstance(entry, dict) and str(entry.get("factor_type") or "").casefold() == "totp"]
        )
        if factor_id:
            return factor_id

    for key, candidate in payload.items():
        normalized_key = str(key).replace("-", "_").casefold()
        if normalized_key in _MFA_FACTOR_ID_KEYS:
            factor_id = str(candidate or "").strip()
            if factor_id:
                return factor_id

    nested_mfa = payload.get("mfa_info") or payload.get("mfa")
    if isinstance(nested_mfa, dict):
        return _extract_mfa_factor_id(nested_mfa)
    return None


def _mfa_request_headers(transport: BrowserPageTransport, access_token: str, path: str) -> dict:
    """Build headers used by ChatGPT's account-scoped MFA routes."""
    headers = transport.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = transport.device_id
    headers["oai-language"] = transport.navigator_language()
    headers["x-openai-target-path"] = path
    headers["x-openai-target-route"] = path
    return headers


def _login_and_get_access_token(driver, item: TwofaChangeInput) -> str:
    """Login again once when the browser session does not expose a token."""
    from core.email_change import _login_chatgpt_with_credentials

    last_error: Exception | None = None
    for attempt in range(1, _LOGIN_ATTEMPTS + 1):
        try:
            _login_chatgpt_with_credentials(driver, item.credentials())
            session = fetch_session(BrowserPageTransport(driver))
            access_token = str(session.get("accessToken") or "").strip()
            if access_token:
                return access_token
            raise RuntimeError("ChatGPT login completed without accessToken")
        except Exception as exc:  # noqa: BLE001 - retry the complete login boundary once.
            last_error = exc
            if attempt < _LOGIN_ATTEMPTS:
                logger.warning(
                    "[2FA] login/session did not provide accessToken; retrying login (%s/%s)",
                    attempt + 1,
                    _LOGIN_ATTEMPTS,
                )

    detail = _redacted_error(f"{type(last_error).__name__}: {last_error}", item)
    raise RuntimeError(
        f"ChatGPT login did not provide accessToken after {_LOGIN_ATTEMPTS} attempts: {detail}"
    ) from last_error


def deactivate_2fa_in_page(driver, access_token: str | None = None) -> bool:
    """Disable the current account MFA using the authenticated browser session."""
    transport = BrowserPageTransport(driver)
    access_token = str(access_token or "").strip()
    if not access_token:
        session = fetch_session(transport)
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("2FA deactivate requires an authenticated access token")

    info_path = "/backend-api/accounts/mfa_info"
    info_response = transport.get(
        _MFA_INFO_URL,
        headers=_mfa_request_headers(transport, access_token, info_path),
    )
    info_status_code = int(getattr(info_response, "status_code", 0) or 0)
    if not 200 <= info_status_code < 300:
        raise RuntimeError(
            f"2FA factor lookup failed HTTP {info_status_code}: {_response_detail(info_response)}"
        )
    try:
        factor_id = _extract_mfa_factor_id(info_response.json())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("2FA factor lookup returned invalid JSON") from exc
    if not factor_id:
        raise RuntimeError("2FA factor lookup returned no factor_id")

    disable_path = "/backend-api/accounts/mfa/user/disable_in_house"
    response = transport.post(
        _MFA_DISABLE_URL,
        headers=_mfa_request_headers(transport, access_token, disable_path),
        data=json.dumps({"factor_id": factor_id}),
    )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        hint = "; endpoint may be unavailable" if status_code in {404, 405} else ""
        raise RuntimeError(f"2FA deactivate failed HTTP {status_code}{hint}: {_response_detail(response)}")
    logger.info("2FA deactivate succeeded")
    return True


def change_twofa_in_browser(driver, item: TwofaChangeInput) -> dict[str, object]:
    """Login, disable the old TOTP, and enroll a new TOTP in one session."""
    remote_disabled = False
    access_token = ""
    try:
        access_token = _login_and_get_access_token(driver, item)
        deactivate_2fa_in_page(driver, access_token=access_token)
        remote_disabled = True
        new_secret = setup_2fa_in_page(driver, item.email)
        return {
            "ok": True,
            "email": item.email,
            "new_totp_secret": new_secret,
            "remote_disabled": True,
            "access_token": access_token,
        }
    except Exception as exc:  # noqa: BLE001 - each account must produce a result.
        result = {
            "ok": False,
            "email": item.email,
            "remote_disabled": remote_disabled,
            "error": _redacted_error(f"{type(exc).__name__}: {exc}", item),
        }
        if access_token:
            result["access_token"] = access_token
        return result
    finally:
        logout = getattr(driver, "get", None)
        if callable(logout):
            try:
                logout("https://chatgpt.com/auth/logout")
            except Exception:  # noqa: BLE001 - logout is best-effort cleanup.
                logger.debug("ChatGPT logout cleanup failed")


def redact_twofa_result(result: dict[str, object]) -> dict[str, object]:
    """Remove newly generated secrets before a result crosses the API boundary."""
    safe = dict(result)
    safe.pop("new_totp_secret", None)
    safe.pop("totp_secret", None)
    safe.pop("access_token", None)
    return safe
