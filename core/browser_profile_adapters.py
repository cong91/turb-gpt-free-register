"""Provider-specific browser profile lifecycle adapters."""
from __future__ import annotations

import logging
from typing import Any, cast

from core.browser_profile import BrowserProfileSession

logger = logging.getLogger(__name__)


def _close_roxy(session: BrowserProfileSession) -> None:
    session.close()


def _delete_roxy(session: BrowserProfileSession) -> None:
    session.cleanup()


def _close_cloak(session: BrowserProfileSession) -> None:
    session.close()


def _delete_cloak(session: BrowserProfileSession) -> None:
    session.cleanup()


def _open_roxy(proxy: str | None = None) -> BrowserProfileSession:
    from config import roxybrowser as driver_config
    from core.browser_selenium_adapter import (
        build_selenium_driver,
        center_browser_window,
    )
    from core.roxybrowser_client import RoxyBrowserClient

    client = RoxyBrowserClient()
    opened = client.open_profile(proxy=proxy)
    driver = build_selenium_driver(opened)
    center_browser_window(driver)
    timeout = max(1, int(getattr(driver_config, "ROXY_SELENIUM_TIMEOUT", 90) or 90))
    driver.set_page_load_timeout(timeout)
    driver.set_script_timeout(
        max(1, int(getattr(driver_config, "ROXY_SCRIPT_TIMEOUT", timeout) or timeout))
    )
    keep_open = bool(getattr(driver_config, "ROXY_KEEP_BROWSER_OPEN", False))
    return BrowserProfileSession(
        driver=driver,
        provider="roxy",
        timeout=timeout,
        keep_open=keep_open,
        _cleanup=(lambda: None) if keep_open else lambda: client.cleanup_profile(opened),
    )


def _open_cloak(proxy: str | None = None) -> BrowserProfileSession:
    from config import cloakbrowser as driver_config
    from core.browser_selenium_adapter import center_browser_window
    from core.cloakbrowser_driver import build_cloak_driver

    driver, _opened = build_cloak_driver(proxy=proxy)
    center_browser_window(driver)
    timeout = max(1, int(getattr(driver_config, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    driver.set_page_load_timeout(timeout)
    driver.set_script_timeout(
        max(1, int(getattr(driver_config, "CLOAK_SCRIPT_TIMEOUT", timeout) or timeout))
    )
    return BrowserProfileSession(
        driver=driver,
        provider="cloak",
        timeout=timeout,
        keep_open=bool(getattr(driver_config, "CLOAK_KEEP_BROWSER_OPEN", False)),
        _cleanup=lambda: None,
    )


def _open_cloud_session(
    *,
    provider: str,
    session: Any,
    timeout: int,
    connect_kwargs: dict[str, Any],
    session_cleanup,
) -> BrowserProfileSession:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("personal-information changes require playwright") from exc

    from core.cloakbrowser_driver import BrowserSeleniumDriver

    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.connect_over_cdp(
            session.connect_url,
            **cast(dict[str, Any], connect_kwargs),
        )
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        driver = BrowserSeleniumDriver(
            browser=browser,
            context=context,
            page=page,
            typing_js_fallback_allowed=False,
        )
        driver._registration_log_prefix = f"[{provider}]"
        driver._registration_timeout = timeout
        driver.set_page_load_timeout(timeout)
        driver.set_script_timeout(timeout)
    except Exception:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Cloud browser close after open failure failed: %s", exc)
        playwright.stop()
        raise

    def cleanup() -> None:
        try:
            session_cleanup()
        finally:
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Cloud browser close failed: %s", exc)
            playwright.stop()

    return BrowserProfileSession(
        driver=driver,
        provider=provider,
        timeout=timeout,
        keep_open=False,
        _cleanup=cleanup,
        session_info=session,
    )


def _open_browser_use(proxy: str | None = None) -> BrowserProfileSession:
    from config import browser_use as browser_use_config
    from core.browser_use_client import BrowserUseClient

    client = BrowserUseClient()
    session = client.open_session(proxy=proxy) if proxy is not None else client.open_session()
    timeout = max(1, int(getattr(browser_use_config, "BROWSER_USE_TIMEOUT", 90) or 90))
    return _open_cloud_session(
        provider="browser_use",
        session=session,
        timeout=timeout,
        connect_kwargs={},
        session_cleanup=lambda: None,
    )


def _open_skyvern(proxy: str | None = None) -> BrowserProfileSession:
    if proxy is not None:
        raise RuntimeError(
            "Skyvern Cloud hiện không hỗ trợ custom rotating proxy cho browser session; "
            "hãy dùng Browser Use Cloud hoặc tắt rotating proxy."
        )
    from config import browser_use as shared_browser_config
    from config import skyvern as skyvern_config
    from core.skyvern_client import SkyvernClient

    client = SkyvernClient()
    session = client.open_session()
    timeout = max(
        1,
        int(
            getattr(
                skyvern_config,
                "SKYVERN_PROFILE_TIMEOUT",
                getattr(shared_browser_config, "SKYVERN_PROFILE_TIMEOUT", 45),
            )
            or 45
        ),
    )
    session_id = str(getattr(session, "session_id", "") or "")
    return _open_cloud_session(
        provider="skyvern",
        session=session,
        timeout=timeout,
        connect_kwargs={"headers": client.cdp_headers()},
        session_cleanup=(
            lambda: client.close_browser_session(session_id)
            if session_id
            else None
        ),
    )
