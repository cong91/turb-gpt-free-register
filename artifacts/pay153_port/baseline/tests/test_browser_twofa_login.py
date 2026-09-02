import unittest
from unittest.mock import patch

from core.browser_twofa_login import _login_existing_account, _login_password
from core.openai_auth import AccountUnusableError


class BrowserTwofaLoginTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
