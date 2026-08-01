# -*- coding: utf-8 -*-
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.paymesh_mail_client import (
    PaymeshMailAccount,
    PaymeshMailError,
    poll_verification_code,
    redeem_cdk,
)
from core.provider_card_ledger import ProviderCardLedger


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class PaymeshMailClientTests(unittest.TestCase):
    def test_redeem_uses_paymesh_contract_without_exposing_card(self):
        session = Mock()
        session.post.return_value = _Response({
            "code": 0,
            "data": {
                "emailAddress": "User@example.com",
                "endTime": "2026-08-01T00:00:00Z",
            },
        })

        account = redeem_cdk("SECRET-CARD", session=session)

        self.assertEqual(account.email, "user@example.com")
        self.assertNotIn("SECRET-CARD", repr(account))
        session.post.assert_called_once_with(
            "https://sms.paymesh.cn/api/v1/redeem",
            json={"code": "SECRET-CARD"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )

    def test_active_card_falls_back_to_lookup(self):
        session = Mock()
        session.post.return_value = _Response({"code": 2004, "msg": "card in use"})
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {
                        "emailAddress": "user@example.com",
                        "status": "active",
                        "expiresAt": "2026-08-01T00:00:00Z",
                    },
                    "codes": [],
                },
            },
        })

        account = redeem_cdk("MAIL A/B", session=session)

        self.assertEqual(account.email, "user@example.com")
        session.get.assert_called_once_with(
            "https://sms.paymesh.cn/api/v1/order/lookup?code=MAIL%20A%2FB&poll=true",
            headers={"Accept": "application/json"},
            timeout=30,
        )

    def test_redeem_without_mailbox_uses_lookup(self):
        session = Mock()
        session.post.return_value = _Response({"code": 0, "data": {"type": "email"}})
        session.get.return_value = _Response({
            "code": 0,
            "data": {"session": {"emailAddress": "user@example.com", "status": "active"}},
        })

        account = redeem_cdk("MAIL-ONE", session=session)

        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(session.get.call_count, 1)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_poll_baselines_old_code_and_returns_newest_unseen(self, _sleep):
        session = Mock()
        account = PaymeshMailAccount(
            email="user+pm01@example.com",
            cdk="MAIL-ONE",
            remaining_uses=6,
            seen_codes={"id:1"},
        )
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [
                        {"id": 1, "code": "111111", "receivedAt": "2026-07-31T15:15:00"},
                        {"id": 2, "code": "222222", "receivedAt": "2026-07-31T15:16:00"},
                        {"id": 3, "code": "333333", "receivedAt": "2026-07-31T15:17:00"},
                    ],
                },
            },
        })

        code = poll_verification_code(account, max_wait=2, poll_interval=0, session=session)

        self.assertEqual(code, "333333")
        self.assertEqual(account.email, "user+pm01@example.com")
        self.assertIn("id:3", account.seen_codes)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_poll_accepts_newer_code_when_provider_reuses_message_id(self, _sleep):
        session = Mock()
        account = PaymeshMailAccount(
            email="user+pm01@example.com",
            cdk="MAIL-ONE",
            remaining_uses=6,
            seen_codes={"id:7"},
        )
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [
                        {"id": 7, "code": "654321", "receivedAt": "2026-08-01T00:01:00Z"},
                    ],
                },
            },
        })
        after_ts = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()

        code = poll_verification_code(account, max_wait=2, poll_interval=0, session=session, after_ts=after_ts)

        self.assertEqual(code, "654321")

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_one_card_reserves_six_distinct_aliases(self, _config, redeem):
        from core import paymesh_mail_client as client

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            emails = []
            for index in range(6):
                acct = client.pick_account(str(index), [card])
                emails.append(acct.email)
                # A completed registration consumes the slot and releases the per-CDK lock.
                self.assertTrue(client.mark_account_consumed(acct.email))
            with self.assertRaisesRegex(PaymeshMailError, "hết quota"):
                client.pick_account("7", [card])

        self.assertRegex(emails[0], r"^user\+[0-9a-f]{5}@example\.com$")
        self.assertEqual(len(emails), len(set(emails)))
        self.assertTrue(all(email.startswith("user+") for email in emails))
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    @patch("core.paymesh_mail_client.lookup_order")
    @patch("core.paymesh_mail_client.redeem_cdk", side_effect=PaymeshMailError("MAIL card đã hết quota"))
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_known_card_uses_lookup_when_redeem_reports_exhausted(self, _config, _redeem, lookup):
        from core import paymesh_mail_client as client

        card = "SECRET-CARD"
        lookup.return_value = {
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com", "status": "active"},
                    "codes": [],
                },
            },
        }
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            ledger.reserve(
                card,
                ["user+pm01@example.com"],
                "old-job",
                remote_remaining=6,
                configured_limit=6,
            )
            ledger.consume("user+pm01@example.com", "old-job")
            client._LEDGER = ledger

            account = client.pick_account("new-job", [card])

        self.assertRegex(account.email, r"^user\+[0-9a-f]{5}@example\.com$")
        lookup.assert_called_once_with(card, api_base="https://sms.paymesh.cn", timeout=30)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_failed_release_keeps_alias_out_of_rotation(self, _config, redeem):
        from core import paymesh_mail_client as client

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            client._LEDGER = ledger
            first = client.pick_account("failed-job", [card])

            self.assertTrue(client.release_account(first.email, status="failed", note="created upstream"))
            second = client.pick_account("next-job", [card])

            self.assertNotEqual(second.email, first.email)
            self.assertEqual(ledger.find(first.email).status, "failed")

        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    @patch("core.db.list_jobs")
    @patch("core.db.list_accounts")
    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_pick_account_skips_card_when_all_aliases_exist_in_history(
        self, _config, redeem, list_accounts, list_jobs,
    ):
        from core import paymesh_mail_client as client

        used_aliases = client._alias_variants("used@example.com", 6)
        list_accounts.return_value = [{"email": email} for email in used_aliases[:3]]
        list_jobs.return_value = [{"email": email} for email in used_aliases[3:]]
        redeem.side_effect = [
            PaymeshMailAccount("used@example.com", "USED-CARD", 6),
            PaymeshMailAccount("fresh@example.com", "FRESH-CARD", 6),
        ]
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")

                account = client.pick_account("new-job", ["USED-CARD", "FRESH-CARD"])

                self.assertTrue(account.email.startswith("fresh+"))
                self.assertEqual(account.cdk, "FRESH-CARD")
                list_accounts.assert_called_with(limit=100_000, archived="all")
        finally:
            client._CONTEXT_CACHE.clear()
            client._SEEN_CODES_BY_CDK.clear()
            client._CDK_LOCKS.clear()
            client._LEDGER = None

    @patch("core.paymesh_mail_client.lookup_order")
    @patch("core.paymesh_mail_client.redeem_cdk", side_effect=PaymeshMailError("MAIL card đã hết quota"))
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_unknown_exhausted_card_does_not_fallback_to_lookup(self, _config, _redeem, lookup):
        from core import paymesh_mail_client as client

        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            with self.assertRaisesRegex(PaymeshMailError, "hết quota"):
                client.pick_account("new-job", ["UNKNOWN-CARD"])

        lookup.assert_not_called()
        client._LEDGER = None
    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_inventory_pick_acquires_lease_and_reserves_slot(self, _config, redeem):
        from core import paymesh_mail_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("paymesh", card)
            client._INVENTORY_STORE = store

            account = client.pick_account_by_inventory(
                "job-1", [inventory.inventory_id], ttl_seconds=30,
            )

            self.assertRegex(account.email, r"^user\+[0-9a-f]{5}@example\.com$")
            self.assertEqual(account.inventory_id, inventory.inventory_id)
            self.assertTrue(account.reservation_id)
            self.assertTrue(account.owner_token)
            self.assertNotIn(card, repr(account))
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._INVENTORY_STORE = None

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_inventory_lease_prevents_second_concurrent_acquisition(self, _config, redeem):
        from core import paymesh_mail_client as client
        from core.cdk_inventory_store import CdkInventoryStore, CdkInventoryConflict

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("paymesh", card)
            client._INVENTORY_STORE = store

            client.pick_account_by_inventory("job-1", [inventory.inventory_id], ttl_seconds=30)
            with self.assertRaises(PaymeshMailError):
                client.pick_account_by_inventory("job-2", [inventory.inventory_id], ttl_seconds=30)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._INVENTORY_STORE = None

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_inventory_completes_and_skips_used_email_like_old_ledger(self, _config, redeem):
        from core import paymesh_mail_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("paymesh", card)
            client._INVENTORY_STORE = store

            first = client.pick_account_by_inventory("job-1", [inventory.inventory_id])
            self.assertTrue(client.mark_account_consumed(first.email))
            self.assertTrue(client.release_lease(inventory.inventory_id))
            second = client.pick_account_by_inventory("job-2", [inventory.inventory_id])
            self.assertNotEqual(first.email, second.email)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._INVENTORY_STORE = None


if __name__ == "__main__":
    unittest.main()
