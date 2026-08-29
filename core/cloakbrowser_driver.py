"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)


def _is_navigation_context_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "most likely because of a navigation" in message
        or "cannot find context with specified id" in message
    )


def _is_illegal_invocation_error(exc: Exception) -> bool:
    return "illegal invocation" in str(exc or "").lower()


def _is_transient_navigation_error(exc: Exception) -> bool:
    """Return whether Chromium can reasonably retry the failed navigation."""
    message = str(exc or "").lower()
    return any(
        marker in message
        for marker in (
            "err_empty_response",
            "err_connection_reset",
            "err_connection_closed",
            "err_connection_refused",
            "err_timed_out",
            "err_network_changed",
            "err_proxy_connection_failed",
        )
    )


def _is_navigation_timeout_error(exc: Exception) -> bool:
    """Recognize Playwright's page.goto timeout without importing Playwright types."""
    message = str(exc or "").lower()
    return "page.goto:" in message and "timeout" in message


def _has_usable_navigation_document(page, target_url: str) -> bool:
    """Return whether a timed-out navigation left a usable auth-domain document."""
    try:
        current_url = str(getattr(page, "url", "") or "")
        target_host = (urlsplit(str(target_url or "")).hostname or "").lower()
        current_host = (urlsplit(current_url).hostname or "").lower()
    except (TypeError, ValueError):
        return False

    if not target_host or not current_host or current_host == "about:blank":
        return False
    if current_host != target_host and {current_host, target_host} != {
        "chatgpt.com",
        "auth.openai.com",
    }:
        return False

    try:
        state = page.evaluate(
            "() => ({readyState: document.readyState, hasBody: !!document.body})"
        ) or {}
    except Exception:
        return False
    return bool(state.get("hasBody")) and state.get("readyState") in {
        "loading",
        "interactive",
        "complete",
    }


_SELENIUM_KEY_NAMES = {
    "\ue003": "Backspace",
    "\ue004": "Tab",
    "\ue005": "Clear",
    "\ue006": "Return",
    "\ue007": "Enter",
    "\ue008": "Shift",
    "\ue009": "Control",
    "\ue00a": "Alt",
    "\ue00b": "Pause",
    "\ue00c": "Escape",
    "\ue00d": "Space",
    "\ue00e": "PageUp",
    "\ue00f": "PageDown",
    "\ue010": "End",
    "\ue011": "Home",
    "\ue012": "ArrowLeft",
    "\ue013": "ArrowUp",
    "\ue014": "ArrowRight",
    "\ue015": "ArrowDown",
    "\ue016": "Insert",
    "\ue017": "Delete",
    "\ue031": "Meta",
    "\ue03d": "Meta",
}
_SELENIUM_MODIFIERS = {"Shift", "Control", "Alt", "Meta"}


@dataclass
class CloakOpenResult:
    # Cloak launches an isolated session rather than a reusable Roxy profile.
    profile_id: str = field(
        default_factory=lambda: f"cloakbrowser:{uuid.uuid4().hex}"
    )
    raw: dict | None = None


class BrowserElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    @property
    def text(self) -> str:
        """Selenium-compatible visible text used by resend/profile helpers."""
        try:
            if self.locator is not None:
                return str(self.locator.inner_text(timeout=1000) or "")
            return str(self.handle.inner_text(timeout=1000) or "")
        except Exception:
            try:
                return str(self._eval("el => el.innerText || el.textContent || ''") or "")
            except Exception:
                return ""

    def send_keys(self, *values: str) -> None:
        """发送 Selenium 风格按键，同时保留逐字符调用的累积输入。"""
        if self.locator is not None:
            self.locator.focus(timeout=10000)
        else:
            self.handle.focus()
        index = 0
        while index < len(values):
            token = str(values[index] or "")
            if token in _SELENIUM_MODIFIERS:
                index += 1
                continue
            if token in _SELENIUM_KEY_NAMES:
                key = _SELENIUM_KEY_NAMES[token]
                if key in _SELENIUM_MODIFIERS and index + 1 < len(values):
                    next_token = str(values[index + 1] or "")
                    if next_token and next_token not in _SELENIUM_KEY_NAMES:
                        self.page.keyboard.press(f"{key}+{next_token}")
                        index += 2
                        continue
                self.page.keyboard.press(key)
                index += 1
                continue
            # Cloak humanize may click again inside locator.press_sequentially,
            # moving the caret. Focus is stable, then page keyboard preserves it.
            self.page.keyboard.type(token, delay=25)
            index += 1

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: BrowserSeleniumDriver):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class BrowserSeleniumDriver:
    """实现本项目页面流程实际用到的 Selenium WebDriver 子集。"""

    _registration_log_prefix: str
    _registration_timeout: int

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._registration_log_prefix = ""
        self._registration_timeout = 90
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self._script_timeout_ms = self._page_load_timeout_ms
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    @property
    def script_timeout(self) -> int:
        return max(1, int(self._script_timeout_ms / 1000))

    def set_script_timeout(self, seconds: int) -> None:
        self._script_timeout_ms = max(1, int(seconds)) * 1000

    def get_cookie(self, name: str) -> dict | None:
        """Return a Selenium-shaped cookie from the active Playwright context."""
        try:
            cookies = self.context.cookies(["https://chatgpt.com", "https://auth.openai.com"])
            for cookie in cookies:
                if cookie.get("name") == name:
                    return dict(cookie)
        except Exception:
            pass
        return None

    def get(self, url: str) -> None:
        max_attempts = max(1, int(getattr(_cfg, "CLOAK_NAVIGATION_RETRIES", 3) or 3))
        base_delay = max(0.0, float(getattr(_cfg, "CLOAK_NAVIGATION_RETRY_DELAY", 1.5) or 1.5))
        for attempt in range(1, max_attempts + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)
                if attempt > 1:
                    logger.info("[Cloak] 页面导航重试成功：attempt=%s/%s", attempt, max_attempts)
                return
            except Exception as exc:
                timeout_error = _is_navigation_timeout_error(exc)
                if timeout_error and _has_usable_navigation_document(self.page, url):
                    logger.warning(
                        "[Cloak] 页面导航超时但认证域 DOM 已可用，交给上层继续等待：url=%s attempt=%s/%s",
                        url,
                        attempt,
                        max_attempts,
                    )
                    return
                retryable = timeout_error or _is_transient_navigation_error(exc)
                if attempt >= max_attempts or not retryable:
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Cloak] 页面导航遇到临时错误，将重试：attempt=%s/%s delay=%.1fs error=%s",
                    attempt,
                    max_attempts,
                    delay,
                    str(exc)[:180],
                )
                try:
                    self.page.goto("about:blank", wait_until="commit", timeout=min(self._page_load_timeout_ms, 10000))
                except Exception:
                    pass
                if delay:
                    time.sleep(delay)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def save_screenshot(self, filename: str) -> bool:
        """Save the active Playwright page using Selenium's screenshot contract."""
        self.page.screenshot(path=str(filename), full_page=False)
        return True

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass

    def find_elements(self, by: Any, selector: str) -> list[BrowserElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [BrowserElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> BrowserElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[BrowserElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 BrowserElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], BrowserElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, BrowserElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return BrowserElement(page, handle=element)
        try:
            value = handle.json_value()
            properties = handle.get_properties()
            if not properties:
                return value
            if isinstance(value, dict):
                result = dict(value)
                for name, child in properties.items():
                    result[name] = BrowserSeleniumDriver._unwrap_js_result(page, child)
                return result
            if isinstance(value, list):
                result = list(value)
                for name, child in properties.items():
                    try:
                        index = int(name)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= index < len(result):
                        result[index] = BrowserSeleniumDriver._unwrap_js_result(page, child)
                return result
            return value
        except Exception as exc:
            if _is_navigation_context_error(exc):
                logger.info("[Cloak] JS 执行期间页面发生跳转，忽略本次临时结果")
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            try:
                if first_el is not None:
                    result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
                else:
                    result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            except Exception as exc:
                if _is_navigation_context_error(exc):
                    logger.info("[Cloak] 异步 JS 执行期间页面发生跳转，忽略本次临时结果")
                    return {"ok": True, "reason": "navigation_after_script"}
                raise
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        try:
            if first_el is not None:
                handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
            else:
                handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        except Exception as exc:
            if _is_navigation_context_error(exc):
                logger.info("[Cloak] JS 执行期间页面发生跳转，忽略本次临时结果")
                return {"ok": True, "reason": "navigation_after_script"}
            if _is_illegal_invocation_error(exc):
                logger.warning("[Cloak] evaluate_handle 出现 Illegal invocation，改用 evaluate 兼容执行")
                if first_el is not None:
                    return first_el._eval(element_wrapper, {"script": script, "args": serial_args})
                return self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            raise
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    from config.proxy import normalize_proxy_url

    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def build_cloak_driver(proxy: str | None = None) -> tuple[BrowserSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    """
    if proxy is None and bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
        from config.proxy import pick_proxy

        # Probe every configured entry so a dead random prefix cannot hide a
        # healthy proxy later in the pool.
        proxy = pick_proxy(
            probe_url="https://chatgpt.com/auth/login",
            probe_timeout=4.0,
        )
        if not proxy:
            raise RuntimeError(
                "Cloak registration requires a proxy that can reach chatgpt.com; "
                "all PROXY_POOL entries failed"
            )
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) else None
    locale_opts = _build_cloak_locale_options(proxy_url)
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if proxy_url:
        opts["proxy"] = proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        proxy_url or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
    )
    context_kwargs = {}
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    if user_data_dir:
        context = launch_persistent_context(user_data_dir, **opts)
        page = context.new_page()
        browser = getattr(context, "browser", None) or context
        # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
    else:
        browser = launch(**opts)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

    driver = BrowserSeleniumDriver(browser=browser, context=context, page=page)
    # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver._registration_timeout = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90)
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(raw={"driver": "cloakbrowser", "proxy": proxy_url, "locale": locale_opts, "options": {k: v for k, v in opts.items() if k != "license_key"}})
