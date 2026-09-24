import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import email_provider


class BambooMmoEmailProviderTests(unittest.TestCase):
    def test_source_is_normalized(self):
        self.assertTrue(email_provider.is_valid_email_source("bamboommo"))
        self.assertEqual(email_provider.normalize_email_source("bamboo_mmo"), "bamboommo")

    @patch("core.bamboommo_client.pick_account")
    def test_acquire_routes_to_bamboommo(self, pick):
        pick.return_value = SimpleNamespace(email="alias@gmail.com")
        self.assertEqual(email_provider.acquire_email(email_source="bamboommo"), "alias@gmail.com")
        pick.assert_called_once()

    @patch("core.email_provider.resolve_email_source", return_value="bamboommo")
    @patch("core.bamboommo_client.fetch_latest_otp", return_value="123456")
    def test_wait_routes_to_bamboommo(self, fetch, source):
        with patch("config.email.USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0, max_wait=30), "123456")
        fetch.assert_called_once_with("alias@gmail.com", after_ts=1.0, max_wait=30)

    @patch("core.email_provider.resolve_email_source", return_value="bamboommo")
    @patch("core.bamboommo_client.release_account", return_value=True)
    def test_release_routes_to_bamboommo(self, release, source):
        self.assertTrue(email_provider.release_email_if_unconsumed("alias@gmail.com", note="failed"))
        release.assert_called_once_with("alias@gmail.com", status="failed", note="failed")

    @patch("core.email_provider.resolve_email_source", return_value="bamboommo")
    @patch("core.bamboommo_client.mark_account_consumed", return_value=True)
    def test_mark_consumed_routes_to_bamboommo(self, consumed, source):
        self.assertTrue(email_provider.mark_email_consumed("alias@gmail.com"))
        consumed.assert_called_once_with("alias@gmail.com")


if __name__ == "__main__":
    unittest.main()
