import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import email_provider


class OtpGmailProviderTests(unittest.TestCase):
    def test_source_aliases_are_normalized(self):
        self.assertTrue(email_provider.is_valid_email_source("otpmail"))
        self.assertEqual(email_provider.normalize_email_source("otpgmail"), "otpmail")
        self.assertEqual(email_provider.normalize_email_source("otp_gmail"), "otpmail")
        self.assertEqual(email_provider.normalize_email_source("otpgmail.net"), "otpmail")

    @patch("core.otpgmail_client.pick_account")
    def test_acquire_routes_to_otpgmail_client(self, pick):
        pick.return_value = SimpleNamespace(email="alias@gmail.com")

        self.assertEqual(email_provider.acquire_email(email_source="otpgmail"), "alias@gmail.com")
        pick.assert_called_once()

    @patch("core.otpgmail_client.get_account_context")
    def test_resolve_source_recognizes_otpgmail_context(self, context):
        context.return_value = SimpleNamespace(email="alias@gmail.com")

        self.assertEqual(email_provider.resolve_email_source("alias@gmail.com"), "otpmail")

    @patch("core.email_provider.resolve_email_source", return_value="otpmail")
    @patch("core.otpgmail_client.fetch_latest_otp", return_value="123456")
    def test_wait_passes_before_code_to_otpgmail(self, fetch, source):
        with patch("config.email.USE_EMAIL_SERVICE", True):
            result = email_provider.wait_for_otp(
                "alias@gmail.com", after_ts=1.0, max_wait=30, before_code="111111"
            )

        self.assertEqual(result, "123456")
        fetch.assert_called_once_with(
            "alias@gmail.com",
            after_ts=1.0,
            max_wait=30,
            before_code="111111",
        )

    @patch("core.db.get_job")
    @patch("core.registration_service._THREAD_CTX", SimpleNamespace(job_id=772))
    @patch("core.email_provider.resolve_email_source", return_value="otpmail")
    @patch("core.otpgmail_client.fetch_latest_otp", return_value="654321")
    def test_wait_passes_persisted_job_order_id_to_otpgmail(
        self, fetch, source, get_job
    ):
        get_job.return_value = {
            "email": "alias@gmail.com",
            "email_source": "otpmail",
            "provider_context": {
                "otpmail_order_id": "ord-job",
                "otpmail_query_email": "root@gmail.com",
                "otpmail_aliases": ["alias@gmail.com"],
            },
        }

        with patch("config.email.USE_EMAIL_SERVICE", True):
            result = email_provider.wait_for_otp(
                "alias@gmail.com", after_ts=1.0, max_wait=30
            )

        self.assertEqual(result, "654321")
        fetch.assert_called_once_with(
            "alias@gmail.com",
            after_ts=1.0,
            max_wait=30,
            order_id="ord-job",
        )

    @patch("core.email_provider.resolve_email_source", return_value="otpmail")
    @patch("core.otpgmail_client.release_account", return_value=True)
    def test_release_routes_failed_cleanup_to_cancel_capable_client(self, release, source):
        self.assertTrue(email_provider.release_email_if_unconsumed("alias@gmail.com", note="registration failed"))
        release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="registration failed",
        )

    @patch("core.email_provider.resolve_email_source", return_value="otpmail")
    @patch("core.otpgmail_client.mark_account_consumed", return_value=True)
    def test_mark_consumed_routes_to_otpgmail(self, consumed, source):
        self.assertTrue(email_provider.mark_email_consumed("alias@gmail.com"))
        consumed.assert_called_once_with("alias@gmail.com")


if __name__ == "__main__":
    unittest.main()
