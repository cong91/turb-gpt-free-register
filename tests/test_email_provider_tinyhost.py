import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider, tinyhost_mail_client


class EmailProviderTinyHostTests(unittest.TestCase):
    def setUp(self):
        tinyhost_mail_client._CONTEXT_CACHE.clear()

    def test_parse_email_sources_keeps_tinyhost(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook,tinyhost,gptmail"),
            ["outlook", "tinyhost", "gptmail"],
        )

    @patch("core.tinyhost_mail_client.create_account")
    def test_acquire_email_uses_tinyhost_account_creator(self, create_account):
        create_account.return_value.email = "fresh@tinyhost.test"

        with patch.object(email_provider, "parse_email_sources", return_value=["tinyhost"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@tinyhost.test")

        create_account.assert_called_once()

    @patch("core.tinyhost_mail_client.get_account_context", return_value=object())
    def test_resolve_email_source_recognizes_tinyhost_context(self, get_context):
        self.assertEqual(email_provider.resolve_email_source("fresh@tinyhost.test"), "tinyhost")
        get_context.assert_called_once_with("fresh@tinyhost.test")

    @patch("core.tinyhost_mail_client.fetch_latest_otp", return_value="112233")
    @patch("core.email_provider.resolve_email_source", return_value="tinyhost")
    def test_wait_for_otp_uses_tinyhost_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.wait_for_otp("fresh@tinyhost.test", after_ts=123.0),
                "112233",
            )

        resolve.assert_called_once_with("fresh@tinyhost.test")
        fetch_latest_otp.assert_called_once_with("fresh@tinyhost.test", after_ts=123.0)

    @patch("core.tinyhost_mail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="tinyhost")
    def test_release_email_uses_tinyhost_client(self, resolve, release_account):
        self.assertEqual(
            email_provider.release_email("fresh@tinyhost.test", status="failed"),
            "tinyhost",
        )
        release_account.assert_called_once_with("fresh@tinyhost.test", status="failed", note=None)

    @patch("core.tinyhost_mail_client.mark_domain_supported", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="tinyhost")
    def test_mark_email_consumed_records_tinyhost_domain_support(self, resolve, mark_domain_supported):
        self.assertTrue(email_provider.mark_email_consumed("fresh@tinyhost.test"))
        resolve.assert_called_once_with("fresh@tinyhost.test")
        mark_domain_supported.assert_called_once_with("fresh@tinyhost.test")


if __name__ == "__main__":
    unittest.main()
