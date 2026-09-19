import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import email_provider


class AutomatedEmailApiProviderTests(unittest.TestCase):
    def test_source_aliases_are_gmail_provider_only(self):
        self.assertTrue(email_provider.is_valid_email_source("automated_email_api"))
        self.assertEqual(email_provider.normalize_email_source("email_api"), "automated_email_api")
        self.assertEqual(email_provider.normalize_email_source("automated_email"), "automated_email_api")

    @patch("core.automated_email_api_client.pick_account")
    def test_acquire_routes_to_automated_email_api_client(self, pick):
        pick.return_value = SimpleNamespace(email="alias@gmail.com")

        result = email_provider.acquire_email(email_source="automated_email_api")

        self.assertEqual(result, "alias@gmail.com")
        pick.assert_called_once()

    @patch("core.automated_email_api_client.get_account_context")
    def test_resolve_source_recognizes_automated_alias(self, context):
        context.return_value = SimpleNamespace(email="alias@gmail.com")

        self.assertEqual(email_provider.resolve_email_source("alias@gmail.com"), "automated_email_api")

    @patch("core.email_provider.resolve_email_source", return_value="automated_email_api")
    @patch("core.automated_email_api_client.fetch_latest_otp", return_value="123456")
    def test_wait_routes_to_automated_client(self, fetch, source):
        with patch("config.email.USE_EMAIL_SERVICE", True):
            result = email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0, max_wait=30)

        self.assertEqual(result, "123456")
        fetch.assert_called_once()

    @patch("core.email_provider.resolve_email_source", return_value="automated_email_api")
    @patch("core.automated_email_api_client.release_account", return_value=True)
    def test_release_preserves_terminal_410_status(self, release, source):
        result = email_provider.release_email_if_unconsumed(
            "alias@gmail.com",
            note="等待验证码失败: HTTP 410; email unavailable",
        )

        self.assertTrue(result)
        release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="等待验证码失败: HTTP 410; email unavailable",
        )

    @patch("core.email_provider.resolve_email_source", return_value="automated_email_api")
    @patch("core.automated_email_api_client.mark_account_consumed", return_value=True)
    def test_mark_consumed_routes_to_automated_client(self, consumed, source):
        self.assertTrue(email_provider.mark_email_consumed("alias@gmail.com"))
        consumed.assert_called_once_with("alias@gmail.com")


if __name__ == "__main__":
    unittest.main()
