"""Regression tests for browser challenge detection and waiting."""

import unittest
from unittest.mock import Mock, patch

from core.browser_challenge import (
    browser_challenge_state as _browser_challenge_state,
    complete_owned_lab_challenge,
    describe_browser_challenge,
    inspect_turnstile_response,
    run_owned_lab_challenge,
)
from core.browser_registration import (
    _account_unusable_page_code,
    _has_access_token,
    _is_email_verification_page,
    _is_signup_password_page,
    _submit_email_and_wait_next,
    _wait_for_browser_challenge,
)


class _Driver:
    def __init__(self, states):
        self.states = iter(states)
        self.current_url = "https://chatgpt.com/auth/login"

    def execute_script(self, _script):
        return next(self.states)


class _StringStateDriver:
    current_url = "https://chatgpt.com/auth/login"

    def execute_script(self, _script):
        return "navigation_after_script"

    def execute_async_script(self, _script):
        return {"ok": True, "reason": "navigation_after_script"}


class _ScriptCaptureDriver:
    current_url = "http://127.0.0.1:8000/challenge"

    def __init__(self, state):
        self.state = state
        self.script = ""

    def execute_script(self, script):
        self.script = script
        return self.state


class _LabDriver:
    current_url = "http://127.0.0.1:8000/challenge"

    def execute_script(self, script):
        self.script = script
        return {"clicked": True, "checked": True}


class _OwnedLabFlowDriver:
    current_url = "http://127.0.0.1:8000/challenge"

    def __init__(self):
        self.states = iter(
            [
                {
                    "url": self.current_url,
                    "title": "Mock challenge",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": False,
                    "has_challenge_widget": True,
                    "has_challenge_checkbox": True,
                    "turnstile_response_source": "none",
                },
                {
                    "url": self.current_url,
                    "title": "Mock challenge complete",
                    "body_text": "",
                    "has_email_input": True,
                    "has_challenge_widget": True,
                    "has_challenge_checkbox": False,
                    "turnstile_response_source": "input[name=cf-turnstile-response]",
                },
            ]
        )
        self.action_calls = 0

    def execute_script(self, script):
        if 'data-classroom-challenge="true"' in script:
            self.action_calls += 1
            return {"clicked": True, "checked": True}
        return next(self.states)


class BrowserRegistrationChallengeTests(unittest.TestCase):
    def test_shared_selenium_page_reader_detects_deactivated_html(self):
        html = (
            "<h1>Authentication Error</h1>"
            "<div>You do not have an account because it has been deleted or deactivated.</div>"
            "<span>error_code: account_deactivated</span>"
        )
        driver = _Driver([{"body": html}])

        self.assertEqual(_account_unusable_page_code(driver), "account_deactivated")

    def test_state_reader_does_not_click_the_checkbox(self):
        driver_url = "http://127.0.0.1:8000/challenge"
        driver = _ScriptCaptureDriver(
            {
                "url": driver_url,
                "title": "Mock challenge",
                "body_text": "Select the checkbox to continue.",
                "has_email_input": False,
                "has_challenge_widget": True,
                "has_challenge_checkbox": True,
                "turnstile_response_source": "none",
            }
        )

        state = _browser_challenge_state(driver)

        self.assertEqual(state["url"], driver_url)
        self.assertNotIn("checkbox.click()", driver.script)

    def test_owned_lab_action_clicks_only_the_explicit_demo_checkbox(self):
        driver = _LabDriver()

        result = complete_owned_lab_challenge(driver)

        self.assertEqual(result, {"clicked": True, "checked": True})
        self.assertIn('input[data-classroom-challenge="true"]', driver.script)

    def test_owned_lab_action_rejects_non_lab_pages(self):
        driver = _LabDriver()
        driver.current_url = "https://chatgpt.com/auth/login"

        with self.assertRaisesRegex(RuntimeError, "owned classroom lab"):
            complete_owned_lab_challenge(driver)

    def test_owned_lab_runner_connects_existing_functions_once(self):
        driver = _OwnedLabFlowDriver()

        final_state = run_owned_lab_challenge(driver)

        self.assertEqual(driver.action_calls, 1)
        self.assertTrue(final_state["challenge_completed"])
        self.assertEqual(
            final_state["turnstile_response_source"],
            "input[name=cf-turnstile-response]",
        )

    def test_describe_detects_and_prints_steps_without_waiting(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": False,
                    "has_challenge_widget": True,
                    "has_challenge_checkbox": True,
                    "turnstile_response_source": "none",
                }
            ]
        )

        with self.assertLogs("core.browser_challenge", level="INFO") as logs:
            state = describe_browser_challenge(driver)

        self.assertTrue(state["is_challenge"])
        self.assertEqual(
            state["next_steps"],
            [
                "complete the checkbox challenge in the browser",
                "observe the response input or Turnstile API",
                "dispatch the form only after challenge completion",
            ],
        )
        self.assertTrue(
            any("complete the checkbox challenge in the browser" in line for line in logs.output)
        )

    def test_detects_checkbox_only_challenge(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                    "has_challenge_checkbox": True,
                    "turnstile_response_source": "none",
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertTrue(state["is_challenge"])
        self.assertEqual(state["reason"], "challenge checkbox")

    def test_detects_inline_widget_until_response_is_ready(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": True,
                    "has_challenge_widget": True,
                    "turnstile_response_source": "none",
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertTrue(state["is_challenge"])
        self.assertFalse(state["turnstile_response_ready"])
        self.assertFalse(state["challenge_completed"])

    def test_response_ready_completes_challenge_before_widget_disappears(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": False,
                    "has_challenge_widget": True,
                    "turnstile_response_source": "input[name=cf-turnstile-response]",
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertFalse(state["is_challenge"])
        self.assertTrue(state["turnstile_response_ready"])
        self.assertTrue(state["challenge_completed"])
        self.assertEqual(state["turnstile_response_source"], "input[name=cf-turnstile-response]")

    def test_detects_japanese_cloudflare_wait_page_without_form(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "しばらくお待ちください...",
                    "body_text": "しばらくお待ちください...",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertTrue(state["is_challenge"])
        self.assertIn("しばらくお待ちください", state["reason"])

    def test_detects_vietnamese_cloudflare_wait_page_without_form(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "Chờ một chút...",
                    "body_text": "Chờ một chút...",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertTrue(state["is_challenge"])
        self.assertIn("chờ một chút", state["reason"])

    def test_detects_traditional_chinese_cloudflare_security_page_without_form(self):
        driver = _Driver(
            [
                {
                    "url": "https://auth.openai.com/api/accounts/email-otp/send",
                    "title": "請稍候...",
                    "body_text": "正在執行安全驗證。此網站使用安全服務抵禦惡意機器人。",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                }
            ]
        )

        state = _browser_challenge_state(driver)

        self.assertTrue(state["is_challenge"])
        self.assertTrue(any(marker in state["reason"] for marker in ("請稍候", "安全驗證", "惡意機器人")))

    def test_wait_returns_after_challenge_page_clears_without_email_form(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "しばらくお待ちください...",
                    "body_text": "しばらくお待ちください...",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                },
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "Mock challenge cleared",
                    "body_text": "",
                    "has_email_input": False,
                    "has_challenge_widget": False,
                },
            ]
        )

        with patch("core.browser_registration.time.sleep") as sleep:
            state = _wait_for_browser_challenge(driver, timeout=2)

        self.assertFalse(state["is_challenge"])
        sleep.assert_called_once_with(0.5)

    def test_wait_returns_when_response_is_ready_but_widget_remains(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": True,
                    "has_challenge_widget": True,
                    "turnstile_response_source": "none",
                },
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "Select the checkbox to continue.",
                    "has_email_input": True,
                    "has_challenge_widget": True,
                    "turnstile_response_source": "turnstile.getResponse()",
                },
            ]
        )

        with patch("core.browser_registration.time.sleep") as sleep:
            state = _wait_for_browser_challenge(driver, timeout=2)

        self.assertFalse(state["is_challenge"])
        self.assertTrue(state["turnstile_response_ready"])
        self.assertTrue(state["challenge_completed"])
        sleep.assert_called_once_with(0.5)

    def test_wait_fails_with_challenge_context_when_page_never_changes(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "Just a moment...",
                    "body_text": "Checking your browser before accessing ChatGPT.",
                    "has_email_input": False,
                    "has_challenge_widget": True,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "browser challenge was not completed"):
            _wait_for_browser_challenge(driver, timeout=0)

    def test_email_submission_runs_challenge_gate_before_finding_email(self):
        driver = _Driver(
            [
                {
                    "url": "https://chatgpt.com/auth/login",
                    "title": "ChatGPT",
                    "body_text": "",
                    "has_email_input": True,
                    "has_challenge_widget": False,
                }
            ]
        )

        with (
            patch(
                "core.browser_registration._wait_for_browser_challenge",
                return_value={"is_challenge": False},
            ) as wait_for_challenge,
            patch("core.browser_registration._type_email_address"),
            patch(
                "core.browser_registration._email_input_value_state",
                return_value={"inputs": [{"value": "user@example.com"}]},
            ),
            patch("core.browser_registration.human_delay"),
            patch("core.browser_registration._submit_email_step"),
            patch(
                "core.browser_registration._wait_email_submit_next_state",
                return_value="otp",
            ),
        ):
            result = _submit_email_and_wait_next(driver, "user@example.com", attempts=1)

        self.assertEqual(result, "otp")
        wait_for_challenge.assert_called_once()

    def test_navigation_script_string_does_not_break_page_state_checks(self):
        driver = _StringStateDriver()

        self.assertFalse(_is_email_verification_page(driver))
        self.assertFalse(_is_signup_password_page(driver))
        self.assertFalse(_has_access_token(driver))

    def test_transient_state_reader_error_reloads_login_and_retries_email(self):
        driver = Mock()
        email = "user@example.com"

        with (
            patch(
                "core.browser_registration._wait_for_browser_challenge",
                return_value={"is_challenge": False},
            ),
            patch("core.browser_registration._type_email_address") as type_email,
            patch(
                "core.browser_registration._email_input_value_state",
                return_value={"inputs": [{"value": email}]},
            ),
            patch("core.browser_registration._submit_email_step") as submit_email,
            patch(
                "core.browser_registration._wait_email_submit_next_state",
                side_effect=[AttributeError("'str' object has no attribute 'get'"), "otp"],
            ),
            patch("core.browser_registration._maybe_accept") as maybe_accept,
            patch("core.browser_registration._assert_not_external_idp"),
            patch("core.browser_registration.human_delay"),
        ):
            result = _submit_email_and_wait_next(driver, email, attempts=2)

        self.assertEqual(result, "otp")
        self.assertEqual(type_email.call_count, 2)
        self.assertEqual(submit_email.call_count, 2)
        driver.get.assert_called_once_with("https://chatgpt.com/auth/login")
        maybe_accept.assert_called_once_with(driver)


class TurnstileResponseInspectionTests(unittest.TestCase):
    def test_inspector_reports_hidden_input_source_without_exposing_token(self):
        driver = _Driver(
            [
                {
                    "input_present": True,
                    "api_available": True,
                    "source": "input[name=cf-turnstile-response]",
                }
            ]
        )

        state = inspect_turnstile_response(driver)

        self.assertEqual(state["source"], "input[name=cf-turnstile-response]")
        self.assertNotIn("response_length", state)
        self.assertNotIn("token", state)
        self.assertNotIn("response", state)

    def test_inspector_reports_turnstile_api_source_without_exposing_token(self):
        driver = _Driver(
            [
                {
                    "input_present": False,
                    "api_available": True,
                    "source": "turnstile.getResponse()",
                }
            ]
        )

        state = inspect_turnstile_response(driver)

        self.assertEqual(state["source"], "turnstile.getResponse()")
        self.assertNotIn("response_length", state)
        self.assertNotIn("token", state)
        self.assertNotIn("response", state)


if __name__ == "__main__":
    unittest.main()
