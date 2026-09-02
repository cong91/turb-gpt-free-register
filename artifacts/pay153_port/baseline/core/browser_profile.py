"""Provider-neutral lifecycle for browser sessions used by account tools."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserProfileSession:
    """A Selenium-compatible driver plus provider-owned cleanup callbacks."""

    driver: Any
    provider: str
    timeout: int
    keep_open: bool
    _cleanup: Callable[[], None]
    # Cloud providers need the original session descriptor to reuse the same CDP session.
    session_info: Any | None = None

    def close(self) -> None:
        if not self.keep_open:
            quit_driver = getattr(self.driver, "quit", None)
            if callable(quit_driver):
                quit_driver()

    def cleanup(self) -> None:
        self._cleanup()


def _configured_provider() -> str:
    from config import roxybrowser as driver_config

    value = str(getattr(driver_config, "REGISTRATION_DRIVER", "roxy") or "roxy")
    return value.strip().lower()


def _open_roxy(proxy: str | None = None) -> BrowserProfileSession:
    from config import roxybrowser as driver_config
    from core.browser_registration import _build_driver, _center_browser_window
    from core.roxybrowser_client import RoxyBrowserClient

    client = RoxyBrowserClient()
    opened = client.open_profile(proxy=proxy)
    driver = _build_driver(opened)
    _center_browser_window(driver)
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
    from core.browser_registration import _center_browser_window
    from core.cloakbrowser_driver import build_cloak_driver

    driver, _opened = build_cloak_driver(proxy=proxy)
    _center_browser_window(driver)
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


def _open_cloud(provider: str, proxy: str | None = None) -> BrowserProfileSession:
    """Connect cloud Playwright sessions through the shared Selenium adapter."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("personal-information changes require playwright") from exc

    client: Any
    if provider == "skyvern":
        from config import browser_use as shared_browser_config
        from config import skyvern as skyvern_config
        from core.skyvern_client import SkyvernClient

        client = SkyvernClient()
        if proxy is not None:
            raise RuntimeError(
                "Skyvern Cloud hiện không hỗ trợ custom rotating proxy cho browser session; "
                "hãy dùng Browser Use Cloud hoặc tắt rotating proxy."
            )
        session = client.open_session()
        label = "Skyvern"
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
        connect_kwargs = {"headers": client.cdp_headers()}
    else:
        from config import browser_use as browser_use_config
        from core.browser_use_client import BrowserUseClient

        client = BrowserUseClient()
        session = client.open_session(proxy=proxy) if proxy is not None else client.open_session()
        label = "BrowserUse"
        timeout = max(1, int(getattr(browser_use_config, "BROWSER_USE_TIMEOUT", 90) or 90))
        connect_kwargs = {}

    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.connect_over_cdp(
            session.connect_url,
            **cast(dict[str, Any], connect_kwargs),
        )
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        from core.cloakbrowser_driver import BrowserSeleniumDriver

        driver = BrowserSeleniumDriver(browser=browser, context=context, page=page)
        driver._registration_log_prefix = f"[{label} personal info]"
        driver._registration_timeout = timeout
        driver.set_page_load_timeout(timeout)
        driver.set_script_timeout(timeout)
    except Exception:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue after provider errors.
                logger.debug("Cloud browser close after open failure failed: %s", exc)
        playwright.stop()
        raise

    def cleanup() -> None:
        session_id = str(getattr(cast(Any, session), "session_id", "") or "")
        try:
            if provider == "skyvern" and session_id:
                client.close_browser_session(session_id)
        finally:
            try:
                browser.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the job result.
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


def open_browser_profile(proxy: str | None = None) -> BrowserProfileSession:
    """Open the configured browser provider for a browser account workflow."""
    provider = _configured_provider()
    if provider in {"roxy", "roxybrowser", "fingerprint", "browser"}:
        return _open_roxy() if proxy is None else _open_roxy(proxy=proxy)
    if provider in {"cloak", "cloakbrowser"}:
        return _open_cloak() if proxy is None else _open_cloak(proxy=proxy)
    if provider in {"browser_use", "browseruse", "browser-use", "bu"}:
        return _open_cloud("browser_use") if proxy is None else _open_cloud("browser_use", proxy=proxy)
    if provider in {"skyvern", "sv"}:
        return _open_cloud("skyvern") if proxy is None else _open_cloud("skyvern", proxy=proxy)
    raise RuntimeError(
        f"personal-information changes do not support REGISTRATION_DRIVER={provider!r}"
    )
