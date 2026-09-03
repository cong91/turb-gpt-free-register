import unittest
from pathlib import Path
from unittest.mock import patch

from core.cloakbrowser_driver import (
    BrowserElement,
    BrowserSeleniumDriver,
    CloakOpenResult,
    build_cloak_driver,
)


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.typed = []
        self.pressed = []

    def type(self, text, delay=None):
        self.typed.append((text, delay))
        self.page.insert_text(text)

    def press(self, key):
        self.pressed.append(key)


class _FakePage:
    def __init__(self):
        self.value = ""
        self.caret = 0
        self.keyboard = _FakeKeyboard(self)

    def insert_text(self, text):
        self.value = self.value[:self.caret] + text + self.value[self.caret:]
        self.caret += len(text)


class _FakeLocator:
    def __init__(self, page):
        self.page = page
        self.fills = []
        self.clicks = 0
        self.focuses = 0

    def fill(self, text, timeout=None):
        self.fills.append(text)
        self.page.value = text
        self.page.caret = len(text)

    def click(self, timeout=None):
        self.clicks += 1
        self.page.caret = len(self.page.value) // 2

    def focus(self, timeout=None):
        self.focuses += 1


class BrowserElementTests(unittest.TestCase):
    def test_send_keys_keeps_email_order_without_reclicking_the_input(self):
        page = _FakePage()
        locator = _FakeLocator(page)
        element = BrowserElement(page, locator=locator)

        email = "bateslarry831501+7a52a@gmail.com"
        for chunk in ("bateslarry831501", "+7", "a52a", "@gmail", ".com"):
            element.send_keys(chunk)

        self.assertEqual(email, page.value)
        self.assertEqual(0, locator.clicks)
        self.assertEqual(5, locator.focuses)
        self.assertEqual([], locator.fills)
        self.assertTrue(all(delay == 25 for _, delay in page.keyboard.typed))

    def test_send_keys_translates_selenium_control_and_backspace_keys(self):
        page = _FakePage()
        locator = _FakeLocator(page)
        element = BrowserElement(page, locator=locator)

        element.send_keys("\ue009", "a")
        element.send_keys("\ue003")

        self.assertEqual(["Control+a", "Backspace"], page.keyboard.pressed)

    def test_text_exposes_locator_inner_text_for_resend_logic(self):
        page = _FakePage()
        locator = _FakeLocator(page)
        locator.inner_text = lambda timeout=None: "Send a new code"
        element = BrowserElement(page, locator=locator)

        self.assertEqual("Send a new code", element.text)

    def test_browser_helpers_use_native_locator_for_cloak_elements(self):
        source = (Path(__file__).parents[1] / "core" / "browser_registration.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('native = getattr(el, "locator", None) or getattr(el, "handle", None)', source)
        self.assertIn('if getattr(el, "locator", None) is not None or getattr(el, "handle", None) is not None:', source)
        self.assertIn('if driver.__class__.__name__ == "BrowserSeleniumDriver":', source)


class _NavigatingPage:
    def evaluate_handle(self, _expression, _arg):
        raise RuntimeError(
            "Page.evaluate_handle: Execution context was destroyed, "
            "most likely because of a navigation"
        )


class _IllegalInvocationPage:
    def evaluate_handle(self, _expression, _arg):
        raise RuntimeError("Page.evaluate_handle: TypeError: Illegal invocation")

    def evaluate(self, _expression, _arg):
        return {"ok": True, "fallback": True}


class _CookieContext:
    def cookies(self, _urls):
        return [{"name": "oai-did", "value": "device-123"}]


class _SessionResponse:
    status = 200

    def json(self):
        return {"accessToken": "request-token"}


class _SessionRequest:
    def __init__(self):
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        return _SessionResponse()


class _SessionContext(_CookieContext):
    def __init__(self):
        self.request = _SessionRequest()


class _ScriptTimeoutPage:
    def set_default_navigation_timeout(self, _timeout):
        pass

    def set_default_timeout(self, _timeout):
        pass


class CloakAdapterContractTests(unittest.TestCase):
    def test_each_cloak_open_result_has_unique_session_identity(self):
        first = CloakOpenResult()
        second = CloakOpenResult()

        self.assertNotEqual(first.profile_id, second.profile_id)
        self.assertTrue(first.profile_id.startswith("cloakbrowser:"))
        self.assertTrue(second.profile_id.startswith("cloakbrowser:"))

    def test_cookie_and_script_timeout_contracts_are_available(self):
        driver = BrowserSeleniumDriver(
            browser=None,
            context=_CookieContext(),
            page=_ScriptTimeoutPage(),
        )

        driver.set_script_timeout(7)

        self.assertEqual("device-123", driver.get_cookie("oai-did")["value"])
        self.assertEqual(7, driver.script_timeout)

    def test_chatgpt_session_uses_context_request_with_script_timeout(self):
        context = _SessionContext()
        driver = BrowserSeleniumDriver(
            browser=None,
            context=context,
            page=_ScriptTimeoutPage(),
        )
        driver.set_script_timeout(7)

        self.assertEqual(driver.get_chatgpt_auth_session(), {"accessToken": "request-token"})
        self.assertEqual(
            context.request.calls,
            [("https://chatgpt.com/api/auth/session", 7000)],
        )


class BrowserSeleniumDriverTests(unittest.TestCase):
    @patch("core.browser_registration._find_any")
    def test_browser_registration_returns_native_email_element_for_cloak(self, find_any):
        from core.browser_registration import _wait_for_email_input

        driver = type("BrowserSeleniumDriver", (), {})()
        expected_element = object()
        find_any.return_value = expected_element

        result = _wait_for_email_input(driver, timeout=1)

        self.assertIs(result, expected_element)

    @patch("config.proxy.pick_proxy", return_value="")
    @patch("core.cloakbrowser_driver._cfg.CLOAK_USE_PROXY", True)
    def test_build_fails_before_browser_when_no_proxy_reaches_chatgpt(self, pick_proxy):
        with self.assertRaisesRegex(RuntimeError, "all PROXY_POOL entries failed"):
            build_cloak_driver()
        pick_proxy.assert_called_once_with(
            probe_url="https://chatgpt.com/auth/login",
            probe_timeout=4.0,
        )

    def test_get_retries_transient_empty_response(self):
        page = _RetryingNavigationPage()
        driver = BrowserSeleniumDriver(browser=None, context=None, page=page)
        driver.set_page_load_timeout(5)

        driver.get("https://auth.openai.com/oauth/authorize?state=test")

        self.assertEqual(
            page.urls,
            [
                "https://auth.openai.com/oauth/authorize?state=test",
                "about:blank",
                "https://auth.openai.com/oauth/authorize?state=test",
            ],
        )

    def test_get_retries_aborted_navigation(self):
        page = _AbortedNavigationPage()
        driver = BrowserSeleniumDriver(browser=None, context=None, page=page)
        driver.set_page_load_timeout(5)

        driver.get("https://chatgpt.com/auth/login")

        self.assertEqual(
            page.urls,
            [
                "https://chatgpt.com/auth/login",
                "about:blank",
                "https://chatgpt.com/auth/login",
            ],
        )

    @patch("core.cloakbrowser_driver._cfg.CLOAK_NAVIGATION_RETRY_DELAY", 0)
    @patch("core.cloakbrowser_driver._cfg.CLOAK_NAVIGATION_RETRIES", 2)
    def test_get_retries_playwright_navigation_timeout(self):
        page = _TimeoutNavigationPage()
        driver = BrowserSeleniumDriver(browser=None, context=None, page=page)

        driver.get("https://chatgpt.com/auth/login")

        self.assertEqual(
            page.urls,
            [
                "https://chatgpt.com/auth/login",
                "about:blank",
                "https://chatgpt.com/auth/login",
            ],
        )

    @patch("core.cloakbrowser_driver._cfg.CLOAK_NAVIGATION_RETRIES", 2)
    def test_get_accepts_timeout_when_target_dom_is_already_usable(self):
        page = _UsableAfterTimeoutPage()
        driver = BrowserSeleniumDriver(browser=None, context=None, page=page)

        driver.get("https://chatgpt.com/auth/login")

        self.assertEqual(page.urls, ["https://chatgpt.com/auth/login"])

    def test_execute_script_treats_navigation_context_loss_as_transient(self):
        driver = BrowserSeleniumDriver(browser=None, context=None, page=_NavigatingPage())

        result = driver.execute_script("return document.readyState;")

        self.assertEqual(result, {"ok": True, "reason": "navigation_after_script"})

    def test_execute_script_falls_back_to_evaluate_for_illegal_invocation(self):
        driver = BrowserSeleniumDriver(browser=None, context=None, page=_IllegalInvocationPage())

        result = driver.execute_script("return {ok:true};")

        self.assertEqual(result, {"ok": True, "fallback": True})

    def test_execute_script_unwraps_nested_dom_elements_in_result_object(self):
        element_handle = object()
        handle = _NestedResultHandle(
            {"ok": True, "target": {}},
            {"ok": _NestedResultHandle(True), "target": _NestedResultHandle(element_handle)},
        )

        result = BrowserSeleniumDriver._unwrap_js_result(_FakePage(), handle)

        self.assertTrue(result["ok"])
        self.assertIsInstance(result["target"], BrowserElement)
        self.assertIs(result["target"].handle, element_handle)

    def test_shared_registration_scripts_guard_optional_scroll_api(self):
        source = (Path(__file__).parents[1] / "core" / "browser_registration.py").read_text(
            encoding="utf-8"
        )

        unsafe_calls = [
            line.strip()
            for line in source.splitlines()
            if ".scrollIntoView(" in line
            and "driver.execute_script" not in line
            and "typeof" not in line
        ]

        self.assertEqual([], unsafe_calls)
        unsafe_focus_calls = [
            line.strip()
            for line in source.splitlines()
            if ".focus();" in line
            and "driver.execute_script" not in line
            and "typeof" not in line
        ]

        self.assertEqual([], unsafe_focus_calls)

    def test_cloak_registration_does_not_import_roxy_driver_module(self):
        source = (Path(__file__).parents[1] / "core" / "cloakbrowser_registration.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("from core.roxy_registration import", source)
        self.assertIn("from core.browser_registration import", source)


class _RetryingNavigationPage:
    def __init__(self):
        self.urls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.urls.append(url)
        if url != "about:blank" and len(self.urls) == 1:
            raise RuntimeError("Page.goto: net::ERR_EMPTY_RESPONSE")


class _TimeoutNavigationPage:
    def __init__(self):
        self.urls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.urls.append(url)
        if url != "about:blank" and len(self.urls) == 1:
            raise TimeoutError(
                'Page.goto: Timeout 45000ms exceeded. navigating to "https://chatgpt.com/auth/login"'
            )


class _AbortedNavigationPage:
    def __init__(self):
        self.urls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.urls.append(url)
        if url != "about:blank" and len(self.urls) == 1:
            raise RuntimeError("Page.goto: net::ERR_ABORTED")


class _UsableAfterTimeoutPage:
    def __init__(self):
        self.urls = []
        self.url = "about:blank"

    def goto(self, url, wait_until=None, timeout=None):
        self.urls.append(url)
        self.url = url
        if url != "about:blank" and len(self.urls) == 1:
            raise TimeoutError(
                'Page.goto: Timeout 45000ms exceeded. navigating to "https://chatgpt.com/auth/login"'
            )

    def evaluate(self, _expression):
        return {"readyState": "interactive", "hasBody": True}


class _NestedResultHandle:
    def __init__(self, value, properties=None):
        self.value = value
        self.properties = properties or {}
        self.disposed = False

    def as_element(self):
        return self.value if not isinstance(self.value, (dict, list, bool, str, int, float, type(None))) else None

    def json_value(self):
        return self.value

    def get_properties(self):
        return self.properties

    def dispose(self):
        self.disposed = True


if __name__ == "__main__":
    unittest.main()
