# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class Gmail123452026ProviderTests(unittest.TestCase):
    def test_parse_sources_keeps_gmail_cdk_in_order(self):
        self.assertEqual(
            email_provider.parse_email_sources("gmail_123452026,outlook"),
            ["gmail_123452026", "outlook"],
        )

    @patch("core.gmail_123452026_client.pick_account")
    def test_acquire_email_passes_job_owner_and_batch_cdks(self, pick_account):
        pick_account.return_value.email = "abcdef@gmail.com"

        with patch("core.email_provider.parse_email_sources", return_value=["gmail_123452026"]):
            email = email_provider.acquire_email(job_id=17, gmail_cdks=["CDK-ONE", "CDK-TWO"])

        self.assertEqual(email, "abcdef@gmail.com")
        pick_account.assert_called_once_with(job_id="17", cdks=["CDK-ONE", "CDK-TWO"])

    @patch("core.gmail_123452026_client.pick_account_by_batch")
    def test_acquire_email_uses_persisted_gmail_batch(self, pick_account):
        pick_account.return_value.email = "abcdef@route-one.net"

        email = email_provider.acquire_email(
            job_id=17,
            gmail_batch_id="batch-123",
            gmail_routed_domains=["route-one.net"],
            email_source="gmail_123452026",
        )

        self.assertEqual(email, "abcdef@route-one.net")
        pick_account.assert_called_once_with(
            job_id="17",
            batch_id="batch-123",
            routed_domains=["route-one.net"],
        )

    @patch("core.gmail_123452026_client.pick_account_by_inventory")
    def test_acquire_email_passes_routed_domains_to_inventory(self, pick_account):
        pick_account.return_value.email = "abcdef@route-one.net"

        email = email_provider.acquire_email(
            job_id=17,
            gmail_inventory_ids=["inventory-1"],
            gmail_routed_domains=["route-one.net", "route-two.org"],
            email_source="gmail_123452026",
        )

        self.assertEqual(email, "abcdef@route-one.net")
        pick_account.assert_called_once_with(
            job_id="17",
            inventory_ids=["inventory-1"],
            routed_domains=["route-one.net", "route-two.org"],
        )


    @patch("core.gmail_123452026_client.pick_account")
    def test_acquire_email_passes_request_scoped_routed_domains(self, pick_account):
        pick_account.return_value.email = "abcdef@route-one.net"

        email = email_provider.acquire_email(
            job_id=17,
            gmail_cdks=["CDK-ONE"],
            gmail_routed_domains=["route-one.net", "route-two.org"],
            email_source="gmail_123452026",
        )

        self.assertEqual(email, "abcdef@route-one.net")
        pick_account.assert_called_once_with(
            job_id="17",
            cdks=["CDK-ONE"],
            routed_domains=["route-one.net", "route-two.org"],
        )

    @patch("core.gmail_123452026_client.pick_account", side_effect=RuntimeError("redeem failed"))
    @patch("core.outlook_client.pick_account")
    def test_explicit_gmail_source_does_not_fallback_to_outlook(self, outlook_pick, _gmail_pick):
        with self.assertRaisesRegex(RuntimeError, "gmail_123452026"):
            email_provider.acquire_email(
                job_id=17,
                gmail_cdks=["CDK-ONE"],
                email_source="gmail_123452026",
            )

        outlook_pick.assert_not_called()

    @patch("core.gmail_123452026_client.get_account_context", return_value=object())
    def test_resolve_email_source_recognizes_gmail_cdk_context(self, get_context):
        self.assertEqual(email_provider.resolve_email_source("abcdef@gmail.com"), "gmail_123452026")
        get_context.assert_called_once_with("abcdef@gmail.com")

    @patch("core.gmail_123452026_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_123452026")
    def test_wait_for_otp_routes_to_gmail_cdk_client(self, _resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            code = email_provider.wait_for_otp("abcdef@gmail.com", after_ts=123.0, max_wait=12)

        self.assertEqual(code, "654321")
        fetch_latest_otp.assert_called_once_with("abcdef@gmail.com", after_ts=123.0, max_wait=12)

    @patch("core.gmail_123452026_client.release_account", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="gmail_123452026")
    def test_release_unconsumed_returns_gmail_cdk_reservation(self, _resolve, release_account):
        self.assertTrue(email_provider.release_email_if_unconsumed("abcdef@gmail.com", note="failed"))
        release_account.assert_called_once_with("abcdef@gmail.com", status="available", note="failed")

    @patch("core.gmail_123452026_client.mark_account_consumed", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="gmail_123452026")
    def test_mark_email_consumed_commits_gmail_cdk_reservation(self, _resolve, mark_consumed):
        self.assertTrue(email_provider.mark_email_consumed("abcdef@gmail.com"))
        mark_consumed.assert_called_once_with("abcdef@gmail.com")


if __name__ == "__main__":
    unittest.main()
