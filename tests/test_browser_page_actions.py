"""Tests cho PageDriver port và page-action primitives dùng chung (P2)."""
import unittest
from unittest.mock import Mock, patch

from core import browser_page_actions as bpa
from core.browser_page_actions import (
    PageState,
    _human_type_text,
    _is_email_verification_page,
    _is_login_password_page,
    _resolve_email_submit_state,
    _safe_get,
    _wait_email_submit_next_state,
    typing_js_fallback_allowed,
)


class _FakeElement:
    def __init__(self, value=""):
        self.value = value
        self.typed = []

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def send_keys(self, *values):
        for v in values:
            self.typed.append(str(v))

    def clear(self):
        self.value = ""


class _PortDriver:
    """Driver giả thỏa đầy đủ mặt sàn PageDriver (không có capability tùy chọn)."""

    def __init__(self, url="https://chatgpt.com/auth/login"):
        self.current_url = url
        self.window_handles = ["w0"]
        self._script_timeout = 30
        self._registration_timeout = 90
        self.get_calls = []
        self.page_load_timeouts = []
        self.script_timeouts = []
        self.screenshot_files = []
        self.element = object()
        self.cleared_cookies = 0
        self.browser = object()
        self.context = object()
        self.page = object()

    @property
    def script_timeout(self):
        return self._script_timeout

    def get(self, url):
        self.get_calls.append(url)

    def execute_script(self, script, *args):
        return {}

    def execute_async_script(self, script, *args):
        return False

    def find_element(self, by, selector):
        return self.element

    def find_elements(self, by, selector):
        return []

    def set_page_load_timeout(self, timeout):
        self.page_load_timeouts.append(timeout)

    def set_script_timeout(self, timeout):
        self._script_timeout = timeout
        self.script_timeouts.append(timeout)

    def save_screenshot(self, filename):
        self.screenshot_files.append(filename)
        return True

    def clear_cookies(self):
        self.cleared_cookies += 1


class PageDriverPortTests(unittest.TestCase):
    def test_port_driver_satisfies_every_declared_member(self):
        driver = _PortDriver()

        self.assertEqual(driver.current_url, "https://chatgpt.com/auth/login")
        self.assertEqual(driver.window_handles, ["w0"])
        self.assertEqual(driver.script_timeout, 30)
        driver.get("https://example.com")
        self.assertEqual(driver.get_calls, ["https://example.com"])
        self.assertEqual(driver.execute_script("return 1;"), {})
        self.assertFalse(driver.execute_async_script("return 1;"))
        self.assertIs(driver.find_element(None, "input"), driver.element)
        self.assertEqual(driver.find_elements(None, "input"), [])
        driver.set_page_load_timeout(45)
        self.assertEqual(driver.page_load_timeouts, [45])
        driver.set_script_timeout(8)
        self.assertEqual(driver.script_timeout, 8)
        self.assertTrue(driver.save_screenshot("shot.png"))
        self.assertEqual(driver.screenshot_files, ["shot.png"])
        driver.clear_cookies()
        self.assertEqual(driver.cleared_cookies, 1)
        # Optional members (Playwright adapters expose them).
        self.assertIsNotNone(driver.browser)
        self.assertIsNotNone(driver.context)
        self.assertIsNotNone(driver.page)

    def test_selenium_webdriver_and_cloak_driver_satisfy_the_port_surface(self):
        from core.cloakbrowser_driver import BrowserSeleniumDriver

        page = Mock()
        page.url = "https://chatgpt.com/auth/login"
        cloak = BrowserSeleniumDriver(browser=object(), context=object(), page=page)

        for member in (
            "current_url", "window_handles", "script_timeout", "get",
            "execute_script", "execute_async_script", "find_elements",
            "set_page_load_timeout", "set_script_timeout", "save_screenshot",
        ):
            self.assertTrue(
                hasattr(cloak, member),
                f"BrowserSeleniumDriver thiếu member {member} của PageDriver",
            )

    def test_safe_get_restores_script_timeout_after_navigation(self):
        driver = _PortDriver()

        _safe_get(driver, "https://chatgpt.com/auth/login", timeout=45)

        # Restore về đúng giá trị cũ (bug-fix cho lane Roxy trước đây bỏ restore).
        self.assertEqual(driver.script_timeout, 30)
        # Trong lúc get, script timeout hạ xuống 8; sau đó restore về 30.
        self.assertEqual(driver.script_timeouts, [8, 30])
        # Page load timeout hạ xuống 45 rồi restore về _registration_timeout (90).
        self.assertEqual(driver.page_load_timeouts, [45, 90])


class PageStatePrecedenceTests(unittest.TestCase):
    def _ambiguous_driver(self):
        """URL /log-in/password nhưng DOM vẫn có OTP inputs — trang ambiguous."""
        return _AmbiguousDriver()

    def test_ambiguous_login_password_url_with_otp_dom_resolves_login_password(self):
        state = _resolve_email_submit_state(self._ambiguous_driver())
        self.assertEqual(state, "login_password")

    def test_wait_email_submit_next_state_uses_login_password_first_for_ambiguous_page(self):
        with patch.object(bpa.time, "monotonic", side_effect=[0.0, 1.0, 1.0, 1.0]), \
                patch.object(bpa.time, "sleep"), \
                patch.object(bpa, "_browser_challenge_state", return_value={"is_challenge": False}):
            result = _wait_email_submit_next_state(self._ambiguous_driver(), "user@example.com", timeout=5)
        self.assertEqual(result, "login_password")

    def test_login_password_beats_password_and_otp(self):
        # _is_login_password_page trả True ngay cả khi signup-password URL match sau đó.
        driver = Mock()
        driver.current_url = "https://auth.openai.com/log-in/password"
        state = _resolve_email_submit_state(driver)
        self.assertEqual(state, "login_password")

    def test_signup_password_url_resolves_password(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/create-account/password"
        with patch.object(bpa, "_is_login_password_page", return_value=False), \
                patch.object(bpa, "_is_signup_password_page", return_value=True):
            self.assertEqual(_resolve_email_submit_state(driver), "password")

    def test_is_email_verification_page_excludes_log_in_password_url(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/log-in/password?email=x"
        self.assertFalse(_is_email_verification_page(driver))
        self.assertTrue(_is_login_password_page(driver))

    def test_page_state_coerce_maps_legacy_vocabulary(self):
        self.assertEqual(PageState.coerce("email_verification"), PageState.OTP)
        self.assertEqual(PageState.coerce("email_otp"), PageState.OTP)
        self.assertEqual(PageState.coerce("signup"), PageState.PASSWORD)
        self.assertEqual(PageState.coerce("deactivated:account_deactivated"), PageState.DEACTIVATED)
        self.assertEqual(PageState.coerce("deactivated:account_deleted"), PageState.DEACTIVATED)
        self.assertEqual(PageState.coerce("profile"), PageState.PROFILE)
        self.assertEqual(PageState.coerce("chatgpt"), PageState.CHATGPT)
        self.assertEqual(PageState.coerce("login_password"), PageState.LOGIN_PASSWORD)
        self.assertEqual(PageState.coerce("anything-else"), PageState.UNKNOWN)


class _AmbiguousDriver(_PortDriver):
    """Trang /log-in/password mà DOM vẫn còn input one-time-code (case ambiguous)."""

    def __init__(self):
        super().__init__(url="https://auth.openai.com/log-in/password")

    def execute_script(self, script, *args):
        if "one-time-code" in str(script) or "inputmode" in str(script):
            # _email_otp_page_state: có OTP input visible.
            return {
                "url": self.current_url,
                "inputs": [{
                    "type": "text", "name": "code", "id": "code",
                    "autocomplete": "one-time-code", "inputmode": "numeric",
                    "ariaLabel": "", "ariaInvalid": "", "value": "",
                }],
                "buttons": [], "errors": [], "text": "Enter code",
            }
        if "password" in str(script):
            # _password_page_state: URL vẫn là log-in/password.
            return {"url": self.current_url, "inputs": [], "forms": [], "buttons": []}
        return {}


class TypingPolicyTests(unittest.TestCase):
    def test_local_driver_allows_js_fallback_by_default(self):
        self.assertTrue(typing_js_fallback_allowed(_PortDriver()))

    def test_cloud_driver_disabling_js_fallback_is_honored(self):
        driver = _PortDriver()
        driver._typing_js_fallback_allowed = False
        self.assertFalse(typing_js_fallback_allowed(driver))

    def test_human_type_text_raises_without_js_fallback_when_capability_forbids(self):
        driver = _PortDriver()
        driver._typing_js_fallback_allowed = False
        element = _FakeElement()
        # send_keys ném lỗi trong _human_type_text → policy phải chặn JS fallback.
        element.send_keys = Mock(side_effect=RuntimeError("detached element"))

        with patch.object(bpa, "_human_click"), patch.object(bpa, "_human_scroll_to"), \
                patch.object(bpa, "human_delay"), \
                self.assertRaisesRegex(RuntimeError, "已禁止瞬时 fill 兜底"):
            _human_type_text(driver, element, "secret-value", clear=False)

    def test_human_type_text_uses_js_fallback_when_capability_allows(self):
        driver = _PortDriver()
        element = _FakeElement()
        element.send_keys = Mock(side_effect=RuntimeError("detached element"))

        with patch.object(bpa, "_human_click"), patch.object(bpa, "_human_scroll_to"), \
                patch.object(bpa, "human_delay"), \
                patch.object(bpa, "_set_element_value", return_value=True) as setter:
            _human_type_text(driver, element, "secret-value", clear=False)

        setter.assert_called_once()


class SharedWaitStateTests(unittest.TestCase):
    def test_wait_email_submit_next_state_reads_only_url_when_bounded_adapter_present(self):
        """Adapter có read_auth_flow_state → không dùng synchronous JS probes."""
        driver = _BoundedProbeDriver()
        result = _wait_email_submit_next_state(driver, "user@example.com", timeout=2)
        self.assertEqual(result, "login_password")
        self.assertEqual(driver.probe_timeouts_ms, [400])


class _BoundedProbeDriver(_PortDriver):
    def __init__(self):
        super().__init__()
        self.probe_timeouts_ms = []

    def read_auth_flow_state(self, *, timeout_ms=400):
        self.probe_timeouts_ms.append(timeout_ms)
        return {"state": "login_password", "body_text": ""}

    def execute_script(self, script, *args):
        raise AssertionError("bounded adapter path must not run synchronous JS probes")


if __name__ == "__main__":
    unittest.main()
