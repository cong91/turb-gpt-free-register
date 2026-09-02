# -*- coding: utf-8 -*-
import unittest

from core.browser_use_codex_oauth import (
    _classify_credential_login_state,
    _wait_after_email_submit,
)
from core.browser_use_registration import _quick_auth_state
from core.openai_auth import (
    account_unusable_message,
    account_unusable_error_message,
    detect_account_unusable_response_body,
    detect_account_unusable_text,
)


class CodexDeadAccountDetectionTests(unittest.TestCase):
    def test_account_deactivated_has_vietnamese_user_status(self):
        self.assertEqual(
            account_unusable_message("account_deactivated"),
            "OpenAI đã khóa tài khoản",
        )
        self.assertEqual(
            account_unusable_error_message("account_deactivated"),
            "OpenAI đã khóa tài khoản (account_deactivated)",
        )

    def test_browser_use_auth_state_detects_deactivated_page_text(self):
        html = (
            "Authentication Error You do not have an account because it has been deleted "
            "or deactivated. error_code: account_deactivated"
        )

        class Page:
            def evaluate(self, _script):
                return {
                    "state": "login_password",
                    "url": "https://auth.openai.com/log-in/password",
                    "textPreview": html,
                }

        self.assertEqual(_quick_auth_state(Page())["state"], "deactivated:account_deactivated")
        self.assertEqual(_classify_credential_login_state(Page()), "deactivated:account_deactivated")

    def test_detect_account_unusable_response_body_uses_error_code(self):
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deactivated"}}'),
            "account_deactivated",
        )
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deleted"}}'),
            "account_deleted",
        )

    def test_detect_account_unusable_text_handles_html_message_without_code(self):
        self.assertEqual(
            detect_account_unusable_text(
                "Authentication Error: You do not have an account because it has been deleted or deactivated."
            ),
            "account_deactivated",
        )
        self.assertEqual(detect_account_unusable_response_body('Your account has been deactivated.'), "")

    def test_detect_account_locked_text_as_unusable(self):
        self.assertEqual(
            detect_account_unusable_text("Authentication Error: Your account has been locked."),
            "account_deactivated",
        )

    def test_browser_use_email_submit_returns_deactivated_from_response_tracker(self):
        class Body:
            def inner_text(self, timeout=1000):
                return "Your account has been deactivated."

        class Page:
            url = "https://auth.openai.com/email-verification"
            def locator(self, selector):
                return Body()

        tracker = {"code": "account_deactivated"}
        self.assertEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker=tracker), "deactivated:account_deactivated")
        self.assertEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker={}), "deactivated:account_deactivated")

    def test_browser_use_email_submit_detects_deactivated_html_before_url_acceptance(self):
        html = (
            "<h1>Authentication Error</h1>"
            "You do not have an account because it has been deleted or deactivated."
            "error_code: account_deactivated"
        )

        class Body:
            def inner_text(self, timeout=1000):
                return html

        class Page:
            url = "https://auth.openai.com/log-in/password"

            def locator(self, _selector):
                return Body()

        self.assertEqual(
            _wait_after_email_submit(Page(), timeout=1, dead_tracker={}),
            "deactivated:account_deactivated",
        )


if __name__ == "__main__":
    unittest.main()
