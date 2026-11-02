"""Select the browser provider for a Codex OAuth request."""
from __future__ import annotations


def dispatch_browser_oauth(
    *,
    email: str,
    otp_provider,
    proxy: str | None,
    credentials,
    oauth_driver: str | None,
    existing_driver,
    existing_opened,
    existing_browser,
    existing_context,
    existing_page,
    existing_session_info,
    fresh_browser_profile: bool,
    clear_existing_state: bool,
) -> dict | None:
    """Return a browser result, or None when the protocol driver is selected."""
    from config import codex as codex_config
    from config import roxybrowser as browser_config
    from core.codex_oauth import _codex_result

    driver = str(oauth_driver or getattr(codex_config, "CODEX_OAUTH_DRIVER", "protocol") or "protocol").strip().lower()
    if driver == "same_as_registration":
        driver = str(getattr(browser_config, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    roxy_context = existing_driver is not None or existing_opened is not None
    cloud_context = any(value is not None for value in (
        existing_browser, existing_context, existing_page, existing_session_info,
    ))
    reused = roxy_context or cloud_context
    if reused and driver in {"protocol", "api", "http"}:
        return _codex_result(status="failed", email=email, message="protocol Codex OAuth 没有可复用的 browser profile；请在拥有当前 browser 的 worker 内执行")
    if roxy_context and driver not in {"roxy", "roxybrowser", "fingerprint", "browser", "cloak", "cloakbrowser"}:
        return _codex_result(status="failed", email=email, message="当前 Roxy/Cloak browser context 与 CODEX_OAUTH_DRIVER 不匹配")
    if cloud_context and driver not in {"browser_use", "browseruse", "browser-use", "bu", "skyvern", "sv"}:
        return _codex_result(status="failed", email=email, message="当前 BrowserUse/Skyvern session 与 CODEX_OAUTH_DRIVER 不匹配")
    if credentials:
        if str(getattr(credentials, "email", "") or "").strip().casefold() != email.strip().casefold():
            return _codex_result(status="failed", email=email, message="Codex credential email mismatch")
        if driver not in {"roxy", "roxybrowser", "fingerprint", "browser", "cloak", "cloakbrowser", "browser_use", "browseruse", "browser-use", "bu", "skyvern", "sv"}:
            return _codex_result(status="failed", email=email, message="Codex credential login 只支持 Roxy/Cloak/BrowserUse/Skyvern browser driver")
    try:
        if driver in {"roxy", "roxybrowser", "fingerprint", "browser"}:
            from core.roxy_codex_oauth import run_roxy_codex_oauth

            kwargs = {
                "otp_provider": otp_provider, "proxy": proxy, "force": True,
                "credentials": credentials, "existing_driver": existing_driver,
                "existing_opened": existing_opened, "reuse_existing_profile": roxy_context,
            }
            if fresh_browser_profile:
                kwargs["fresh_profile"] = True
            if not clear_existing_state:
                kwargs["clear_existing_state"] = False
                kwargs["proxy"] = "" if proxy is None else proxy
            return run_roxy_codex_oauth(email, **kwargs)
        if driver in {"browser_use", "browseruse", "browser-use", "bu", "skyvern", "sv"}:
            if driver in {"skyvern", "sv"}:
                from core.skyvern_codex_oauth import run_skyvern_codex_oauth as run_cloud
            else:
                from core.browser_use_codex_oauth import run_browser_use_codex_oauth as run_cloud
            kwargs = {
                "otp_provider": otp_provider, "proxy": proxy, "force": True,
                "credentials": credentials, "existing_browser": existing_browser,
                "existing_context": existing_context, "existing_page": existing_page,
                "existing_session_info": existing_session_info,
            }
            if fresh_browser_profile:
                kwargs["fresh_browser_profile"] = True
            if not clear_existing_state:
                kwargs["clear_existing_state"] = False
            return run_cloud(email, **kwargs)
        if driver in {"cloak", "cloakbrowser"}:
            from config import cloakbrowser as cloak_config
            from core.cloakbrowser_driver import build_cloak_driver
            from core.roxy_codex_oauth import run_roxy_codex_oauth

            if roxy_context:
                return run_roxy_codex_oauth(
                    email, otp_provider=otp_provider, proxy=("" if not clear_existing_state and proxy is None else proxy), force=True,
                    existing_driver=existing_driver, existing_opened=existing_opened,
                    reuse_existing_profile=True, clear_existing_state=clear_existing_state,
                    credentials=credentials,
                )
            driver_instance, opened = build_cloak_driver(proxy=proxy)
            try:
                return run_roxy_codex_oauth(
                    email, otp_provider=otp_provider, proxy=proxy, force=True,
                    existing_driver=driver_instance, existing_opened=opened,
                    reuse_existing_profile=True, clear_existing_state=True,
                    credentials=credentials,
                )
            finally:
                if not bool(getattr(cloak_config, "CLOAK_KEEP_BROWSER_OPEN", False)):
                    try:
                        driver_instance.quit()
                    except Exception:
                        pass
        if driver not in {"protocol", "api", "http"}:
            raise RuntimeError(f"[Codex] 不支持的 CODEX_OAUTH_DRIVER={driver!r}，可选 protocol / roxy / cloak / browser_use / skyvern")
    except ImportError as exc:
        if reused:
            return _codex_result(status="failed", email=email, message=f"复用 browser context 所需的 Codex OAuth driver 不可用：{type(exc).__name__}")
        if fresh_browser_profile:
            return _codex_result(status="failed", email=email, message=f"Codex retry 的 fresh browser profile driver 不可用：{type(exc).__name__}")
        if credentials:
            return _codex_result(status="failed", email=email, message=f"Codex credential driver unavailable: {type(exc).__name__}")
    return None
