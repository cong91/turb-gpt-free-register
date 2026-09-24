"""Provider-neutral Selenium adapter setup for browser profile sessions."""
from __future__ import annotations

import ctypes
import logging
import platform

from config import roxybrowser as _cfg
from core.browser_page_actions import _browser_actions_enabled, _log_prefix

logger = logging.getLogger(__name__)


def build_selenium_driver(opened):
    """Build a PageDriver-compatible Selenium adapter for a profile session."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    if opened.debugger_address:
        options = Options()
        options.page_load_strategy = "eager"
        _enable_performance_logging(options)
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
        driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip() if isinstance(raw_data, dict) else ""
        raw_driver = (
            webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
            if driver_path
            else webdriver.Chrome(options=options)
        )
    elif opened.webdriver_url:
        options = Options()
        options.page_load_strategy = "eager"
        _enable_performance_logging(options)
        raw_driver = RemoteWebDriver(command_executor=opened.webdriver_url, options=options)
    else:
        raise RuntimeError("浏览器未返回可连接的 Selenium 地址")

    apply_automation_mask(raw_driver)
    return SeleniumPageDriver(raw_driver)


class SeleniumPageDriver:
    """Adapt raw Selenium WebDriver to the shared PageDriver port."""

    def __init__(self, driver):
        self._driver = driver
        self._script_timeout = 30
        self._typing_js_fallback_allowed = True

    def __getattr__(self, name):
        return getattr(self._driver, name)

    @property
    def current_url(self):
        return self._driver.current_url

    @property
    def window_handles(self):
        return list(self._driver.window_handles)

    @property
    def script_timeout(self):
        return self._script_timeout

    def set_script_timeout(self, seconds: int) -> None:
        self._script_timeout = max(1, int(seconds))
        self._driver.set_script_timeout(self._script_timeout)

    def clear_cookies(self) -> None:
        self._driver.delete_all_cookies()

    @property
    def browser(self):
        return getattr(self._driver, "browser", None)

    @property
    def context(self):
        return getattr(self._driver, "context", None)

    @property
    def page(self):
        return getattr(self._driver, "page", None)

def _enable_performance_logging(options) -> None:
    try:
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("当前 Selenium 选项不支持 performance log：%s", exc)


def center_browser_window(driver) -> None:
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        if platform.system().lower() != "windows":
            return

        class _Rect(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:  # noqa: BLE001
        logger.warning("浏览器窗口居中失败，继续执行：%s", exc)


def apply_automation_mask(driver) -> None:
    if not _browser_actions_enabled():
        return
    try:
        script = r"""
        Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
          window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : originalQuery(parameters)
          );
        }
        """
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            driver.execute_script(script)
        except Exception:  # noqa: BLE001, S110
            pass
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:  # noqa: BLE001
        logger.debug("注入自动化特征弱化脚本失败：%s", exc)
