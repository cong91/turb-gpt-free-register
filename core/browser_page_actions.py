"""PageDriver port and shared page-action primitives for browser adapters.

Provider-specific behavior is expressed through adapter capabilities, not driver
name branches.
"""
from __future__ import annotations

import logging
import random
import time
from enum import Enum
from typing import Any, Protocol

from core.browser_challenge import (
    browser_challenge_state as _browser_challenge_state,
)
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)


class PageState(str, Enum):
    """Shared auth page state with canonical vocabulary."""

    LOGIN_PASSWORD = "login_password"
    PASSWORD = "password"
    OTP = "otp"
    LOGGED_IN = "logged_in"
    CHATGPT = "chatgpt"
    PROFILE = "profile"
    DEACTIVATED = "deactivated"
    EMAIL_PAGE = "email_page"
    EMAIL_CLEARED = "email_cleared"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: str) -> PageState:
        """Map biến thể vocabulary cũ về canonical ('email_verification' → OTP)."""
        raw = str(value or "").strip().lower()
        if raw.startswith("deactivated:"):
            return cls.DEACTIVATED
        aliases = {
            "email_verification": cls.OTP,
            "email_otp": cls.OTP,
            "signup": cls.PASSWORD,
            "other": cls.UNKNOWN,
            **{member.value: member for member in cls},
        }
        return aliases.get(raw, cls.UNKNOWN)


class AuthFlowStateReader(Protocol):
    """Optional bounded state-reading capability used by Playwright adapters."""

    def read_auth_flow_state(self, *, timeout_ms: int = 400) -> dict[str, Any]: ...


class PageDriver(Protocol):
    """Capability surface required by shared page operations."""

    @property
    def current_url(self) -> str: ...

    @property
    def window_handles(self) -> list[str]: ...

    @property
    def script_timeout(self) -> int: ...

    def get(self, url: str) -> None: ...

    def execute_script(self, script: str, *args: Any) -> Any: ...

    def execute_async_script(self, script: str, *args: Any) -> Any: ...

    def find_element(self, by: Any, selector: str) -> Any: ...

    def find_elements(self, by: Any, selector: str) -> list[Any]: ...

    def set_page_load_timeout(self, timeout: int) -> None: ...

    def set_script_timeout(self, timeout: int) -> None: ...

    def save_screenshot(self, filename: str) -> bool: ...

    def clear_cookies(self) -> None: ...

    # Optional bounded state-reading capability for adapters exposing page objects:
    browser: Any
    context: Any
    page: Any



def _log_prefix(driver=None) -> str:
    """Prefix log theo capability `_registration_log_prefix` trên driver."""
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
    except Exception:  # noqa: BLE001, S110
        pass
    return "[Browser注册]"


def _registration_timeout(driver=None, fallback: int | None = None) -> int:
    """Resolve timeout cho lane hiện tại qua capability `_registration_timeout`."""
    explicit = getattr(driver, "_registration_timeout", None)
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            pass
    return max(1, int(fallback or 90))


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or _registration_timeout(driver))


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


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """Navigate with bounded retries and restore adapter timeouts."""
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
    """Click with adapter capabilities and native fallback."""
    _human_scroll_to(driver, el)
    if getattr(el, "locator", None) is not None or getattr(el, "handle", None) is not None:
        try:
            human_delay("click")
            el.click()
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Native locator click failed label=%s err=%s", _log_prefix(driver), label, exc)
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


def typing_js_fallback_allowed(driver) -> bool:
    """Capability hook cho typing policy.

    Local Selenium-compatible adapters default to the JS-setter fallback.
    Remote browser adapters may set `_typing_js_fallback_allowed = False` to
    disable that fallback; _human_type_text enforces the policy.
    """
    allowed = getattr(driver, "_typing_js_fallback_allowed", None)
    return True if allowed is None else bool(allowed)


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """Typing policy shared by local and cloud browser adapters."""
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
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        if not typing_js_fallback_allowed(driver):
            # Remote backend forbids the JS-setter fallback.
            raise RuntimeError(f"人工输入失败，已禁止瞬时 fill 兜底: {type(exc).__name__}: {exc}") from exc
        if not _set_element_value(driver, el, value):
            raise RuntimeError("输入控件在页面重绘期间失效，无法写入值") from exc


def _set_element_value(driver, el, value: str) -> bool:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    return driver.execute_script(r"""
    const el = arguments[0];
    if (!el) return false;
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
    return true;
    """, el, value)


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


def _is_chrome_error_page(driver) -> bool:
    """Detect browser network errors and server-generated HTTP 500 pages."""
    try:
        url = str(driver.current_url or "").lower()
    except Exception:  # noqa: BLE001
        url = ""
    if url.startswith("chrome-error://") or "net::err_" in url:
        return True
    state = _email_otp_page_state(driver)
    if not isinstance(state, dict):
        return False
    errors = state.get("errors")
    if not isinstance(errors, (list, tuple)):
        errors = []
    text = f"{state.get('text') or ''} {' '.join(str(e) for e in errors)}".lower()
    return "http error 500" in text or "isn't working" in text or "isn’t working" in text


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


def _resolve_email_submit_state(driver, *, token_timeout_ms: int | None = None) -> str | None:
    """Phân loại trang sau khi submit email theo precedence duy nhất.

    login_password > password > otp > logged_in. Trang vừa có URL /log-in/password
    vừa có OTP DOM (trạng thái ambiguous) được xử lý theo login_password —
    email đã đăng ký/不可用 — thay vì误判 thành trang验证码. Trả về None khi chưa
    rơi vào một trong bốn trạng thái này.

    Declared behavior change (cloak lane): lane Cloak trước đây dùng bản
    password-check TRƯỚC (browser_registration fork, login_password kiểm tra sau
    cùng); từ flow dùng chung mọi lane đều áp dụng precedence trên — trang
    /log-in/password luôn được nhận diện là login_password trước tiên.
    """
    if _is_login_password_page(driver):
        return PageState.LOGIN_PASSWORD.value
    if _is_signup_password_page(driver):
        return PageState.PASSWORD.value
    if _is_email_verification_page(driver):
        return PageState.OTP.value
    if _has_access_token(driver, timeout_ms=token_timeout_ms):
        return PageState.LOGGED_IN.value
    return None


def _is_transient_email_submission_error(exc: Exception) -> bool:
    """Identify browser-state failures that are recoverable by reloading login."""
    message = str(exc or "").lower()
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
        return True
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


def _auth_flow_url_state(url: object) -> str | None:
    """Classify auth transitions in login_password > password > OTP order."""
    lower_url = str(url or "").lower()
    if "/log-in/password" in lower_url:
        return "login_password"
    if any(
        marker in lower_url
        for marker in ("/create-account/password", "/u/signup/password", "/signup/password")
    ):
        return "password"
    if any(marker in lower_url for marker in ("email-verification", "email_otp")):
        return "otp"
    if "verify" in lower_url and "email" in lower_url:
        return "otp"
    return None


def _wait_email_submit_next_state_with_bounded_adapter(
    driver,
    timeout: float,
    read_auth_flow_state,
) -> str:
    """Wait for auth transitions without unbounded synchronous JS probes."""
    end = time.monotonic() + max(0.0, float(timeout))
    last_url = ""
    while time.monotonic() < end:
        remaining = max(0.0, end - time.monotonic())
        try:
            last_url = str(getattr(driver, "current_url", "") or "")
        except Exception:  # noqa: BLE001
            last_url = ""
        url_state = _auth_flow_url_state(last_url)
        if url_state:
            return url_state
        try:
            snapshot = read_auth_flow_state(
                timeout_ms=max(1, min(400, int(remaining * 1000))),
            ) or {}
        except Exception as exc:
            if _is_transient_email_submission_error(exc):
                snapshot = {}
            else:
                raise
        state = PageState.coerce(snapshot.get("state"))
        if state is PageState.LOGIN_PASSWORD:
            return state.value
        if state is PageState.PASSWORD:
            return state.value
        if state is PageState.OTP:
            return state.value
        if state in {PageState.DEACTIVATED, PageState.PROFILE}:
            suffix = str(snapshot.get("error_code") or "").strip()
            return f"{state.value}:{suffix}" if suffix else state.value
        if state is PageState.CHATGPT:
            return PageState.LOGGED_IN.value
        if _has_access_token(driver, timeout_ms=max(1, int(min(5.0, remaining) * 1000))):
            return PageState.LOGGED_IN.value
        time.sleep(min(0.8, remaining))
    return "email_page" if "/auth/login" in last_url.lower() else "unknown"


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


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 login_password / password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Auth-page submission can briefly navigate to `chatgpt.com/auth/login?email=...`
    and React may temporarily clear the email input. Debounce that intermediate
    state so the shared flow does not refill before the real transition.
    """
    end = time.monotonic() + max(0.0, float(timeout))
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    read_auth_flow_state = getattr(driver, "read_auth_flow_state", None)
    if callable(read_auth_flow_state):
        return _wait_email_submit_next_state_with_bounded_adapter(
            driver,
            timeout,
            read_auth_flow_state,
        )
    while time.monotonic() < end:
        remaining = max(0.0, end - time.monotonic())
        challenge_state = _browser_challenge_state(driver)
        if challenge_state.get("is_challenge"):
            _wait_for_browser_challenge(
                driver,
                timeout=min(float(_registration_timeout(driver)), remaining),
            )
            continue
        # Precedence duy nhất (login_password > password > otp > logged_in) —
        # xem _resolve_email_submit_state.
        state = _resolve_email_submit_state(
            driver,
            token_timeout_ms=max(1, int(min(5.0, remaining) * 1000)),
        )
        if state is not None:
            return state
        state_dict = _email_input_value_state(driver)
        last = state_dict
        inputs = state_dict.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state_dict.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.monotonic()
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
    last_inputs = last.get("inputs") if isinstance(last, dict) else None
    return "email_page" if last_inputs else "unknown"


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


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[@data-dd-action-name='Continue']",
        "//button[@data-dd-action-name='continue']",
        "//button[@data-login-web-auth-control='true' and @type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '続行')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


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


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            # Phân nhánh theo capability element (locator/handle của adapter
            # Playwright) thay vì tên class.
            if getattr(el, "locator", None) is not None or getattr(el, "handle", None) is not None:
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


def _has_access_token(driver, *, timeout_ms: int | None = None) -> bool:
    """Check for a session token without allowing a page fetch to stall polling."""
    if timeout_ms is None:
        timeout_ms = min(5000, max(1000, _registration_timeout(driver) * 1000))
    else:
        try:
            timeout_ms = max(1, int(timeout_ms))
        except (TypeError, ValueError):
            timeout_ms = 1000

    request_session = getattr(driver, "get_chatgpt_auth_session", None)
    if callable(request_session):
        try:
            result = request_session(timeout_ms=timeout_ms)
            return isinstance(result, dict) and bool(result.get("accessToken"))
        except Exception:  # noqa: BLE001
            return False

    try:
        script = f"""
        const timeoutMs = {timeout_ms};
        const done = arguments[arguments.length - 1];
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        const options = {{credentials: 'include'}};
        if (controller) options.signal = controller.signal;
        let settled = false;
        const finish = value => {{
          if (settled) return;
          settled = true;
          done(value);
        }};
        const timer = setTimeout(() => {{
          try {{ if (controller) controller.abort(); }} catch (_) {{}}
          finish(false);
        }}, timeoutMs);
        fetch('https://chatgpt.com/api/auth/session', options)
          .then(r => r.json())
          .then(j => finish(Boolean(j && j.accessToken)))
          .catch(() => finish(false))
          .finally(() => clearTimeout(timer));
        """
        result = driver.execute_async_script(script)
        if isinstance(result, dict):
            return bool(result.get("accessToken"))
        return bool(result)
    except Exception:  # noqa: BLE001
        return False
