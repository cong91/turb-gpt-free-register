from __future__ import annotations

from urllib.parse import urlparse

from core.gmail_aliases import GmailAliasError, normalize_routed_domains
from core.paymesh_aliases import PaymeshAliasError, normalize_paymesh_routed_domains


def validate_email_sources(
    sources: list[str],
    config,
    gmail_cdks: list[str] | None = None,
    paymesh_cdks: list[str] | None = None,
    gmail_routed_domains: list[str] | None = None,
    paymesh_routed_domains: list[str] | None = None,
) -> str | None:
    """Trả thông báo lỗi prerequisite; không gọi provider bên ngoài."""
    if "gmail_123452026" in sources:
        if not gmail_cdks:
            return "Đã chọn gmail_123452026, hãy nhập ít nhất một CDK cho batch đăng ký."
        try:
            limit = int(getattr(config, "GMAIL_123452026_ACCOUNTS_PER_CDK", 6))
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= 6:
            return "Số tài khoản trên mỗi CDK phải từ 1 đến 6."
        api_base = str(getattr(config, "GMAIL_123452026_API_BASE", "") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Địa chỉ Gmail CDK API không hợp lệ."
        try:
            normalize_routed_domains(gmail_routed_domains or [])
        except GmailAliasError as exc:
            return str(exc)

    if "paymesh" in sources:
        if not paymesh_cdks:
            return "Đã chọn Paymesh, hãy nhập ít nhất một MAIL card cho batch đăng ký."
        try:
            limit = int(getattr(config, "PAYMESH_ACCOUNTS_PER_CDK", 6))
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= 6:
            return "Số tài khoản trên mỗi Paymesh card phải từ 1 đến 6."
        api_base = str(getattr(config, "PAYMESH_API_BASE", "") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Địa chỉ Paymesh API không hợp lệ."
        try:
            normalize_paymesh_routed_domains(paymesh_routed_domains or [])
        except PaymeshAliasError as exc:
            return str(exc)

    if "automated_email_api" in sources:
        api_base = str(getattr(config, "EMAIL_API_BASE_URL", "") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Địa chỉ Automated Email API không hợp lệ."
        if not str(getattr(config, "EMAIL_API_KEY", "") or "").strip():
            return "Đã chọn automated_email_api, hãy cấu hình EMAIL_API_KEY."

    if "otpmail" in sources:
        api_base = str(getattr(config, "OTPGMAIL_API_BASE", "https://otpgmail.net") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Địa chỉ OTPGmail API không hợp lệ."
        if not str(getattr(config, "OTPGMAIL_API_KEY", "") or "").strip():
            return "Đã chọn otpmail, hãy cấu hình OTPGMAIL_API_KEY."
        if not str(getattr(config, "OTPGMAIL_SERVICE_CODE", "dr") or "").strip():
            return "Đã chọn otpmail, hãy cấu hình OTPGMAIL_SERVICE_CODE theo GET /v1/services."

    if "bamboommo" in sources:
        api_base = str(getattr(config, "BAMBOOMMO_API_BASE", "https://api.bamboommo.com") or "").strip()
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Đã chọn bamboommo, hãy cấu hình BAMBOOMMO_API_BASE hợp lệ."
        if not str(getattr(config, "BAMBOOMMO_API_KEY", "") or "").strip():
            return "Đã chọn bamboommo, hãy cấu hình BAMBOOMMO_API_KEY."
        try:
            server = int(getattr(config, "BAMBOOMMO_SERVER", 2) or 0)
        except (TypeError, ValueError):
            server = 0
        if server not in (1, 2):
            return "BAMBOOMMO_SERVER chỉ được là 1 hoặc 2."
        if str(getattr(config, "BAMBOOMMO_MAIL_TYPE", "GM") or "").strip().upper() != "GM":
            return "BAMBOOMMO_MAIL_TYPE phải là GM cho Gmail."
        if str(getattr(config, "BAMBOOMMO_SERVICE", "OP") or "").strip().upper() != "OP":
            return "BAMBOOMMO_SERVICE phải là OP cho Open AI."

    return None
