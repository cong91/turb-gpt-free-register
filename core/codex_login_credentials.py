"""Credential model and TOTP helpers for local Codex OAuth login."""
from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field

import pyotp


@dataclass(frozen=True, slots=True)
class CodexLoginCredentials:
    email: str
    password: str = dataclass_field(repr=False)
    totp_secret: str = dataclass_field(repr=False)

    @classmethod
    def from_account(cls, account: dict) -> CodexLoginCredentials:
        email = str(account.get("email") or "").strip()
        password = str(account.get("registration_password") or "")
        totp_secret = str(account.get("totp_secret") or "").strip()
        if not email or not password or not totp_secret:
            raise ValueError("Codex credential account requires email, password, and 2FA secret")
        return cls(email=email, password=password, totp_secret=totp_secret)


def normalize_totp_secret(value: str) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("otpauth://"):
        try:
            raw = str(pyotp.parse_uri(raw).secret or "")
        except Exception as exc:
            raise ValueError("Invalid otpauth TOTP URI") from exc
    normalized = "".join(ch for ch in raw.upper() if not ch.isspace() and ch != "-")
    if not normalized:
        raise ValueError("TOTP secret is empty")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        base64.b32decode(normalized + padding, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("TOTP secret is not valid Base32") from exc
    return normalized


def generate_totp_code(
    secret: str,
    *,
    min_remaining: float = 3.0,
    previous_code: str | None = None,
) -> str:
    totp = pyotp.TOTP(normalize_totp_secret(secret))
    now = time.time()
    remaining = float(totp.interval) - (now % float(totp.interval))
    code = str(totp.at(now))
    if remaining <= max(0.0, float(min_remaining)) or (previous_code and code == previous_code):
        time.sleep(remaining + 0.05)
        now = time.time()
        code = str(totp.at(now))
    return code
