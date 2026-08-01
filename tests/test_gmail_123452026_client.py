# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core.gmail_123452026_client import (
    Gmail123452026Account,
    Gmail123452026Error,
    poll_verification_code,
    redeem_cdk,
)


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Gmail123452026ClientTests(unittest.TestCase):
    def test_redeem_parses_active_mailbox_without_exposing_cdk(self):
        session = Mock()
        session.post.return_value = _Response({
            "status": "active",
            "emailAddress": "abcdef@gmail.com",
            "expiresAt": "2026-08-01T00:00:00Z",
            "remainingUses": 4,
        })

        account = redeem_cdk("SECRET-CDK", session=session, allow_insecure_http=True)

        self.assertEqual(account.email, "abcdef@gmail.com")
        self.assertEqual(account.remaining_uses, 4)
        self.assertNotIn("SECRET-CDK", repr(account))
        session.post.assert_called_once_with(
            "http://gmail.123452026.xyz/api/mailbox/redeem",
            json={"cdk": "SECRET-CDK"},
            headers={"Accept": "*/*", "Content-Type": "application/json"},
            timeout=30,
        )

    def test_redeem_accepts_http_endpoint_without_explicit_opt_in(self):
        session = Mock()
        session.post.return_value = _Response({
            "status": "active",
            "emailAddress": "abcdef@gmail.com",
            "remainingUses": 6,
        })

        account = redeem_cdk("SECRET-CDK", session=session, allow_insecure_http=False)

        self.assertEqual(account.email, "abcdef@gmail.com")
        session.post.assert_called_once_with(
            "http://gmail.123452026.xyz/api/mailbox/redeem",
            json={"cdk": "SECRET-CDK"},
            headers={"Accept": "*/*", "Content-Type": "application/json"},
            timeout=30,
        )

    def test_redeem_rejects_inactive_or_invalid_response(self):
        session = Mock()
        session.post.return_value = _Response({"status": "expired"})

        with self.assertRaisesRegex(Gmail123452026Error, "không hoạt động"):
            redeem_cdk("SECRET-CDK", session=session, allow_insecure_http=True)

        session.post.return_value = _Response({"status": "active", "emailAddress": "bad"})
        with self.assertRaisesRegex(Gmail123452026Error, "Gmail"):
            redeem_cdk("SECRET-CDK", session=session, allow_insecure_http=True)

    def test_poll_otp_surfaces_email_invalid_message(self):
        session = Mock()
        session.post.return_value = _Response({
            "status": "email_invalid",
            "message": "The email account is invalid",
        })
        account = Gmail123452026Account(
            email="abcdef@gmail.com",
            cdk="SECRET-CDK",
            remaining_uses=0,
        )

        with self.assertRaisesRegex(Gmail123452026Error, "The email account is invalid"):
            poll_verification_code(
                account,
                max_wait=2,
                poll_interval=0,
                session=session,
            )

        self.assertEqual(session.post.call_count, 1)

    @patch("core.gmail_123452026_client.time.sleep", return_value=None)
    def test_fetch_latest_otp_skips_seen_code_and_returns_fresh_code(self, _sleep):
        session = Mock()
        session.post.side_effect = [
            _Response({"status": "success", "code": "111111"}),
            _Response({"status": "success", "code": "222222"}),
        ]
        account = Gmail123452026Account(
            email="abcdef@gmail.com",
            cdk="SECRET-CDK",
            remaining_uses=6,
        )
        account.seen_codes.add("111111")

        code = poll_verification_code(
            account,
            max_wait=2,
            poll_interval=0,
            session=session,
            allow_insecure_http=True,
        )

        self.assertEqual(code, "222222")
        self.assertIn("222222", account.seen_codes)
        self.assertEqual(session.post.call_count, 2)

    @patch("core.gmail_123452026_client._ledger")
    def test_context_cannot_recover_raw_request_cdk_after_restart(self, ledger):
        from core.gmail_123452026_client import get_account_context
        from core.gmail_cdk_ledger import GmailCdkSlot

        ledger.return_value.find.return_value = GmailCdkSlot(
            email="abcdef@gmail.com",
            status="reserved",
            job_id="17",
            cdk_key="fingerprint-only",
        )

        self.assertIsNone(get_account_context("abcdef@gmail.com"))

    @patch("core.gmail_cdk_ledger.GmailCdkLedger")
    def test_ledger_initialization_reconciles_jobs_and_accounts(self, ledger_class):
        from core import gmail_123452026_client as client

        client._LEDGER = None
        instance = ledger_class.return_value
        with patch("core.db.get_account_by_email", return_value=None), patch(
            "core.db.get_job", return_value={"status": "running"}
        ):
            self.assertIs(client._ledger(), instance)

        instance.reconcile.assert_called_once()
        account_exists = instance.reconcile.call_args.kwargs["account_exists"]
        job_is_active = instance.reconcile.call_args.kwargs["job_is_active"]
        with patch("core.db.get_account_by_email", return_value={"id": 1}):
            self.assertTrue(account_exists("abcdef@gmail.com"))
        with patch("core.db.get_job", return_value={"status": "running"}):
            self.assertTrue(job_is_active("17"))
        with patch("core.db.get_job", return_value={"status": "failed"}):
            self.assertFalse(job_is_active("17"))
        client._LEDGER = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_pick_account_preserves_last_provider_error(self, config_values, redeem):
        from core import gmail_123452026_client as client

        config_values.return_value = ("http://mail.example.com/api", 30, 6, False)
        redeem.side_effect = Gmail123452026Error("API dùng HTTP; cần khởi động lại WebUI")

        with self.assertRaisesRegex(Gmail123452026Error, "khởi động lại WebUI"):
            client.pick_account("1", ["CDK-ONE", "CDK-TWO"])

        self.assertEqual(redeem.call_count, 2)

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_one_cdk_issues_exactly_six_ordered_accounts(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.gmail_cdk_ledger import GmailCdkLedger

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 6)
        client._CONTEXT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = GmailCdkLedger(Path(temp_dir) / "ledger.json")
            emails = [client.pick_account(str(index), [cdk]).email for index in range(6)]
            with self.assertRaisesRegex(Gmail123452026Error, "hết quota"):
                client.pick_account("7", [cdk])

        self.assertEqual(emails[:3], [
            "abcdef@gmail.com",
            "a.bcdef@gmail.com",
            "abcde.f@gmail.com",
        ])
        self.assertEqual(len(emails), len(set(emails)))
        self.assertTrue(all("+" in email for email in emails[3:]))
        client._CONTEXT_CACHE.clear()
        client._LEDGER = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_redeem_remaining_uses_does_not_cap_local_alias_slots(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.gmail_cdk_ledger import GmailCdkLedger

        cdk = "SECRET-CDK"
        config_values.return_value = ("http://gmail.123452026.xyz/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 0)
        client._CONTEXT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = GmailCdkLedger(Path(temp_dir) / "ledger.json")
            account = client.pick_account("1", [cdk])

        self.assertEqual(account.email, "abcdef@gmail.com")
        self.assertEqual(account.remaining_uses, 0)
        client._CONTEXT_CACHE.clear()
        client._LEDGER = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_aliases_from_same_cdk_share_seen_otp_codes(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.gmail_cdk_ledger import GmailCdkLedger

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = GmailCdkLedger(Path(temp_dir) / "ledger.json")
            first = client.pick_account("1", [cdk])
            second = client.pick_account("2", [cdk])

        first.seen_codes.add("111111")
        self.assertIn("111111", second.seen_codes)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._LEDGER = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_pick_account_by_inventory_resolves_cdk_and_reserves_slot(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, created = store.import_cdk("gmail", cdk)
            self.assertTrue(created)
            client._INVENTORY_STORE = store

            account = client.pick_account_by_inventory(
                job_id="job-1",
                inventory_ids=[inventory.inventory_id],
            )

        self.assertEqual(account.email, "abcdef@gmail.com")
        self.assertEqual(account.inventory_id, inventory.inventory_id)
        self.assertTrue(account.reservation_id)
        self.assertTrue(account.owner_token)
        self.assertNotIn(cdk, repr(account))
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._INVENTORY_STORE = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_inventory_preserves_alias_order_and_six_slot_capacity(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("gmail", cdk)
            client._INVENTORY_STORE = store

            emails = [
                client.pick_account_by_inventory(
                    job_id=str(index),
                    inventory_ids=[inventory.inventory_id],
                ).email
                for index in range(6)
            ]

        self.assertEqual(emails[:3], [
            "abcdef@gmail.com",
            "a.bcdef@gmail.com",
            "abcde.f@gmail.com",
        ])
        self.assertEqual(len(emails), len(set(emails)))
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._INVENTORY_STORE = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_inventory_remaining_uses_zero_does_not_cap_local(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 0)
        client._CONTEXT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("gmail", cdk)
            client._INVENTORY_STORE = store

            account = client.pick_account_by_inventory(
                job_id="1",
                inventory_ids=[inventory.inventory_id],
            )

            self.assertEqual(account.email, "abcdef@gmail.com")
            self.assertEqual(account.remaining_uses, 0)
            store_record = store.get_inventory(inventory.inventory_id)
            self.assertEqual(store_record.provider_remaining, 0)
        client._CONTEXT_CACHE.clear()
        client._INVENTORY_STORE = None

    @patch("core.gmail_123452026_client.redeem_cdk")
    @patch("core.gmail_123452026_client._config_values")
    def test_restart_lookup_recovers_inventory_context(self, config_values, redeem):
        import tempfile
        from pathlib import Path

        from core import gmail_123452026_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        cdk = "SECRET-CDK"
        config_values.return_value = ("https://mail.example.com/api", 30, 6, False)
        redeem.return_value = Gmail123452026Account("abcdef@gmail.com", cdk, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "cdk.sqlite3"
            store = CdkInventoryStore(store_path)
            inventory, _ = store.import_cdk("gmail", cdk)
            client._INVENTORY_STORE = store

            account = client.pick_account_by_inventory(
                job_id="job-1",
                inventory_ids=[inventory.inventory_id],
            )
            email = account.email

            client._CONTEXT_CACHE.clear()
            client._SEEN_CODES_BY_CDK.clear()

            reopened = CdkInventoryStore(store_path)
            client._INVENTORY_STORE = reopened
            ctx = client.get_account_context(email)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.email, email)
            self.assertEqual(ctx.inventory_id, inventory.inventory_id)
            self.assertNotIn(cdk, repr(ctx))
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._INVENTORY_STORE = None


if __name__ == "__main__":
    unittest.main()
