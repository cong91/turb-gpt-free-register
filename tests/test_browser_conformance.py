"""Executable conformance checks for the shared browser architecture (P6)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import browser_failure_policy as failure_policy
from core import browser_page_actions as page_actions
from core import browser_registry
from core.cloakbrowser_driver import BrowserSeleniumDriver
from core.openai_auth import AccountUnusableError
from core.registration_flow import run_registration_page_flow
from core.registration_profile_utils import profile_submission_failure_message


class _FakeLocator:
    def __init__(self, *, text="", count=0, handle=None):
        self.text = text
        self._count = count
        self.handle = handle or Mock()

    def inner_text(self, **kwargs):
        return self.text

    def count(self):
        return self._count

    def nth(self, index):
        return self

    def evaluate_handle(self, expression, payload):
        return self.handle

    def evaluate(self, expression, payload=None):
        return not (isinstance(payload, dict) and payload.get("script") == "return false;")


class _FakeCloakPage:
    def __init__(self):
        self.url = "https://chatgpt.com/auth/login"
        self.keyboard = Mock()
        self.body = _FakeLocator(text="login")
        self.input = _FakeLocator(count=1)
        self.navigation_timeout = None
        self.default_timeout = None

    def locator(self, selector):
        return self.body if selector == "body" else self.input

    def goto(self, url, **kwargs):
        self.url = url

    def evaluate_handle(self, expression, payload):
        return Mock(as_element=Mock(return_value=None), json_value=Mock(return_value=True), get_properties=Mock(return_value={}), dispose=Mock())

    def evaluate(self, expression, payload=None):
        if "__cloak_done" in expression and isinstance(payload, dict):
            result = payload["script"] == "return true;"
            return result
        if "document.readyState" in expression:
            return {"readyState": "complete", "hasBody": True}
        return True

    def wait_for_selector(self, selector, **kwargs):
        return Mock(dispose=Mock())

    def screenshot(self, **kwargs):
        self.screenshot_args = kwargs

    def set_default_navigation_timeout(self, timeout):
        self.navigation_timeout = timeout

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def go_back(self, **kwargs):
        return None

    def reload(self, **kwargs):
        return None

    def bring_to_front(self):
        return None


class _FakeCloakContext:
    def __init__(self, page):
        self.pages = [page]
        self.clear_cookies = Mock()
        self.close = Mock()
        self.cookies = Mock(return_value=[])


class _ConformanceDriver:
    def __init__(self, url="https://chatgpt.com/auth/login"):
        self.current_url = url
        self.window_handles = ["main"]
        self.browser = object()
        self.context = object()
        self.page = object()
        self.screenshot_files = []
        self._script_timeout = 30
        self._registration_timeout = 30
        self.calls: list[tuple[str, object]] = []

    @property
    def script_timeout(self):
        return self._script_timeout

    def get(self, url):
        self.calls.append(("get", url))

    def execute_script(self, script, *args):
        self.calls.append(("execute_script", script))
        return {}

    def execute_async_script(self, script, *args):
        self.calls.append(("execute_async_script", script))
        return False

    def find_elements(self, by, selector):
        self.calls.append(("find_elements", (by, selector)))
        return []

    def find_element(self, by, selector):
        self.calls.append(("find_element", (by, selector)))
        return object()

    def set_page_load_timeout(self, timeout):
        self.calls.append(("set_page_load_timeout", timeout))

    def set_script_timeout(self, timeout):
        self.calls.append(("set_script_timeout", timeout))
        self._script_timeout = timeout

    def save_screenshot(self, filename):
        self.calls.append(("save_screenshot", filename))
        return True

    def clear_cookies(self):
        self.calls.append(("clear_cookies", None))

    def read_auth_flow_state(self, *, timeout_ms=400):
        self.calls.append(("read_auth_flow_state", timeout_ms))
        return {"state": "other", "body_text": ""}


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _SessionDriver:
    current_url = "https://chatgpt.com/"

    def refresh(self):
        return None


class _RawSeleniumDriver:
    current_url = "https://chatgpt.com/auth/login"
    def __init__(self):
        self.window_handles = ["raw"]
        self.script_timeouts = []
        self.cleared = False

    def get(self, url):
        self.current_url = url

    def execute_script(self, script, *args):
        return {}

    def execute_async_script(self, script, *args):
        return False

    def find_element(self, by, selector):
        return object()

    def find_elements(self, by, selector):
        return []

    def set_page_load_timeout(self, timeout):
        self.page_timeout = timeout

    def set_script_timeout(self, timeout):
        self.script_timeouts.append(timeout)

    def save_screenshot(self, filename):
        return True

    def delete_all_cookies(self):
        self.cleared = True


class BrowserConformancePortTests(unittest.TestCase):
    def test_port_protocol_lists_required_and_optional_capabilities(self):
        required = set(page_actions.PageDriver.__annotations__) | {
            "current_url", "window_handles", "script_timeout", "get", "execute_script",
            "execute_async_script", "find_element", "find_elements", "set_page_load_timeout",
            "set_script_timeout", "save_screenshot", "clear_cookies",
        }
        self.assertTrue({"browser", "context", "page"}.issubset(page_actions.PageDriver.__annotations__))
        for name in required - page_actions.PageDriver.__annotations__.keys():
            self.assertTrue(hasattr(page_actions.PageDriver, name), name)
        self.assertTrue(hasattr(page_actions.AuthFlowStateReader, "read_auth_flow_state"))

    def test_raw_selenium_adapter_implements_timeout_and_cookie_port(self):
        from core.browser_selenium_adapter import SeleniumPageDriver

        raw = _RawSeleniumDriver()
        driver = SeleniumPageDriver(raw)
        self.assertEqual(driver.script_timeout, 30)
        driver.set_script_timeout(9)
        self.assertEqual(driver.script_timeout, 9)
        driver.clear_cookies()
        self.assertTrue(raw.cleared)
        self.assertEqual(raw.script_timeouts, [9])
        self.assertTrue(driver._typing_js_fallback_allowed)

    def test_selenium_compatible_driver_calls_every_required_port_member(self):
        driver = _ConformanceDriver()
        self.assertEqual(driver.current_url, "https://chatgpt.com/auth/login")
        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.script_timeout, 30)
        driver.get("https://example.test")
        driver.execute_script("return true;")
        driver.execute_async_script("return true;")
        driver.find_element("css selector", "input")
        driver.find_elements("css selector", "input")
        driver.set_page_load_timeout(45)
        driver.set_script_timeout(8)
        self.assertTrue(driver.save_screenshot("batch/shot.png"))
        driver.clear_cookies()
        self.assertEqual(driver.read_auth_flow_state(timeout_ms=400)["state"], "other")
        self.assertIsNotNone(driver.browser)
        self.assertIsNotNone(driver.context)
        self.assertIsNotNone(driver.page)
        self.assertEqual(len(driver.calls), 10)

    def test_cloak_adapter_executes_every_required_port_member(self):
        page = _FakeCloakPage()
        context = _FakeCloakContext(page)
        browser = Mock(contexts=[context])
        driver = BrowserSeleniumDriver(browser=browser, context=context, page=page)

        self.assertEqual(driver.current_url, page.url)
        self.assertEqual(driver.window_handles, ["0"])
        self.assertGreater(driver.script_timeout, 0)
        driver.set_page_load_timeout(12)
        self.assertEqual(page.navigation_timeout, 12000)
        self.assertEqual(page.default_timeout, 12000)
        driver.set_script_timeout(7)
        self.assertEqual(driver.script_timeout, 7)
        driver.get("https://example.test/path")
        self.assertEqual(page.url, "https://example.test/path")
        self.assertTrue(driver.execute_script("return true;"))
        self.assertFalse(driver.execute_async_script("return false;"))
        self.assertEqual(len(driver.find_elements("css selector", "input")), 1)
        self.assertIsInstance(driver.find_element("css selector", "input"), object)
        self.assertEqual(driver.read_auth_flow_state(timeout_ms=400)["state"], "password")
        self.assertTrue(driver.save_screenshot("batch/shot.png"))
        self.assertEqual(page.screenshot_args, {"path": "batch/shot.png", "full_page": False})
        driver.clear_cookies()
        context.clear_cookies.assert_called_once_with()
        self.assertIs(driver.browser, browser)
        self.assertIs(driver.context, context)
        self.assertIs(driver.page, page)


class BrowserConformanceContractTests(unittest.TestCase):
    def test_login_password_contract_is_emitted_by_shared_password_step(self):
        driver = SimpleNamespace(
            current_url="https://auth.openai.com/log-in/password",
            execute_script=lambda script, *args: {},
        )
        with (
            self.assertRaisesRegex(
                RuntimeError,
                r"^邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url=https://auth\.openai\.com/log-in/password$",
            ),
            patch("core.registration_flow._password_page_state", return_value={}),
            patch("core.registration_flow._is_signup_password_page", return_value=False),
            patch("core.registration_flow._is_login_password_page", return_value=True),
        ):
            from core.registration_flow import _fill_password_page_if_present

            _fill_password_page_if_present(driver, "user@example.com", timeout=1)

    def test_browser_use_quick_auth_uses_shared_precedence_for_ambiguous_page(self):
        from core.browser_use_registration import _quick_auth_state

        class Page:
            def evaluate(self, _script):
                return {
                    "state": "login_password",
                    "url": "https://auth.openai.com/log-in/password",
                    "textPreview": "Enter code",
                }

        self.assertEqual(_quick_auth_state(Page())["state"], "login_password")

        self.assertEqual(
            profile_submission_failure_message("email is not supported"),
            "about-you 提交失败：email is not supported",
        )

    def test_warning_banner_http_200_contract_is_emitted_by_session_wait(self):
        from core import registration_flow

        clock = _FakeClock()
        banner = {"WARNING_BANNER": "unusual activity", "_http_status": 200}
        with patch.object(registration_flow, "_read_chatgpt_session_once", return_value=banner), \
            patch.object(registration_flow, "_check_manual_stop"), \
            patch.object(registration_flow, "time", clock), \
            self.assertRaises(RuntimeError) as ctx:
            registration_flow._fetch_chatgpt_session(_SessionDriver(), timeout=120)
        message = str(ctx.exception)
        self.assertIn("WARNING_BANNER", message)
        self.assertIn("'_http_status': 200", message)
        self.assertIn("等待 /api/auth/session accessToken 超时", message)

    def test_shared_flow_uses_common_failure_release_path(self):
        driver = Mock()
        with patch("core.registration_flow._safe_get"), \
            patch("core.registration_flow._maybe_accept"), \
            patch("core.registration_flow._check_manual_stop"), \
            patch("core.registration_flow._submit_email_and_wait_next", side_effect=RuntimeError("邮箱提交后进入登录密码页")), \
            patch("core.registration_flow.release_registration_email_on_failure") as release, \
            patch("core.registration_flow.registration_failure_result", return_value={"success": False}) as result:
            output = run_registration_page_flow(
                driver,
                "user@example.com",
                "Test",
                "1990-01-01",
                registration_driver="test",
            )
        self.assertEqual(output, {"success": False})
        release.assert_called_once()
        result.assert_called_once()


class BrowserConformanceTypingTests(unittest.TestCase):
    def test_cloud_capability_disallows_js_fallback_through_shared_step(self):
        driver = _ConformanceDriver()
        driver._typing_js_fallback_allowed = False
        element = Mock()
        element.send_keys.side_effect = RuntimeError("detached")
        with patch.object(page_actions, "_human_scroll_to"), \
            patch.object(page_actions, "_human_click"), \
            patch.object(page_actions, "human_delay"), \
            self.assertRaisesRegex(RuntimeError, "已禁止瞬时 fill 兜底"):
            page_actions._human_type_text(driver, element, "secret", clear=False)

    def test_local_capability_allows_js_fallback_through_shared_step(self):
        driver = _ConformanceDriver()
        element = Mock()
        element.send_keys.side_effect = RuntimeError("detached")
        with patch.object(page_actions, "_human_scroll_to"), \
            patch.object(page_actions, "_human_click"), \
            patch.object(page_actions, "human_delay"), \
            patch.object(page_actions, "_set_element_value", return_value=True) as setter:
            page_actions._human_type_text(driver, element, "secret", clear=False)
        setter.assert_called_once_with(driver, element, "secret")


class BrowserConformanceStateTests(unittest.TestCase):
    def test_cloak_lane_precedence_declared_behavior_change(self):
        """Declared behavior change (cloak lane): shared precedence checks
        login_password FIRST. A /log-in/password URL that also renders OTP DOM
        must classify as login_password (registered/unusable email) even on the
        cloak lane, which previously checked password first via the removed
        browser_registration fork."""
        driver = Mock(current_url="https://auth.openai.com/log-in/password")
        with patch.object(page_actions, "_is_login_password_page", return_value=True), \
            patch.object(page_actions, "_is_signup_password_page", return_value=True), \
            patch.object(page_actions, "_is_email_verification_page", return_value=True), \
            patch.object(page_actions, "_has_access_token", return_value=True):
            self.assertEqual(page_actions._resolve_email_submit_state(driver), "login_password")

    def test_page_state_alias_union_and_precedence(self):
        self.assertEqual(page_actions.PageState.coerce("email_verification"), page_actions.PageState.OTP)
        self.assertEqual(page_actions.PageState.coerce("signup"), page_actions.PageState.PASSWORD)
        self.assertEqual(page_actions.PageState.coerce("deactivated:account_deleted"), page_actions.PageState.DEACTIVATED)
        driver = Mock(current_url="https://auth.openai.com/log-in/password")
        with patch.object(page_actions, "_is_login_password_page", return_value=True), \
            patch.object(page_actions, "_is_signup_password_page", return_value=True), \
            patch.object(page_actions, "_is_email_verification_page", return_value=True), \
            patch.object(page_actions, "_has_access_token", return_value=True):
            self.assertEqual(page_actions._resolve_email_submit_state(driver), "login_password")


class BrowserConformanceFailurePolicyTests(unittest.TestCase):
    def test_failure_policy_covers_live_and_roxy_paths(self):
        dead = AccountUnusableError("account_deactivated", error_code="account_deactivated")
        self.assertEqual(failure_policy.release_status_for_failure(dead), "disabled")
        self.assertEqual(failure_policy.release_status_for_failure(RuntimeError("proxy timeout")), "available")
        self.assertEqual(
            failure_policy.release_status_for_failure(RuntimeError("邮箱提交后进入登录密码页")),
            "failed",
        )
        with self.assertRaises(AccountUnusableError):
            failure_policy.raise_if_account_unusable(_DeadDriver())


class _DeadDriver:
    def execute_script(self, script, *args):
        return "Your account has been deactivated. error_code: account_deactivated"


class BrowserConformanceRegistryTests(unittest.TestCase):
    def test_registry_guard_derives_driver_capabilities_from_one_alias_map(self):
        canonical_drivers = set(browser_registry.ALIASES.values())
        self.assertEqual(browser_registry.LIVE_BROWSER_DRIVERS, canonical_drivers)
        self.assertEqual(
            browser_registry.SUPPORTED_REGISTRATION_DRIVERS,
            canonical_drivers | {"protocol"},
        )
        for alias, canonical in browser_registry.ALIASES.items():
            self.assertEqual(browser_registry.normalize_driver(alias), canonical)
            self.assertTrue(browser_registry.is_live_browser_driver(alias))
            self.assertTrue(browser_registry.is_supported_driver(alias))
        self.assertEqual(browser_registry.DEFAULT_DRIVER, "roxy")
        self.assertEqual(browser_registry.resolve_registration_driver(SimpleNamespace()), "roxy")
        self.assertFalse(browser_registry.is_live_browser_driver("protocol"))

    def test_registry_live_browser_predicate_is_canonical(self):
        self.assertTrue(browser_registry.is_live_browser_driver("browser-use"))
        self.assertFalse(browser_registry.is_live_browser_driver("protocol"))


class CloudSessionFetchContractTests(unittest.TestCase):
    """Review round-1 regression: the cloud lane's session_fetch closure must call
    the module-level Playwright-aware `_fetch_chatgpt_session(page, context=...)`.
    A function-local import of the registration_flow variant (which has no
    `context` kwarg) used to shadow it and crash after the account checkpoint."""

    def test_cloud_session_fetch_uses_playwright_aware_reader_with_context(self):
        import core.browser_use_registration as browser_use

        captured = {}

        def fake_flow(driver, email, name, birthday, **kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "email": email,
                "account_id": 1,
                "access_token": "tok",
                "session_info": {},
                "openai_password": None,
                "create_acknowledged": True,
            }

        driver = SimpleNamespace(page=object(), context=object())
        profile = SimpleNamespace(driver=driver, close=Mock(), cleanup=Mock())
        opener = Mock(return_value=profile)

        with patch("core.browser_registry.resolve_profile_opener", return_value=opener), \
            patch("core.registration_flow.run_registration_page_flow", side_effect=fake_flow), \
            patch("core.browser_use_registration._fetch_chatgpt_session", return_value={"accessToken": "tok"}) as fetch, \
            patch("config.twofa.ENABLE_2FA", False), \
            patch("config.codex.ENABLE_CODEX_AUTO", False), \
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False), \
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False):
            result = browser_use.run_browser_use_registration(
                "user@example.com",
                "Test User",
                "1990-01-01",
            )
            session = captured["session_fetch"](driver, timeout=120, auto_jump_wait=15)
            fetch.assert_called_once_with(
                driver.page,
                context=driver.context,
                timeout=120,
            )

        self.assertTrue(result["success"])
        self.assertEqual(session, {"accessToken": "tok"})
        profile.close.assert_called_once()
        profile.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
