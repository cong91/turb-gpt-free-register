"""Parse OpenAI credential accounts for Codex OAuth import."""
from __future__ import annotations


def parse_credential_lines(text: str) -> list[dict[str, str]]:
    """Parse `USER | PASS | 2FA`, preserving pipe characters inside passwords."""
    records: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.find("|")
        last = line.rfind("|")
        if first <= 0 or last <= first:
            continue
        email = line[:first].strip()
        password = line[first + 1:last].strip()
        totp_secret = line[last + 1:].strip()
        if not email or not password or not totp_secret:
            continue
        records.append({
            "email": email,
            "registration_password": password,
            "totp_secret": totp_secret,
        })
    return records
