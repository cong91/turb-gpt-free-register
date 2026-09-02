"""Selenium-compatible browser registration helpers and registration flow."""
from __future__ import annotations

import logging
import random
import time
import uuid
from pathlib import Path

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core import db
from core.account_export import (
    BrowserPageTransport,
    checkpoint_account_data,
    post_register_dwell,
    save_account_data,
)
from core.browser_challenge import (
    browser_challenge_state as _browser_challenge_state,
)
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
from core.codex_login_credentials import CodexLoginCredentials
from core.email_provider import (
    _BEFORE_CODE_UNSET as _OTP_BEFORE_CODE_UNSET,
)
from core.email_provider import (
    acknowledge_verification_code,
    resolve_email_source,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay
from core.openai_auth import (
    AccountUnusableError,
    account_unusable_error_message,
    account_unusable_message,
    detect_account_unusable_text,
)
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult
from core.time_utils import local_today

logger = logging.getLogger(__name__)


def _account_unusable_page_code(driver) -> str:
    """读取 Selenium-compatible 页面正文并识别账号停用/删除状态。"""
    from selenium.common.exceptions import WebDriverException

    try:
        body = driver.execute_script("return document.body?.innerText || ''; ")
    except WebDriverException:
        return ""
    return detect_account_unusable_text(str(body or ""))


def _raise_if_account_unusable(driver) -> None:
    code = _account_unusable_page_code(driver)
    if code:
        raise AccountUnusableError(account_unusable_error_message(code), error_code=code)


def _wait_after_password_submit(driver, initial_url: str, timeout: float = 5.0) -> None:
    """等待密码提交结果；账号停用页出现时立即停止后续步骤。"""
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        _raise_if_account_unusable(driver)
        current_url = str(getattr(driver, "current_url", "") or "")
        if current_url and current_url != initial_url:
            return
        time.sleep(0.25)


def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    Cloud browser adapters share the page operations; the active adapter supplies
    an explicit prefix so logs never identify the wrong provider.
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "BrowserSeleniumDriver":
            return "[Browser注册]"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unable to resolve browser log prefix: %s", exc)
    return "[Roxy注册]"


def _registration_timeout(driver=None, fallback: int | None = None) -> int:
    """Resolve the timeout for the active browser implementation."""
    explicit = getattr(driver, "_registration_timeout", None)
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            pass
    if driver is not None and driver.__class__.__name__ == "BrowserSeleniumDriver":
        try:
            from config import cloakbrowser as _cloak_cfg

            return max(
                1,
                int(getattr(_cloak_cfg, "CLOAK_SELENIUM_TIMEOUT", fallback or 90) or fallback or 90),
            )
        except (ImportError, TypeError, ValueError):
            pass
    return max(1, int(fallback or getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90))


def _coerce_browser_mapping(driver, result, *, label: str) -> dict:
    """Keep page-state readers total while a browser navigation is in flight."""
    if isinstance(result, dict):
        return result
    try:
        current_url = str(getattr(driver, "current_url", "") or "")
    except Exception:  # noqa: BLE001
        current_url = ""
    return {
        "url": current_url,
        "error": f"{label} script returned {type(result).__name__}",
        "reason": "navigation_in_progress",
    }


def _is_transient_email_submission_error(exc: Exception) -> bool:
    """Identify browser-state failures that are recoverable by reloading login."""
    message = str(exc or "").lower()
    if isinstance(exc, AttributeError) and "has no attribute 'get'" in message:
        return True
    return any(
        marker in message
        for marker in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "cannot find context with specified id",
            "navigation_after_script",
            "detached from document",
            "target page, context or browser has been closed",
        )
    )


def _build_driver(opened: RoxyOpenResult):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    if opened.debugger_address:
        logger.info("[Roxy] Selenium 连接 debuggerAddress=%s", opened.debugger_address)
        options = Options()
        # 页面里长轮询/风控脚本偶尔会让 driver.get 等到超时；eager 只等 DOMContentLoaded。
        options.page_load_strategy = "eager"
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        driver_path = ""
        try:
            raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
            if isinstance(raw_data, dict):
                driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip()
        except Exception:  # noqa: BLE001
            driver_path = ""
        if driver_path:
            logger.info("[Roxy] 使用 Roxy chromedriver=%s", driver_path)
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        _apply_browser_automation_mask(driver)
        return driver

    if opened.webdriver_url:
        logger.info("[Roxy] Selenium 连接 webdriver_url=%s", opened.webdriver_url)
        options = Options()
        options.page_load_strategy = "eager"
        driver = RemoteWebDriver(command_executor=opened.webdriver_url, options=options)
        _apply_browser_automation_mask(driver)
        return driver

    raise RuntimeError("Roxy 未返回可连接的 Selenium 地址")


def _center_browser_window(driver) -> None:
    """把可见的 Roxy 窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        import platform
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Roxy] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Roxy] 浏览器窗口居中失败，继续执行：%s", exc)


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or _registration_timeout(driver))


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    old_timeout = _registration_timeout(driver)
    old_script_timeout = getattr(driver, "script_timeout", None)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(8)
            except Exception:  # noqa: BLE001, S110
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：url=%s attempt=%s/%s error=%s",
                _log_prefix(driver), url, attempt, attempts, str(exc).splitlines()[0] if str(exc) else "TimeoutException",
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:  # noqa: BLE001, S110
                pass
            time.sleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:  # noqa: BLE001
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:  # noqa: BLE001
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：current=%s readyState=%s",
                    _log_prefix(driver), current[:180], ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:  # noqa: BLE001, S110
                    pass
                time.sleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("%s 页面跳转失败，准备重试：url=%s attempt=%s/%s error=%s", _log_prefix(driver), url, attempt, attempts, exc)
                time.sleep(1.5 * attempt)
                continue
            raise
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:  # noqa: BLE001, S110
                pass
            if old_script_timeout is not None:
                try:
                    driver.set_script_timeout(old_script_timeout)
                except Exception:  # noqa: BLE001, S110
                    pass
    raise last_exc or RuntimeError(f"页面跳转失败: {url}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:  # noqa: BLE001
        return False


def _browser_actions_enabled() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:  # noqa: BLE001
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。"""
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
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), exc)


def _human_scroll_to(driver, el) -> None:
    native = getattr(el, "locator", None) or getattr(el, "handle", None)
    if native is not None:
        try:
            native.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001, S110
            pass
        return
    try:
        block = random.choice(["center", "nearest", "center"])
        driver.execute_script("typeof arguments[0]?.scrollIntoView === 'function' && arguments[0].scrollIntoView({block: arguments[1], inline:'nearest'});", el, block)
        if _browser_actions_enabled():
            time.sleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_script("typeof arguments[0]?.scrollIntoView === 'function' && arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
    except Exception:  # noqa: BLE001
        try:
            driver.execute_script("typeof arguments[0]?.scrollIntoView === 'function' && arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:  # noqa: BLE001, S110
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    _human_scroll_to(driver, el)
    if getattr(el, "locator", None) is not None or getattr(el, "handle", None) is not None:
        try:
            human_delay("click")
            el.click()
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Cloak locator click failed label=%s err=%s", _log_prefix(driver), label, exc)
    if not _browser_actions_enabled():
        time.sleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            el.click();
            """, el)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, exc)
        time.sleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:  # noqa: BLE001
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。"""
    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:  # noqa: BLE001, S110
                pass
        el.send_keys(value)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                el.send_keys(mod, "a")
                time.sleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:  # noqa: BLE001
                try:
                    el.clear()
                except Exception:  # noqa: BLE001, S110
                    pass
        text = str(value)
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if random.random() < 0.12 and i + 1 < len(text) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        if getattr(el, "locator", None) is not None:
            return
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        _set_element_value(driver, el, value)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:  # noqa: BLE001, S110
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or _registration_timeout(driver))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; last={last}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
        return _coerce_browser_mapping(driver, result, label="email_entry")
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input#email-input',
      'input[autocomplete="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    return null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        state = driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        const visibleEmailInput = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
          .some(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
        const actionAttrs = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('autocomplete'), el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
        const actions = actionAttrs.join(' ');
        return {
          url,
          has_email_entry: visibleEmailInput || formsWithEmail || actionAttrs.some(attrs => /email|username|passwordless|one[-_ ]?time|otp|magic/.test(attrs)),
          has_consent_action: /oauth|authorize|consent|grant|allow/.test(actions)
        };
        """) or {}
        if not isinstance(state, dict):
            return bool(state)
        return bool(state.get("has_consent_action") and not state.get("has_email_entry"))
    except Exception:  # noqa: BLE001
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:  # noqa: BLE001
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    if (typeof candidates[0].el?.scrollIntoView === 'function') candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _wait_for_email_input(driver, timeout: int | None = None):
    """进入邮箱登录/注册方式并返回已找到的可见邮箱输入框。"""
    end = time.time() + (timeout or _registration_timeout(driver))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        if driver.__class__.__name__ == "BrowserSeleniumDriver":
            try:
                el = _find_any(driver, _EMAIL_INPUT_SELECTORS, timeout=2)
                return el
            except Exception as exc:  # noqa: BLE001
                last_state = {"native_locator_error": f"{type(exc).__name__}: {exc}"}
        else:
            el = _find_visible_email_input_js(driver)
            if el:
                return
        last_state = _email_entry_state(driver)
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    _human_type_text(driver, _wait_for_email_input(driver, timeout=timeout), email, clear=True)


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    if (typeof target?.scrollIntoView === 'function') target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:  # noqa: BLE001, S110
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (typeof input?.scrollIntoView === 'function') input.scrollIntoView({block:'center', inline:'nearest'});
        if (typeof input?.focus === 'function') input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        if (typeof input?.focus === 'function') input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (typeof input?.scrollIntoView === 'function') input.scrollIntoView({block:'center', inline:'nearest'});
        if (typeof input?.focus === 'function') input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        if (typeof input?.focus === 'function') input.focus();

        if (typeof submit?.scrollIntoView === 'function') submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步执行 submit.click()：
        // ChromeDriver 会等前端 submit/navigation，Roxy/Chrome 150 上可能卡到 page/script timeout。
        // setTimeout 让 Selenium 先返回，点击在页面事件循环里异步发生，和补交逻辑一致。
        setTimeout(() => {
          try {
            if (typeof input?.focus === 'function') input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keypress', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (typeof input?.focus === 'function') input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后让浏览器跳转。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "url": current[:180]}
    except Exception:  # noqa: BLE001
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(getattr(driver, "script_timeout", 0) or _registration_timeout(driver))
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:  # noqa: BLE001, S110
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, body:csrfText.slice(0, 500)});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, body:text.slice(0, 700)});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            window.location.assign(url);
            done({ok:true, stage:'redirect', url:url.slice(0, 260)});
          } catch (e) {
            done({ok:false, stage:'exception', error:String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """, email, did, auth_log_id) or {}
        return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result", "result": str(result)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:  # noqa: BLE001, S110
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
        return _coerce_browser_mapping(driver, result, label="email_input_state")
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察几秒；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，持续清空才让上层重试。
    """
    end = time.time() + timeout
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    while time.time() < end:
        _raise_if_account_unusable(driver)
        challenge_state = _browser_challenge_state(driver)
        if challenge_state.get("is_challenge"):
            _wait_for_browser_challenge(driver, timeout=_registration_timeout(driver))
            continue
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        state = _email_input_value_state(driver)
        last = state
        inputs = state.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.time()
                if cleared_seen_at is None:
                    cleared_seen_at = now
                # URL 已带 email 查询参数时更像是提交后的中间态，给它更长观察窗口。
                debounce = 18.0 if ("/auth/login" in url and "email=" in url) else 5.0
                if now - cleared_last_log_at > 2.0:
                    logger.info(
                        "%s 邮箱提交后检测到输入框短暂清空，继续等待跳转：elapsed=%.1fs debounce=%.1fs url=%s",
                        _log_prefix(driver), now - cleared_seen_at, debounce, url[:180],
                    )
                    cleared_last_log_at = now
                if (
                    not cleared_recover_done
                    and "/auth/login" in url
                    and "email=" in url
                    and now - cleared_seen_at >= 2.0
                ):
                    recover = _recover_email_submit_if_stuck(driver, email)
                    cleared_recover_done = True
                    logger.info("%s 邮箱提交后仍停留在 login?email，中途补交一次表单：%s", _log_prefix(driver), recover)
                if now - cleared_seen_at >= debounce:
                    return "email_cleared"
            else:
                cleared_seen_at = None
            # 仍是当前邮箱页，继续短等。
        time.sleep(0.8)
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), last)
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _reset_login_page_for_retry(driver) -> None:
    """Tải lại login page trước retry để bỏ DOM SPA đã bị unmount sau submit."""
    driver.get("https://chatgpt.com/auth/login")
    human_delay("navigate")
    _maybe_accept(driver)
    _assert_not_external_idp(driver, "retry login page")


def _submit_email_and_wait_next(driver, email: str | None, attempts: int = 3, allow_login_password: bool = False, email_supplier=None) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in。"""
    last_state = None
    for attempt in range(1, attempts + 1):
        try:
            _wait_for_browser_challenge(driver, timeout=_registration_timeout(driver))
            _type_email_address(driver, email, timeout=20)
            state = _email_input_value_state(driver)
            last_state = state
            values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
            if not any(v.strip().lower() == email.strip().lower() for v in values):
                logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
                time.sleep(0.8)
                continue
            logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
            human_delay("form")
            _submit_email_step(driver, email)
            logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
            state_name = _wait_email_submit_next_state(driver, email, timeout=20)
            if state_name == "login_password" and not allow_login_password:
                raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
            if state_name in ("password", "otp", "logged_in", "login_password"):
                logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
                return state_name
            logger.warning("%s 邮箱提交后仍未进入下一步：%s，准备重填重试 state=%s", _log_prefix(driver), state_name, _email_input_value_state(driver))
            if attempt < attempts:
                _reset_login_page_for_retry(driver)
            else:
                time.sleep(1.0)
        except Exception as exc:
            if attempt >= attempts or not _is_transient_email_submission_error(exc):
                raise
            logger.warning(
                "%s 邮箱提交后的页面状态读取遇到临时错误，reload 登录页后重试：attempt=%s/%s error=%s",
                _log_prefix(driver),
                attempt,
                attempts,
                str(exc)[:180],
            )
            _reset_login_page_for_retry(driver)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _otp_input_value(driver) -> str:
    """Return the concatenated value of visible OTP inputs in DOM order."""
    state = _email_otp_page_state(driver)
    values = []
    for item in state.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        attrs = " ".join(
            str(item.get(key) or "")
            for key in ("type", "name", "id", "autocomplete", "inputmode", "ariaLabel")
        ).lower()
        if any(marker in attrs for marker in ("one-time", "otp", "code", "numeric", "tel")):
            values.append(str(item.get("value") or ""))
    return "".join(values)


def _wait_for_otp_input_value(driver, expected: str, timeout: float = 2.0) -> bool:
    """Wait briefly until React/native input state contains the complete OTP."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if _otp_input_value(driver) == expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _type_otp_with_verification(driver, expected: str, writer, input_kind: str) -> None:
    """Write an OTP and retry when the browser drops a key during React updates."""
    last_error = None
    for attempt in range(1, 4):
        try:
            writer()
        except Exception as exc:  # noqa: BLE001 - Selenium and Playwright adapters expose different transient errors.
            last_error = exc
        if _wait_for_otp_input_value(driver, expected):
            return
        actual = _otp_input_value(driver)
        if attempt < 3:
            logger.warning(
                "%s[OTP] 输入校验不匹配，清空后重试：kind=%s attempt=%s/3 expected_len=%s actual_len=%s",
                _log_prefix(driver), input_kind, attempt, len(expected), len(actual),
            )
            _clear_otp_inputs(driver)
    detail = f": {type(last_error).__name__}: {last_error}" if last_error else ""
    raise RuntimeError(f"OTP 输入校验失败，浏览器未保留完整验证码{detail}")


def _type_otp(driver, code: str) -> None:
    from selenium.webdriver.common.by import By

    expected = str(code or "").strip()
    if not expected:
        raise RuntimeError("邮箱验证码为空")

    # 单输入框
    for selector in [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        if len(els) == 1:
            def write_single_otp(selector=selector) -> None:
                current = [
                    e for e in driver.find_elements(By.CSS_SELECTOR, selector)
                    if _visible(e)
                ]
                if len(current) != 1:
                    raise RuntimeError("OTP 输入框在重试期间已重新挂载")
                _human_type_text(driver, current[0], expected, clear=True)

            _type_otp_with_verification(
                driver,
                expected,
                write_single_otp,
                "single",
            )
            return

    # 6 个分格输入框
    boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    numeric_boxes = []
    for e in boxes:
        attrs = " ".join(str(e.get_attribute(k) or "") for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= len(expected):
        def write_segmented_otp() -> None:
            current_boxes = [
                e for e in driver.find_elements(By.CSS_SELECTOR, "input")
                if _visible(e)
                and any(
                    marker in " ".join(
                        str(e.get_attribute(k) or "")
                        for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type")
                    ).lower()
                    for marker in ("numeric", "one-time", "code", "otp", "tel")
                )
            ]
            if len(current_boxes) < len(expected):
                raise RuntimeError("OTP 分格输入框在重试期间未完整挂载")
            for e, ch in zip(current_boxes, expected):
                if _browser_actions_enabled():
                    _human_scroll_to(driver, e)
                    time.sleep(random.uniform(0.04, 0.18))
                e.send_keys(ch)
                if _browser_actions_enabled():
                    human_delay("keystroke")

        _type_otp_with_verification(driver, expected, write_segmented_otp, "segmented")
        return

    raise RuntimeError("找不到 OTP 输入框")


def _email_otp_page_state(driver) -> dict:
    try:
        result = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaLabel: el.getAttribute('aria-label') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', value: el.getAttribute('value') || '',
          action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        }));
        const errors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return {url: location.href, title: document.title, inputs, buttons, errors, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
        return _coerce_browser_mapping(driver, result, label="email_otp_state")
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(driver, 'current_url', ''), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:  # noqa: BLE001
        url = ''
    if '/log-in/password' in url:
        return False
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','inputmode')) for i in (state.get('inputs') or [])).lower()
    return 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:  # noqa: BLE001, S110
        pass


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => enabled(el) && /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test((el.innerText || el.textContent || '').toLowerCase())) || null;
            """)
            if btn:
                text = str(btn.text or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                return {"ok": True, "text": text}
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={last}, state={_email_otp_page_state(driver)}")


def _wait_after_email_otp_submit(driver, timeout: int = 30) -> str:
    """提交 OTP 后等待页面离开验证码页。

    只有页面明确出现验证码错误（aria-invalid / 错误文案）才判定为无效；
    网络慢时页面跳转可能超过 10s，超时后只要没有错误标记就按 accepted 处理，
    避免把已提交成功的验证码误判为失败后误点“重新发送”把流程搞乱。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(0.5)
        if not _is_email_verification_page(driver):
            return 'accepted'
        last = _email_otp_page_state(driver)
        invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
        if invalid or (last.get('errors') or []):
            return 'invalid'
    if _is_email_verification_page(driver):
        # 超时仍停留：若无明确错误标记，判定为提交成功、跳转缓慢，按 accepted 放行。
        has_error_mark = bool(last.get('errors')) or any(
            str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or [])
        )
        if has_error_mark:
            logger.warning("%s[OTP] 提交后仍停留验证码页且存在错误标记，按验证码无效处理 snapshot=%s", _log_prefix(driver), last)
            return 'invalid'
        logger.warning(
            "%s[OTP] 提交后 %ss 仍在验证码页但无错误标记，按跳转缓慢处理（accepted） snapshot=%s",
            _log_prefix(driver), timeout, last
        )
        return 'accepted'
    return 'accepted'


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


def _complete_email_otp(
    driver,
    email: str,
    *,
    otp_after_ts: float,
    otp_code: str | None = None,
    otp_before_code: str | None | object = _OTP_BEFORE_CODE_UNSET,
    max_attempts: int = 3,
) -> None:
    """获取、提交并在失败后重发邮箱 OTP；每次重试都只接受新取到的码。"""
    current_otp = otp_code
    previous_submitted_otp = None
    last_error: Exception | None = None
    for otp_attempt in range(1, max(1, int(max_attempts)) + 1):
        if current_otp is None:
            logger.info(
                "%s[OTP] 等待验证码：%s（第 %s/%s 次）",
                _log_prefix(driver),
                email,
                otp_attempt,
                max_attempts,
            )
            try:
                wait_kwargs = {
                    "after_ts": otp_after_ts,
                    "stage": "registration_email_otp",
                }
                if otp_before_code is not _OTP_BEFORE_CODE_UNSET and otp_before_code:
                    wait_kwargs["before_code"] = otp_before_code
                elif previous_submitted_otp:
                    wait_kwargs["before_code"] = previous_submitted_otp
                current_otp = wait_for_otp(email, **wait_kwargs)
            except Exception as exc:
                last_error = exc
                if otp_attempt >= max_attempts:
                    raise
                logger.warning(
                    "%s[OTP] 取码失败，先重新发送验证码再请求新码（%s/%s）：%s: %s",
                    _log_prefix(driver),
                    otp_attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                otp_after_ts = time.time()
                otp_before_code = snapshot_verification_code(
                    email,
                    stage="registration_email_resend",
                )
                _click_resend_email_otp(driver, timeout=25)
                human_delay("api")
                continue

        try:
            logger.info("%s[OTP] 收到验证码：%s", _log_prefix(driver), current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            previous_submitted_otp = current_otp
            logger.info("%s[OTP] 已填写邮箱验证码", _log_prefix(driver))
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("%s[OTP] 已提交邮箱验证码，等待资料页或登录态", _log_prefix(driver))
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "%s[OTP] 未找到显式提交按钮，继续等待页面状态：%s",
                    _log_prefix(driver),
                    str(exc)[:120],
                )
            outcome = _wait_after_email_otp_submit(driver, timeout=30)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            outcome = "error"

        if outcome == "accepted":
            acknowledge_verification_code(
                email,
                current_otp,
                stage="registration_email_otp",
            )
            return
        if otp_attempt >= max_attempts:
            if outcome == "invalid":
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            if last_error is not None:
                raise last_error
            raise RuntimeError("邮箱验证码提交失败，已达到最大重试次数")

        logger.warning(
            "%s[OTP] 验证码提交失败，准备重新发送并重新获取验证码（%s/%s）",
            _log_prefix(driver),
            otp_attempt + 1,
            max_attempts,
        )
        otp_after_ts = time.time()
        otp_before_code = snapshot_verification_code(
            email,
            stage="registration_email_resend",
        )
        if previous_submitted_otp and not otp_before_code:
            otp_before_code = current_otp
        _click_resend_email_otp(driver, timeout=25)
        human_delay("api")
        current_otp = None


def _maybe_accept(driver) -> None:
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            time.sleep(0.5)
        except Exception:  # noqa: BLE001, S110
            pass


def _page_snapshot(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          value: el.value || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
          type: el.getAttribute('type') || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        const errors = [...document.querySelectorAll('[role="alert"],[aria-live="assertive"],.react-aria-FieldError,[slot="errorMessage"],[id$="-error"]')]
          .filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length))
          .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
          .filter(Boolean).slice(0, 10);
        return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2000), inputs, buttons, widgets, errors};
        """) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _has_access_token(driver) -> bool:
    try:
        result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('https://chatgpt.com/api/auth/session', {credentials:'include'})
          .then(r => r.json()).then(j => done(Boolean(j && j.accessToken)))
          .catch(() => done(false));
        """)
        if isinstance(result, dict):
            return bool(result.get("accessToken"))
        return bool(result)
    except Exception:  # noqa: BLE001
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    if (typeof el?.scrollIntoView === 'function') el.scrollIntoView({block:'center'});
    if (typeof el?.focus === 'function') el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'BrowserElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:  # noqa: BLE001
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:  # noqa: BLE001
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            _human_type_text(driver, el, str(value), clear=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug('%s 填写字段失败 selectors=%s value=%s err=%s', _log_prefix(driver), selectors, value, exc)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:  # noqa: BLE001, S110
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("typeof arguments[0]?.scrollIntoView === 'function' && arguments[0].scrollIntoView({block:'center'}); typeof arguments[0]?.focus === 'function' && arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:  # noqa: BLE001
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), exc)
        return None


def _generate_roxy_password() -> str:
    """参考 FlowPilot 密码策略：8~64 位，含大小写、数字、符号。"""
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    symbols = '!@#$%^&*?_-+=' 
    groups = [upper, lower, digits, symbols]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, 'REGISTER_PASSWORD', '') or '').strip()
        if configured:
            return configured
    except Exception:  # noqa: BLE001, S110
        pass
    return _generate_roxy_password()


def _password_page_state(driver) -> dict:
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el), value: el.type === 'password' ? '<password>' : (el.value || '')
        })).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, inputs, forms, buttons};
        """) or {}
        return _coerce_browser_mapping(driver, result, label="password_state")
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _password_submission_error(driver) -> str | None:
    """Return a visible password-submit error without exposing form values."""
    snapshot = _page_snapshot(driver)
    if not isinstance(snapshot, dict):
        return None
    url = str(snapshot.get("url") or "").lower()
    if not any(x in url for x in ("/create-account/password", "/u/signup/password", "/signup/password")):
        return None

    messages: list[str] = []
    for value in snapshot.get("errors") or []:
        message = " ".join(str(value or "").split()).strip()
        if message and message not in messages:
            messages.append(message[:240])
    if messages:
        return "; ".join(messages[:3])

    text = " ".join(str(snapshot.get("text") or "").split()).lower()
    markers = (
        "cannot create your account",
        "could not create your account",
        "unable to create your account",
        "something went wrong",
        "an error occurred",
        "try again",
        "vui lòng thử lại",
        "không tạo được tài khoản",
        "无法创建账号",
        "无法创建账户",
        "请重试",
    )
    for marker in markers:
        if marker in text:
            return marker
    return None


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:  # noqa: BLE001
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    return '/log-in/password' in url


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) return {ok:false, reason:'missing_passwordless_button'};
        if (typeof btn?.scrollIntoView === 'function') btn.scrollIntoView({block:'center'});
        return {
          ok:true,
          reason:'passwordless_send_otp_target',
          button: btn,
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if result.get("ok") and result.get("button"):
            _human_click(driver, result.get("button"), label="passwordless_otp")
            result["reason"] = "clicked_passwordless_send_otp"
            result.pop("button", None)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _click_continue_with_password_link(driver) -> bool:
    """Khi ChatGPT landed trên email-verification page, click 'Continue with password'
    hoặc navigate thẳng đến /create-account/password để force password step.

    Giống JnmBrowser engine.rs:3636-3678 — không bao giờ đi OTP-only.
    """
    clicked = False
    try:
        clicked = bool(driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const bad = /passwordless|one[-_\s]?time|otp|magic|code/;
        const good = /continue.*password|password.*continue|continuar.*senha|senha.*continuar/;
        const candidates = [...document.querySelectorAll('button,a,[role="button"],[role="link"]')]
          .filter(visible)
          .filter(el => {
            const text = norm(el.textContent || '');
            const attrs = [el.id, el.getAttribute('name'), el.getAttribute('aria-label'),
              el.getAttribute('title'), el.getAttribute('data-testid'), el.className]
              .join(' ').toLowerCase();
            return good.test(text) || good.test(attrs);
          })
          .filter(el => !bad.test(norm(el.textContent || '')));
        if (candidates.length < 1) return false;
        if (typeof candidates[0]?.scrollIntoView === 'function') candidates[0].scrollIntoView({block:'center'});
        candidates[0].click();
        return true;
        """) or False)
    except Exception:  # noqa: BLE001, S110
        pass
    if clicked:
        logger.info("%s 已点击 'Continue with password' 链接，等待密码表单", _log_prefix(driver))
        time.sleep(2.0)
        return True
    logger.info("%s 未找到 'Continue with password' 链接，直接导航到 /create-account/password", _log_prefix(driver))
    try:
        driver.get("https://auth.openai.com/create-account/password")
        time.sleep(2.0)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 导航到 /create-account/password 失败: %s", _log_prefix(driver), exc)
        return False


def _fill_password_page_if_present(driver, email: str, timeout: int = 25) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    last = {}
    verification_redirected = False
    while time.time() < end:
        _raise_if_account_unusable(driver)
        if _is_email_verification_page(driver):
            if verification_redirected:
                time.sleep(0.5)
                continue
            # The session endpoint can expose a token before email verification
            # completes. Always leave verification for the signup password step.
            _click_continue_with_password_link(driver)
            verification_redirected = True
            continue
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            time.sleep(0.5)
            continue
        # Force password: không click passwordless OTP, luôn fill password (yêu cầu user).
        # _click_passwordless_signup_if_present đã bị bỏ để không bao giờ đi OTP-only.
        if is_login_password:
            logger.info("%s 当前是登录密码页（已注册邮箱），跳过密码填写：state=%s", _log_prefix(driver), last)
            return None
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        if (typeof buttons[0].el?.scrollIntoView === 'function') buttons[0].el.scrollIntoView({block:'center'});
        return {ok:true, reason:'password_targets', input, button: buttons[0].el};
        """) or {}
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        _human_type_text(driver, result.get("input"), password, clear=True)
        human_delay("form", minimum=0.4, maximum=1.4)
        initial_url = str(getattr(driver, "current_url", "") or "")
        _human_click(driver, result.get("button"), label="password_submit")
        _wait_after_password_submit(driver, initial_url, timeout=min(5.0, max(0.0, float(timeout))))
        logger.info("%s 已填写并提交密码页", _log_prefix(driver))
        # 提交密码后通常进入邮箱验证码页，最多等一段时间。
        wait_end = time.time() + 20
        while time.time() < wait_end:
            _raise_if_account_unusable(driver)
            if _is_email_verification_page(driver):
                logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                return password
            if _has_access_token(driver):
                logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                return password
            if _is_signup_password_page(driver):
                submission_error = _password_submission_error(driver)
                if submission_error:
                    raise RuntimeError(f"密码页提交失败：{submission_error}")
            else:
                return password
            time.sleep(0.5)
        if _is_signup_password_page(driver):
            submission_error = _password_submission_error(driver)
            if submission_error:
                raise RuntimeError(f"密码页提交失败：{submission_error}")
            raise RuntimeError("密码页提交后未进入邮箱验证码页，仍停留在注册密码页")
        return password
    if verification_redirected:
        raise RuntimeError(
            f"邮箱验证码页未能跳转到 create-account/password，拒绝绕过密码步骤: state={last}"
        )
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            const scrollTarget = label && visible(label) ? label : el;
            if (typeof scrollTarget?.scrollIntoView === 'function') scrollTarget.scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:  # noqa: BLE001
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _profile_submission_error(snapshot: dict) -> str | None:
    """Return a terminal about-you error reported by the provider, if present."""
    url = str(snapshot.get("url") or "").lower()
    if not any(x in url for x in ("about-you", "profile", "create-account/about", "signup/profile")):
        return None
    messages = [str(snapshot.get("text") or "")]
    messages.extend(str(value or "") for value in (snapshot.get("errors") or []))
    text = " ".join(messages).replace("\\n", " ").strip()
    markers = (
        "利用規約のため、お客様のアカウントを作成できません。",
        "利用規約のため、お客様のアカウントを作成できません",
        "cannot create your account due to the terms of use.",
        "cannot create your account due to the terms",
        "cannot create your account because of the terms of use.",
        "cannot create your account because of the terms",
        "this email is not supported.",
        "this email is not supported",
        "email address is not supported.",
        "email is not supported",
        "email address is not supported",
        "email domain is not supported.",
        "email domain is not supported",
        "unsupported email.",
        "unsupported email",
        "email not supported",
        "email is unsupported",
        "email isn't supported",
    )
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            start = lowered.find(marker.lower())
            return text[start:start + len(marker)]
    if "cannot create your account" in lowered and "terms" in lowered:
        start = lowered.find("cannot create your account")
        return text[start:start + 500]
    return None


def _is_unsupported_email_error(error: object) -> bool:
    """Return whether an about-you failure explicitly rejects the email/domain."""
    text = str(error or "").lower()
    if "about-you 提交失败" not in text:
        return False
    return any(marker in text for marker in (
        "this email is not supported",
        "email is not supported",
        "email address is not supported",
        "email domain is not supported",
        "unsupported email",
        "email not supported",
        "email is unsupported",
        "email isn't supported",
    ))


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    today = local_today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                time.sleep(1)
                profile_error = _profile_submission_error(_page_snapshot(driver))
                if profile_error:
                    logger.error(
                        '%s about-you 提交被服务端拒绝：%s',
                        _log_prefix(driver),
                        profile_error,
                    )
                    raise RuntimeError(f"about-you 提交失败：{profile_error}")
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            if (typeof submit?.scrollIntoView === 'function') submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          if (typeof submitters[0]?.scrollIntoView === 'function') submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          if (typeof buttons[0]?.scrollIntoView === 'function') buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取 /api/auth/session，拿不到 token 返回 None。"""
    script = r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include'})
      .then(r => r.json())
      .then(j => done({ok: true, data: j}))
      .catch(e => done({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script)
    if result and result.get("ok"):
        data = result.get("data") or {}
        if data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            return data
        logger.info("%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s", _log_prefix(driver), list(data.keys()))
    return None


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:  # noqa: BLE001
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("切换浏览器窗口探测失败：%s: %s", type(exc).__name__, exc)
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:  # noqa: BLE001, S110
                pass
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False

    while time.time() < end:
        _check_manual_stop()
        try:
            current = str(driver.current_url or '')
        except Exception:  # noqa: BLE001
            current = ''

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    time.sleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:  # noqa: BLE001
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current:
            try:
                data = _read_chatgpt_session_once(driver)
                if data:
                    return data
                last_data = "session 暂无 accessToken"
            except Exception as exc:  # noqa: BLE001
                last_data = f"{type(exc).__name__}: {exc}"
        time.sleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def run_roxy_registration(email: str, name: str, birthday: str, proxy: str | None = None, otp_code: str | None = None, batch_dir: Path | None = None) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    client = RoxyBrowserClient()
    opened = None
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    network_identity: dict | None = None
    tunnel = getattr(proxy, "tunnel", None)
    if tunnel is not None:
        network_identity = {
            **tunnel.network_identity(),
            "profile_id": None,
            "verified": False,
        }
    try:
        opened = client.open_profile(proxy=proxy, stop_check=_check_manual_stop)
        if tunnel is not None:
            pool = getattr(tunnel, "pool", None)
            if pool is not None:
                pool.bind_profile(tunnel, opened.profile_id)
            from core.registration_network_identity import network_identity_for_tunnel

            network_identity = network_identity_for_tunnel(tunnel, opened.profile_id)
        driver = _build_driver(opened)
        from core.registration_network_identity import probe_browser_geo

        browser_geo = probe_browser_geo(driver)
        if network_identity is not None:
            from core.registration_network_identity import (
                NetworkIdentityError,
                verify_profile_network_identity,
            )

            try:
                network_identity = verify_profile_network_identity(driver, network_identity)
                logger.info(
                    "[Roxy网络] profile=%s port=%s pid=%s server=%s tunnel_ip=%s browser_ip=%s verified=true",
                    opened.profile_id,
                    network_identity.get("local_port"),
                    network_identity.get("wireproxy_pid"),
                    network_identity.get("server_hostname"),
                    network_identity.get("tunnel_egress_ip"),
                    network_identity.get("browser_egress_ip"),
                )
            except NetworkIdentityError as exc:
                logger.warning(
                    "[Roxy网络] IP 验证失败（允许继续）: profile=%s error=%s",
                    opened.profile_id,
                    str(exc)[:200],
                )
                # Graceful degradation - 继续注册流程，不要求 IP 验证通过
                network_identity["verified"] = False
                network_identity["verification_error"] = str(exc)[:500]
        if network_identity is None:
            network_identity = {"verified": False}
        if browser_geo:
            network_identity["browser_geo"] = browser_geo
            network_identity.setdefault("browser_egress_ip", browser_geo.get("ip"))
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        try:
            set_script_timeout = getattr(driver, "set_script_timeout", None)
            if callable(set_script_timeout):
                set_script_timeout(max(1, int(getattr(_cfg, "ROXY_SCRIPT_TIMEOUT", _cfg.ROXY_SELENIUM_TIMEOUT))))
        except Exception:  # noqa: BLE001, S110
            pass
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_before_code = snapshot_verification_code(
            email,
            stage="registration_email_request",
        )
        otp_after_ts = time.time()
        logger.info("[Roxy注册] 打开登录页：https://chatgpt.com/auth/login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Roxy注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        # Luôn luôn force password: dù next_state là "otp" hay password, đều phải tạo password.
        # Giống JnmBrowser: click "Continue with password" hoặc navigate /create-account/password
        # trước khi fill password, không bao giờ đi OTP-only.
        if next_state == "otp":
            _click_continue_with_password_link(driver)
            _check_manual_stop()
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        if openai_password:
            create_acknowledged = True
        _check_manual_stop()

        _complete_email_otp(
            driver,
            email,
            otp_after_ts=otp_after_ts,
            otp_code=otp_code,
            otp_before_code=otp_before_code,
            max_attempts=3,
        )

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Roxy注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("[Roxy注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Roxy注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        account_id = checkpoint_account_data(
            email=email,
            access_token=access_token,
            email_source=resolve_email_source(email),
            proxy_used=str(proxy) if proxy else None,
            registration_ip=(network_identity or {}).get("browser_egress_ip"),
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "device_id": getattr(driver, "device_id", None),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "network_identity": network_identity,
                "registration_password": openai_password,
                "registration_driver": "roxy",
            },
        )
        logger.info("[Roxy注册] token 检查点已保存：account_id=%s twofa=pending", account_id)
        _check_manual_stop()

        totp_secret = None
        twofa_status = "disabled"
        twofa_error = None
        if _twofa_cfg.ENABLE_2FA:
            logger.info("[Roxy注册] 注册 session 已建立，等待首页状态稳定后再设置 2FA")
            human_delay("post_auth", minimum=2.0, maximum=4.0)
            from core.account_export import setup_2fa_for_registration
            try:
                totp_secret = setup_2fa_for_registration(driver, email)
                twofa_status = "active"
                db.update_account_2fa(account_id, status="active", totp_secret=totp_secret)
            except Exception as exc:  # noqa: BLE001
                twofa_status = "failed"
                twofa_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                db.update_account_2fa(account_id, status="failed", error=twofa_error)
                logger.error("[Roxy注册] 2FA 设置失败，账号已保留待重试：%s", twofa_error)
                return {
                    "success": False,
                    "email": email,
                    "account_id": account_id,
                    "access_token": access_token,
                    "twofa_status": twofa_status,
                    "twofa_error": twofa_error,
                    "network_identity": network_identity,
                    "error": f"2FA 设置失败，账号已保存：{twofa_error}",
                }

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        free_codex_auto_enabled = False
        try:
            from config import codex as _codex_cfg
            from config import register as _register_cfg
            from core.codex_oauth import run_codex_oauth
            free_codex_auto_enabled = bool(
                getattr(_register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
            )
            codex_credentials = None
            if openai_password and totp_secret:
                codex_credentials = CodexLoginCredentials(
                    email=email,
                    password=openai_password,
                    totp_secret=totp_secret,
                )

            def _run_codex_in_current_browser() -> dict:
                # 注册流程本身已创建 Roxy 一号一环境。这里不能再新建第二个 Roxy 环境；
                # 复用当前注册窗口，先清理 Cookie/session/localStorage/cache，再开始 Codex 授权。
                login_mode = (
                    "密码 + authenticator TOTP"
                    if codex_credentials
                    else "邮箱 OTP fallback（注册密码或 TOTP 不完整）"
                )
                logger.info(
                    "[Roxy注册][Codex] 复用当前注册 Roxy 窗口执行 Codex 授权，不创建新环境，登录方式=%s",
                    login_mode,
                )
                _check_manual_stop()
                return run_codex_oauth(
                    email,
                    oauth_driver="roxy",
                    force=True,
                    credentials=codex_credentials,
                    existing_driver=driver,
                    existing_opened=opened,
                )

            post_auth_automation_enabled = bool(
                getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
                or free_codex_auto_enabled
                or bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False))
            )
            if post_auth_automation_enabled:
                from core.registration_auto_codex import run_registration_auto_codex

                auto_codex = run_registration_auto_codex(
                    account_id=account_id,
                    email=email,
                    access_token=access_token,
                    proxy=proxy,
                    browser_transport=BrowserPageTransport(driver),
                    run_codex=_run_codex_in_current_browser,
                    twofa_status=twofa_status,
                )
                codex_result = auto_codex["codex"]
            else:
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:  # noqa: BLE001
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=str(proxy) if proxy else None,
            registration_ip=(network_identity or {}).get("browser_egress_ip"),
            batch_dir=batch_dir,
            auto_plan_check=False,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "network_identity": network_identity,
                "registration_password": openai_password,
                "registration_driver": "roxy",
                "twofa_status": twofa_status,
                "twofa_error": twofa_error,
                "codex": codex_result,
            },
        )
        post_register_dwell(email, label="Roxy注册")
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "twofa_status": twofa_status,
            "twofa_error": twofa_error,
            "codex": codex_result,
            "network_identity": network_identity,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Roxy注册] 失败详情", exc_info=True)
        # 未确认创建前回收邮箱；已提交密码或已撞到登录密码页时避免重复使用。
        try:
            from core.email_provider import release_email
            error_text = str(exc)
            note_text = account_unusable_message(exc.error_code) if isinstance(exc, AccountUnusableError) else error_text
            if "about-you 提交失败" in error_text and (
                "利用規約" in error_text
                or "terms of use" in error_text.lower()
                or "cannot create your account" in error_text.lower()
            ):
                try:
                    from core.paymesh_mail_client import block_account_card
                    block_account_card(email, reason="terms_rejected")
                except Exception:
                    logger.debug("%s 标记 Paymesh card blocked 失败", _log_prefix(driver), exc_info=True)
            release_status = "disabled" if (
                isinstance(exc, AccountUnusableError)
                or "account_deactivated" in error_text
                or "account_deleted" in error_text
                or "account_banned" in error_text
                or _is_unsupported_email_error(error_text)
            ) else "failed" if (
                create_acknowledged
                or "邮箱提交后进入登录密码页" in error_text
                or "auth.openai.com/log-in/password" in error_text
                or "/log-in/password" in error_text
            ) else "available"
            release_email(email, status=release_status, note=note_text[:180])
        except Exception:  # noqa: BLE001, S110
            pass
        return {
            "success": False,
            "email": email,
            "network_identity": network_identity,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:  # noqa: BLE001, S110
                pass
        if opened is not None and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
