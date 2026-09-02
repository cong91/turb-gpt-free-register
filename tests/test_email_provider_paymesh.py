import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class PaymeshEmailProviderTests(unittest.TestCase):
    def test_parse_sources_normalizes_paymesh_domain_alias(self):
        self.assertEqual(
            email_provider.parse_email_sources("sms.paymesh.cn,outlook"),
            ["paymesh", "outlook"],
        )

    @patch("core.paymesh_mail_client.pick_account")
    def test_acquire_email_passes_job_owner_and_paymesh_cards(self, pick_account):
        pick_account.return_value.email = "user@example.com"

        email = email_provider.acquire_email(
            job_id=17,
            paymesh_cdks=["MAIL-ONE", "MAIL-TWO"],
            email_source="paymesh",
        )

        self.assertEqual(email, "user@example.com")
        pick_account.assert_called_once_with(job_id="17", cdks=["MAIL-ONE", "MAIL-TWO"], routed_domains=())

    @patch("core.paymesh_mail_client.pick_account")
    def test_acquire_email_forwards_paymesh_routed_domains(self, pick_account):
        pick_account.return_value.email = "user@test.com"

        email = email_provider.acquire_email(
            job_id=18,
            paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["test.com"],
            email_source="paymesh",
        )

        self.assertEqual(email, "user@test.com")
        pick_account.assert_called_once_with(
            job_id="18", cdks=["MAIL-ONE"], routed_domains=["test.com"]
        )

    @patch("core.paymesh_mail_client.pick_account_for_inventory")
    def test_acquire_email_uses_assigned_paymesh_inventory(self, pick_account):
        pick_account.return_value.email = "user@example.com"

        email = email_provider.acquire_email(
            job_id=19,
            paymesh_inventory_id="inventory-4",
            paymesh_routed_domains=["test.com"],
            email_source="paymesh",
        )

        self.assertEqual(email, "user@example.com")
        pick_account.assert_called_once_with(
            job_id="19", inventory_id="inventory-4", routed_domains=["test.com"]
        )

    @patch("core.paymesh_mail_client.pick_account", side_effect=RuntimeError("redeem failed"))
    @patch("core.outlook_client.pick_account")
    def test_explicit_paymesh_source_does_not_fallback_to_outlook(self, outlook_pick, _paymesh_pick):
        with self.assertRaisesRegex(RuntimeError, "paymesh"):
            email_provider.acquire_email(
                job_id=17,
                paymesh_cdks=["MAIL-ONE"],
                email_source="paymesh",
            )

        outlook_pick.assert_not_called()

    @patch("core.paymesh_mail_client.get_account_context", return_value=object())
    @patch("core.gmail_123452026_client.get_account_context", return_value=None)
    def test_resolve_email_source_recognizes_paymesh_context(self, _gmail_context, paymesh_context):
        self.assertEqual(email_provider.resolve_email_source("user@example.com"), "paymesh")
        paymesh_context.assert_called_once_with("user@example.com")

    @patch("core.paymesh_mail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="paymesh")
    def test_wait_for_otp_routes_to_paymesh_client(self, _resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            code = email_provider.wait_for_otp("user@example.com", after_ts=123.0, max_wait=12)

        self.assertEqual(code, "654321")
        fetch_latest_otp.assert_called_once_with("user@example.com", after_ts=123.0, max_wait=12)

    @patch("core.paymesh_mail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="paymesh")
    def test_wait_for_otp_uses_paymesh_specific_default(self, _resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "PAYMESH_OTP_MAX_WAIT", 180):
            code = email_provider.wait_for_otp("user@example.com", after_ts=123.0)

        self.assertEqual(code, "654321")
        fetch_latest_otp.assert_called_once_with(
            "user@example.com", after_ts=123.0, max_wait=180
        )

    @patch("core.outlook_client.fetch_latest_otp", return_value="123456")
    @patch("core.email_provider.resolve_email_source", return_value="outlook")
    def test_wait_for_otp_keeps_global_default_for_other_sources(self, _resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "OTP_MAX_WAIT", 90):
            code = email_provider.wait_for_otp("user@outlook.com", after_ts=123.0)

        self.assertEqual(code, "123456")
        fetch_latest_otp.assert_called_once_with(
            "user@outlook.com", after_ts=123.0
        )

    @patch("core.paymesh_mail_client.release_account", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="paymesh")
    def test_release_unconsumed_returns_paymesh_reservation(self, _resolve, release_account):
        self.assertTrue(email_provider.release_email_if_unconsumed("user@example.com", note="failed"))
        release_account.assert_called_once_with("user@example.com", status="available", note="failed")

    @patch("core.paymesh_mail_client.mark_account_consumed", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="paymesh")
    def test_mark_email_consumed_commits_paymesh_reservation(self, _resolve, mark_consumed):
        self.assertTrue(email_provider.mark_email_consumed("user@example.com"))
        mark_consumed.assert_called_once_with("user@example.com")


if __name__ == "__main__":
    unittest.main()
