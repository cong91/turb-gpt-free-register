"""Codex follow-up for successfully persisted personal-information changes."""
from __future__ import annotations

import json
from typing import Any

def _browser_context(profile: Any, active_proxy: str | None) -> dict[str, Any]:
    provider = str(getattr(profile, "provider", "") or "").strip().lower()
    driver = getattr(profile, "driver", None)
    if provider in {"roxy", "cloak"}:
        opened = getattr(profile, "existing_opened", None)
        if driver is None or opened is None:
            raise RuntimeError("Không đủ ngữ cảnh trình duyệt để tái sử dụng phiên hiện tại")
        return {"existing_driver": driver, "existing_opened": opened}
    if provider in {"browser_use", "skyvern"}:
        if provider == "browser_use" and active_proxy is None:
            from config import browser_use as browser_config
            if bool(getattr(browser_config, "BROWSER_USE_USE_PROXY", True)):
                raise RuntimeError("Không thể xác minh tuyến proxy cloud để đổi token")
        browser = getattr(driver, "browser", None)
        context = getattr(driver, "context", None)
        page = getattr(driver, "page", None)
        session_info = getattr(profile, "session_info", None)
        if any(value is None for value in (browser, context, page, session_info)):
            raise RuntimeError("Không đủ ngữ cảnh phiên cloud để tái sử dụng phiên hiện tại")
        return {
            "existing_browser": browser,
            "existing_context": context,
            "existing_page": page,
            "existing_session_info": session_info,
        }
    raise RuntimeError("Nhà cung cấp trình duyệt hiện tại không hỗ trợ đăng nhập Codex cùng phiên")


def _matches_identity(session: object, expected_email: str) -> None:
    user = session.get("user") if isinstance(session, dict) else None
    actual_email = str((user or {}).get("email") or "").strip().casefold() if isinstance(user, dict) else ""
    if not isinstance(session, dict) or not session.get("accessToken") or not actual_email or actual_email != str(expected_email or "").strip().casefold():
        raise RuntimeError("Tài khoản trong phiên trình duyệt không khớp tài khoản vừa cập nhật")


def verify_cloud_identity(context: Any, expected_email: str) -> None:
    try:
        response = context.request.get("https://chatgpt.com/api/auth/session", timeout=10000)
        if response.status != 200:
            raise RuntimeError("session request failed")
        _matches_identity(response.json(), expected_email)
    except Exception as exc:
        raise RuntimeError("Không thể xác minh tài khoản trong phiên trình duyệt") from exc


def verify_live_identity(driver: Any, expected_email: str) -> None:
    if getattr(driver, "context", None) is not None:
        verify_cloud_identity(driver.context, expected_email)
        return
    original = None
    opened_tab = False
    try:
        original = driver.current_window_handle
        driver.switch_to.new_window("tab")
        opened_tab = True
        driver.get("https://chatgpt.com/")
        session = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'})
              .then(r => r.ok ? r.json() : null).then(done).catch(() => done(null));
        """)
        _matches_identity(session, expected_email)
    except Exception as exc:
        raise RuntimeError("Không thể xác minh tài khoản trong phiên trình duyệt") from exc
    finally:
        if original is not None and opened_tab:
            try:
                driver.close()
            finally:
                driver.switch_to.window(original)


def _verify_current_identity(profile: Any, expected_email: str) -> None:
    driver = getattr(profile, "driver", None)
    if driver is None:
        raise RuntimeError("Không xác định được phiên ChatGPT hiện tại")
    verify_live_identity(driver, expected_email)


def _safe_codex_reason(message: object, *, password: str, totp_secret: str) -> str:
    text = str(message or "").strip().lower()
    if "otp" in text and any(term in text for term in ("timeout", "not received", "did not receive", "未收到", "超时")):
        return "không nhận được email OTP"
    if any(term in text for term in ("sms", "phone verification", "phone otp", "手机号", "短信")):
        return "xác minh SMS thất bại"
    if any(term in text for term in ("password", "credential login")):
        return "xác thực bằng mật khẩu thất bại"
    if any(term in text for term in ("identity", "account does not match", "email mismatch", "không khớp")):
        return "không xác minh được định danh tài khoản"
    if any(term in text for term in ("route", "proxy", "skyvern", "browser use")):
        return "không xác minh được route của phiên trình duyệt hiện tại"
    return "lỗi xác thực hoặc callback OAuth"


def _codex_route_skip_reason(profile: Any, active_proxy: str | None) -> str | None:
    provider = str(getattr(profile, "provider", "") or "").strip().lower()
    if provider in {"roxy", "cloak"} and active_proxy is None:
        if provider == "roxy":
            from config import roxybrowser as browser_config
            implicit_proxy = bool(getattr(browser_config, "ROXY_CREATE_USE_PROXY_POOL", False))
        else:
            from config import cloakbrowser as browser_config
            implicit_proxy = bool(getattr(browser_config, "CLOAK_USE_PROXY", True))
        if implicit_proxy:
            return f"Bỏ qua đăng nhập Codex: không xác minh được tuyến mạng thực tế của phiên {provider} hiện tại."
    if provider == "roxy" and active_proxy is None:
        from config import roxybrowser as browser_config

        if bool(getattr(browser_config, "ROXY_CREATE_USE_PROXY_POOL", False)) or str(getattr(browser_config, "ROXY_PROFILE_ID", "") or "").strip():
            return "Bỏ qua đăng nhập Codex: không xác minh được tuyến mạng thực tế của phiên Roxy hiện tại."
    if provider == "cloak":
        from config import cloakbrowser as browser_config

        use_proxy = bool(getattr(browser_config, "CLOAK_USE_PROXY", True))
        if active_proxy is None and use_proxy:
            return "Bỏ qua đăng nhập Codex: không xác minh được tuyến proxy của phiên Cloak hiện tại."
        if active_proxy is not None and not use_proxy:
            return "Bỏ qua đăng nhập Codex: cấu hình Cloak đã bỏ qua proxy của phiên hiện tại."
    if provider == "skyvern":
        from config import skyvern as skyvern_config

        if active_proxy is not None:
            return "Bỏ qua đăng nhập Codex: Skyvern không hỗ trợ proxy tùy chỉnh của phiên hiện tại."
        proxy_location = str(getattr(skyvern_config, "SKYVERN_PROXY_LOCATION", "") or "").strip().casefold()
        if proxy_location not in {"", "none"}:
            return "Bỏ qua đăng nhập Codex: tuyến proxy cloud của Skyvern không thể tái sử dụng cho token exchange."
    if provider == "browser_use":
        from config import browser_use as browser_config

        if active_proxy is None and bool(getattr(browser_config, "BROWSER_USE_USE_PROXY", True)):
            return "Bỏ qua đăng nhập Codex: proxy do Browser Use quản lý không thể tái sử dụng cho token exchange."
        if active_proxy is not None:
            session_info = getattr(profile, "session_info", None)
            raw = getattr(session_info, "raw", None)
            custom_proxy = raw.get("custom_proxy") if isinstance(raw, dict) else None
            if not isinstance(custom_proxy, dict):
                return "Bỏ qua đăng nhập Codex: không xác minh được proxy của phiên Browser Use hiện tại."
            from core.rotating_proxy_runtime import custom_proxy_details

            expected = custom_proxy_details(active_proxy)
            if custom_proxy.get("host") != expected.get("host") or custom_proxy.get("port") != expected.get("port"):
                return "Bỏ qua đăng nhập Codex: tuyến proxy không khớp phiên Browser Use hiện tại."
    return None


def run_personal_info_codex(
    *,
    email: str,
    profile: Any,
    active_proxy: str | None,
    password: str,
    totp_secret: str,
    otp_provider=None,
) -> dict[str, Any]:
    """Run Codex in the live change worker and return only allowlisted metadata."""
    try:
        route_skip = _codex_route_skip_reason(profile, active_proxy)
        if route_skip:
            return {"status": "skipped", "ok": False, "reason": route_skip}
        if not str(password or "").strip():
            raise RuntimeError("Thiếu mật khẩu cần thiết cho Codex")
        _verify_current_identity(profile, email)
        from core.codex_login_credentials import CodexLoginCredentials
        from core.codex_oauth import _codex_auth_url_source, run_codex_oauth

        oauth_options = _browser_context(profile, active_proxy)
        if otp_provider is not None:
            oauth_options["otp_provider"] = otp_provider
        result = run_codex_oauth(
            email=email,
            force=True,
            credentials=CodexLoginCredentials(
                email=email,
                password=password,
                totp_secret=totp_secret,
            ),
            oauth_driver=str(getattr(profile, "provider", "") or ""),
            proxy=active_proxy,
            clear_existing_state=False,
            **oauth_options,
        )
        auth_source = str(_codex_auth_url_source() or "").strip().lower()
        status = str((result or {}).get("status") or "failed").strip().lower()
        if auth_source in {"cpa", "sub2"} and status == "receipt" and bool((result or {}).get("ok")):
            return {
                "status": "receipt",
                "ok": True,
                "credential_saved": False,
                "reason": "Đã hoàn tất callback OAuth; nhà cung cấp chưa trả credential cục bộ.",
            }
        ok = bool((result or {}).get("ok")) and status in {"success", "ok"}
        if not ok:
            reason = _safe_codex_reason(
                (result or {}).get("message") or (result or {}).get("error"),
                password=password,
                totp_secret=totp_secret,
            )
            return {
                "status": "failed",
                "ok": False,
                "reason": f"Đăng nhập Codex thất bại: {reason}. Thao tác đổi thông tin vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản.",
            }
        file_path = str((result or {}).get("file_path") or "").strip()
        credential_saved = False
        if file_path.startswith("sqlite://codex_accounts/"):
            from core import db
            content, _ = db.read_codex_credential(file_path.removeprefix("sqlite://codex_accounts/"))
            stored = json.loads(content)
            credential_saved = bool(stored.get("access_token") or stored.get("refresh_token"))
        if auth_source == "local" and not credential_saved:
            return {
                "status": "failed",
                "ok": False,
                "reason": "Không xác nhận được credential Codex đã lưu. Thao tác đổi thông tin vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản.",
            }
        return {
            "status": "success" if credential_saved else "receipt",
            "ok": True,
            "credential_saved": credential_saved,
            "reason": "Đã lưu credential Codex cục bộ" if credential_saved else "Đã hoàn tất callback OAuth; không có credential cục bộ",
        }
    except Exception:
        return {
            "status": "failed",
            "ok": False,
            "reason": "Không thể xác minh phiên hoặc lưu kết quả Codex. Thao tác đổi thông tin vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản.",
        }


def run_personal_info_codex_and_record(
    *,
    email: str,
    profile: Any,
    active_proxy: str | None,
    password: str,
    totp_secret: str,
    otp_provider=None,
) -> dict[str, Any]:
    """Keep Codex execution and status writes outside the account-change outcome."""
    try:
        options = {"otp_provider": otp_provider} if otp_provider is not None else {}
        outcome = run_personal_info_codex(
            email=email,
            profile=profile,
            active_proxy=active_proxy,
            password=password,
            totp_secret=totp_secret,
            **options,
        )
    except Exception:
        outcome = {
            "status": "failed",
            "ok": False,
            "reason": "Đăng nhập Codex thất bại. Thao tác đổi thông tin vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản.",
        }
    try:
        record_personal_info_codex(email, outcome)
    except Exception:
        outcome["status_persisted"] = False
    return outcome


def skipped_personal_info_codex(email: str, reason: str) -> dict[str, Any]:
    """Record an explicit non-run outcome without changing account mutation status."""
    outcome: dict[str, Any] = {"status": "skipped", "ok": False, "reason": str(reason or "Codex follow-up skipped")[:300]}
    try:
        record_personal_info_codex(email, outcome)
    except Exception:
        outcome["status_persisted"] = False
    return outcome


def record_personal_info_codex(email: str, codex_result: dict[str, Any]) -> None:
    """Update account Codex status independently from the successful change."""
    from core import db

    status = str(codex_result.get("status") or "failed").strip().lower()
    if status == "skipped":
        return
    if status not in {"success", "receipt", "failed"}:
        status = "failed"
    reason = str(codex_result.get("reason") or "").strip() or None
    try:
        if not db.update_account_codex_status(email, status, None if status in {"success", "receipt"} else reason):
            codex_result["status_persisted"] = False
    except Exception:
        codex_result["status_persisted"] = False
