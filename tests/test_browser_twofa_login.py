import unittest
from unittest.mock import patch

from config import twofa as twofa_config
from core.browser_twofa_login import _login_existing_account, _login_password
from core.openai_auth import AccountUnusableError


class BrowserTwofaLoginTests(unittest.TestCase):
    @patch("core.browser_twofa_login._wait_for_password_submit_state", return_value="otp")
    @patch("core.browser_twofa_login._human_click")
    @patch("core.browser_twofa_login._human_type_text")
    @patch(
        "core.browser_twofa_login._find_login_password_controls",
        side_effect=[
            {"ok": True, "input": object(), "button": object()},
            {"ok": True, "input": object(), "button": object(), "type": "submit"},
        ],
    )
    @patch("core.browser_twofa_login._raise_if_account_unusable")
    @patch("core.browser_twofa_login.human_delay")
    def test_login_password_waits_for_enabled_submit_and_returns_otp_state(
        self,
        _human_delay,
        _raise_unusable,
        find_controls,
        type_text,
        click,
        wait_state,
    ):
        driver = type("Driver", (), {"current_url": "https://auth.openai.com/log-in/password"})()

        state = _login_password(driver, "password", timeout=1)

        self.assertEqual(state, "otp")
        self.assertEqual(find_controls.call_args_list[0].kwargs, {})
        self.assertTrue(find_controls.call_args_list[1].kwargs["require_enabled_submit"])
        type_text.assert_called_once()
        click.assert_called_once()
        wait_state.assert_called_once()

    @patch(
        "core.browser_twofa_login._wait_for_password_submit_state",
        side_effect=["login_password", "otp"],
    )
    @patch("core.browser_twofa_login._request_login_password_submit", return_value=True)
    @patch("core.browser_twofa_login._human_click")
    @patch("core.browser_twofa_login._human_type_text")
    @patch(
        "core.browser_twofa_login._find_login_password_controls",
        side_effect=[
            {"ok": True, "input": object(), "button": object()},
            {"ok": True, "input": object(), "button": object()},
        ],
    )
    @patch("core.browser_twofa_login._raise_if_account_unusable")
    @patch("core.browser_twofa_login.human_delay")
    def test_login_password_uses_native_submit_when_click_stays_on_password_page(
        self,
        _human_delay,
        _raise_unusable,
        _find_controls,
        _type_text,
        _click,
        request_submit,
        _wait_state,
    ):
        driver = type("Driver", (), {"current_url": "https://auth.openai.com/log-in/password"})()

        state = _login_password(driver, "password", timeout=1)

        self.assertEqual(state, "otp")
        request_submit.assert_called_once_with(driver)

    @patch("core.browser_twofa_login._wait_for_password_submit_state", return_value="login_password")
    @patch("core.browser_twofa_login._request_login_password_submit", return_value=False)
    @patch("core.browser_twofa_login._human_click")
    @patch("core.browser_twofa_login._human_type_text")
    @patch(
        "core.browser_twofa_login._find_login_password_controls",
        side_effect=[
            {"ok": True, "input": object(), "button": object()},
            {"ok": True, "input": object(), "button": object()},
        ],
    )
    @patch("core.browser_twofa_login._raise_if_account_unusable")
    @patch("core.browser_twofa_login.human_delay")
    def test_login_password_never_falls_through_to_otp_when_submit_fails(
        self,
        _human_delay,
        _raise_unusable,
        _find_controls,
        _type_text,
        _click,
        request_submit,
        _wait_state,
    ):
        driver = type("Driver", (), {"current_url": "https://auth.openai.com/log-in/password"})()

        with self.assertRaisesRegex(RuntimeError, "仍停留在密码页"):
            _login_password(driver, "password", timeout=1)

        request_submit.assert_called_once_with(driver)

    def test_password_login_stops_when_deactivated_html_is_rendered(self):
        html = (
            '<div class="_titleBlock"><h1>Authentication Error</h1>'
            '<div>You do not have an account because it has been deleted or deactivated.</div>'
            '<span>error_code: account_deactivated</span></div>'
        )

        class Driver:
            current_url = "https://auth.openai.com/log-in/password"

            def execute_script(self, script, *_args):
                if "querySelectorAll('input[type=\"password\"]" in script:
                    return {"ok": True}
                return html

        with self.assertRaisesRegex(AccountUnusableError, "account_deactivated"):
            _login_password(Driver(), "password", timeout=1)

    @patch("core.browser_twofa_login.wait_for_otp")
    @patch("core.browser_twofa_login._login_password", side_effect=RuntimeError("password submit stuck"))
    @patch("core.browser_twofa_login._submit_email_and_wait_next", return_value="login_password")
    @patch("core.browser_twofa_login.snapshot_verification_code", return_value=None)
    @patch("core.browser_twofa_login._maybe_accept")
    @patch("core.browser_twofa_login.human_delay")
    def test_existing_login_does_not_poll_otp_when_password_submit_fails(
        self,
        _human_delay,
        _maybe_accept,
        _snapshot,
        _submit_email,
        _login_password_mock,
        wait_for_otp,
    ):
        driver = type("Driver", (), {"get": lambda self, _url: None})()

        with self.assertRaisesRegex(RuntimeError, "password submit stuck"):
            _login_existing_account(driver, "user@example.com", "password")

        wait_for_otp.assert_not_called()

    @patch("core.browser_twofa_login._fetch_chatgpt_session", return_value={"accessToken": "token"})
    @patch("core.browser_twofa_login.wait_for_otp")
    @patch("core.browser_twofa_login._submit_email_and_wait_next", return_value="logged_in")
    @patch("core.browser_twofa_login._maybe_accept")
    @patch("core.browser_twofa_login.human_delay")
    def test_existing_logged_in_session_skips_login_otp(
        self,
        _human_delay,
        _maybe_accept,
        _submit_email,
        wait_for_otp,
        fetch_session,
    ):
        driver = type("Driver", (), {"get": lambda self, _url: None})()

        result = _login_existing_account(driver, "user@example.com", "password")

        self.assertEqual(result["accessToken"], "token")
        wait_for_otp.assert_not_called()
        fetch_session.assert_called_once_with(driver, timeout=120)

    @patch("core.browser_twofa_login._fetch_chatgpt_session", return_value={"accessToken": "token"})
    @patch("core.browser_twofa_login._has_access_token", return_value=True)
    @patch("core.browser_twofa_login._submit_email_and_wait_next")
    @patch("core.browser_twofa_login._maybe_accept")
    @patch("core.browser_twofa_login.human_delay")
    def test_existing_profile_session_skips_email_input(
        self,
        _human_delay,
        _maybe_accept,
        submit_email,
        has_token,
        fetch_session,
    ):
        driver = type("Driver", (), {"get": lambda self, _url: None})()

        result = _login_existing_account(driver, "user@example.com", "password")

        self.assertEqual(result["accessToken"], "token")
        has_token.assert_called_once_with(driver)
        submit_email.assert_not_called()
        fetch_session.assert_called_once_with(driver, timeout=120)

    @patch("core.browser_twofa_login._fetch_chatgpt_session", return_value={"accessToken": "token"})
    @patch("core.browser_twofa_login._is_email_verification_page", return_value=False)
    @patch("core.browser_twofa_login._wait_after_email_otp_submit", return_value="invalid")
    @patch("core.browser_twofa_login._click_continue")
    @patch("core.browser_twofa_login._type_otp")
    @patch("core.browser_twofa_login._clear_otp_inputs")
    @patch("core.browser_twofa_login.wait_for_otp", return_value="123456")
    @patch("core.browser_twofa_login._submit_email_and_wait_next", return_value="otp")
    @patch("core.browser_twofa_login._maybe_accept")
    @patch("core.browser_twofa_login._has_access_token", return_value=False)
    @patch("core.browser_twofa_login.human_delay")
    def test_existing_login_skips_resend_when_otp_submission_already_left_page(
        self,
        _human_delay,
        _has_token,
        _maybe_accept,
        _submit_email,
        wait_for_otp,
        _clear_otp,
        _type_otp,
        _click_continue,
        _wait_after_submit,
        _is_email_page,
        fetch_session,
    ):
        driver = type("Driver", (), {"get": lambda self, _url: None})()

        with patch("core.browser_twofa_login._click_resend_email_otp") as resend:
            result = _login_existing_account(driver, "user@example.com", "password")

        self.assertEqual(result["accessToken"], "token")
        resend.assert_not_called()
        self.assertEqual(
            wait_for_otp.call_args.kwargs["max_wait"],
            int(getattr(twofa_config, "TWOFA_OTP_MAX_WAIT", 90) or 90),
        )
        fetch_session.assert_called_once_with(driver, timeout=120)


if __name__ == "__main__":
    unittest.main()
