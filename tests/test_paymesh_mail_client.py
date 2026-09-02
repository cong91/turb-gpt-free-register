import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.paymesh_mail_client import (
    PaymeshMailAccount,
    PaymeshMailError,
    fetch_latest_otp,
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
    def setUp(self):
        from core import paymesh_mail_client as client
        from core.otp_identity_store import OtpIdentityStore

        self._otp_temp_dir = tempfile.TemporaryDirectory()
        self._previous_otp_store = client._OTP_STORE
        client._OTP_STORE = OtpIdentityStore(
            Path(self._otp_temp_dir.name) / "otp.sqlite3"
        )

    @patch("core.paymesh_mail_client.poll_verification_code", return_value="654321")
    @patch("core.paymesh_mail_client.get_account_context")
    def test_fetch_latest_otp_uses_paymesh_default_wait(self, get_context, poll):
        from config import email as email_config

        get_context.return_value = PaymeshMailAccount("user@example.com", "MAIL-ONE", 6)
        with patch.object(email_config, "PAYMESH_OTP_MAX_WAIT", 180):
            self.assertEqual(fetch_latest_otp("user@example.com"), "654321")

        self.assertEqual(poll.call_args.kwargs["max_wait"], 180)

    @patch("core.paymesh_mail_client.poll_verification_code", return_value="654321")
    @patch("core.paymesh_mail_client.get_account_context")
    def test_fetch_latest_otp_explicit_wait_wins(self, get_context, poll):
        get_context.return_value = PaymeshMailAccount("user@example.com", "MAIL-ONE", 6)

        self.assertEqual(fetch_latest_otp("user@example.com", max_wait=12), "654321")

        self.assertEqual(poll.call_args.kwargs["max_wait"], 12)

    def tearDown(self):
        from core import paymesh_mail_client as client

        client._OTP_STORE = self._previous_otp_store
        self._otp_temp_dir.cleanup()

    def test_redeem_uses_paymesh_contract_without_exposing_card(self):
        session = Mock()
        session.post.return_value = _Response({
            "code": 0,
            "data": {
                "emailAddress": "User@example.com",
                "endTime": "2026-08-01T00:00:00Z",
            },
        })
        session.get.return_value = _Response({
            "code": 0,
            "data": {"emailAddress": "User@example.com", "codes": []},
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

    def test_redeem_without_codes_looks_up_existing_otp_array(self):
        session = Mock()
        session.post.return_value = _Response({
            "code": 0,
            "data": {"emailAddress": "user@example.com"},
        })
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [{"id": 7, "code": "654321"}],
                },
            },
        })

        account = redeem_cdk("SECRET-CARD", session=session)

        self.assertIn("id:7:654321", account.seen_codes)
        self.assertEqual(session.get.call_count, 1)

    def test_redeemed_card_code_2002_uses_lookup_even_when_lookup_code_is_2002(self):
        session = Mock()
        session.post.return_value = _Response({"code": 2002, "msg": "card already used"})
        session.get.return_value = _Response({
            "code": 2002,
            "msg": "card already used",
            "data": {
                "email": {
                    "session": {
                        "emailAddress": "user@example.com",
                        "status": "active",
                    },
                    "codes": [],
                },
            },
        })

        account = redeem_cdk("SECRET-CARD", session=session)

        self.assertEqual(account.email, "user@example.com")
        session.get.assert_called_once_with(
            "https://sms.paymesh.cn/api/v1/order/lookup?code=SECRET-CARD&poll=true",
            headers={"Accept": "application/json"},
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
            seen_codes={"id:1:111111"},
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
        self.assertIn("id:3:333333", account.seen_codes)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_poll_rejects_claimed_code_when_provider_reuses_message_id(self, _sleep):
        session = Mock()
        account = PaymeshMailAccount(
            email="user+pm01@example.com",
            cdk="MAIL-ONE",
            remaining_uses=6,
            seen_codes={"id:7:654321"},
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

        with self.assertRaises(PaymeshMailError):
            poll_verification_code(
                account,
                max_wait=2,
                poll_interval=0,
                session=session,
                after_ts=after_ts,
            )

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_reused_message_id_allows_a_different_otp_value(self, _sleep):
        session = Mock()
        session.get.side_effect = [
            _Response({
                "code": 0,
                "data": {
                    "email": {
                        "session": {"emailAddress": "user@example.com"},
                        "codes": [{"id": 7, "code": "111111"}],
                    },
                },
            }),
            _Response({
                "code": 0,
                "data": {
                    "email": {
                        "session": {"emailAddress": "user@example.com"},
                        "codes": [{"id": 7, "code": "222222"}],
                    },
                },
            }),
        ]
        first = PaymeshMailAccount("first@example.com", "MAIL-ONE", 6)
        second = PaymeshMailAccount("second@example.com", "MAIL-ONE", 6)

        self.assertEqual(
            poll_verification_code(first, max_wait=1, poll_interval=0, session=session),
            "111111",
        )
        self.assertEqual(
            poll_verification_code(second, max_wait=1, poll_interval=0, session=session),
            "222222",
        )

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_same_cdk_does_not_reuse_claimed_message_identity(self, _sleep):
        session = Mock()
        first = PaymeshMailAccount(
            email="user+pm01@example.com",
            cdk="MAIL-ONE",
            remaining_uses=6,
            seen_codes=set(),
        )
        second = PaymeshMailAccount(
            email="user+pm02@example.com",
            cdk="MAIL-ONE",
            remaining_uses=6,
            seen_codes=first.seen_codes,
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

        self.assertEqual(
            poll_verification_code(first, max_wait=1, poll_interval=0, session=session, after_ts=after_ts),
            "654321",
        )
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(second, max_wait=1, poll_interval=0, session=session, after_ts=after_ts)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_claim_survives_empty_memory_context_after_restart(self, _sleep):
        from core import paymesh_mail_client as client
        from core.otp_identity_store import OtpIdentityStore

        session = Mock()
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [{"id": 7, "code": "654321"}],
                },
            },
        })
        first = PaymeshMailAccount("first@example.com", "MAIL-ONE", 6)
        self.assertEqual(
            poll_verification_code(first, max_wait=1, poll_interval=0, session=session),
            "654321",
        )

        path = client._OTP_STORE.path
        client._OTP_STORE = OtpIdentityStore(path)
        restarted = PaymeshMailAccount("second@example.com", "MAIL-ONE", 6)
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(restarted, max_wait=1, poll_interval=0, session=session)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_old_code_filtered_by_after_ts_is_persisted_as_baseline(self, _sleep):
        from core import paymesh_mail_client as client
        from core.otp_identity_store import OtpIdentityStore

        session = Mock()
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [
                        {"id": 7, "code": "654321", "receivedAt": "2026-08-01T00:00:00Z"},
                    ],
                },
            },
        })
        after_ts = datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc).timestamp()
        first = PaymeshMailAccount("first@example.com", "MAIL-ONE", 6)
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(
                first, max_wait=1, poll_interval=0, session=session, after_ts=after_ts
            )

        path = client._OTP_STORE.path
        client._OTP_STORE = OtpIdentityStore(path)
        restarted = PaymeshMailAccount("second@example.com", "MAIL-ONE", 6)
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(restarted, max_wait=1, poll_interval=0, session=session)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_claim_baselines_full_paymesh_otp_array(self, _sleep):
        session = Mock()
        session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [
                        {"id": 1, "code": "111111"},
                        {"id": 2, "code": "222222"},
                    ],
                },
            },
        })
        first = PaymeshMailAccount("first@example.com", "MAIL-ONE", 6)
        second = PaymeshMailAccount("second@example.com", "MAIL-ONE", 6)

        self.assertEqual(
            poll_verification_code(first, max_wait=1, poll_interval=0, session=session),
            "222222",
        )
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(second, max_wait=1, poll_interval=0, session=session)

    @patch("core.paymesh_mail_client.time.sleep", return_value=None)
    def test_redeem_baseline_blocks_same_otp_under_new_message_id(self, _sleep):
        redeem_session = Mock()
        redeem_session.post.return_value = _Response({
            "code": 0,
            "data": {
                "emailAddress": "user@example.com",
                "codes": [{"id": 7, "code": "654321"}],
            },
        })
        redeem_cdk("MAIL-ONE", session=redeem_session)

        poll_session = Mock()
        poll_session.get.return_value = _Response({
            "code": 0,
            "data": {
                "email": {
                    "session": {"emailAddress": "user@example.com"},
                    "codes": [{"id": 8, "code": "654321"}],
                },
            },
        })
        restarted = PaymeshMailAccount("second@example.com", "MAIL-ONE", 6)
        with self.assertRaises(PaymeshMailError):
            poll_verification_code(
                restarted,
                max_wait=1,
                poll_interval=0,
                session=poll_session,
            )

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

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_terms_rejection_blocks_card_before_next_alias(self, _config, redeem):
        from core import paymesh_mail_client as client

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            account = client.pick_account("terms-job", [card])
            self.assertTrue(client.block_account_card(account.email, "terms_rejected"))
            self.assertTrue(client.release_account(account.email, status="failed"))
            with self.assertRaisesRegex(PaymeshMailError, "bị chặn"):
                client.pick_account("next-job", [card])
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
    @patch("core.paymesh_mail_client.pick_account")
    def test_assigned_inventory_resolves_only_its_card(self, pick_account):
        from core import paymesh_mail_client as client
        from core.cdk_inventory_store import CdkInventoryStore

        pick_account.return_value = PaymeshMailAccount("user@example.com", "CARD-4", 6)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CdkInventoryStore(Path(temp_dir) / "cdk.sqlite3")
            inventory, _ = store.import_cdk("paymesh", "CARD-4", configured_limit=3)
            client._INVENTORY_STORE = store

            account = client.pick_account_for_inventory(
                "job-4",
                inventory.inventory_id,
                routed_domains=["test.com"],
            )

        self.assertEqual(account.email, "user@example.com")
        pick_account.assert_called_once_with(
            "job-4", ["CARD-4"], routed_domains=["test.com"]
        )
        client._INVENTORY_STORE = None

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
        from core.cdk_inventory_store import CdkInventoryStore

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

    @patch("core.paymesh_mail_client.redeem_cdk")
    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_pick_account_with_routed_domain_yields_extra_aliases(self, _config, redeem):
        from core import paymesh_mail_client as client

        card = "SECRET-CARD"
        redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
            emails = []
            for index in range(12):
                acct = client.pick_account(
                    str(index), [card], routed_domains=["test.com"]
                )
                emails.append(acct.email)
                self.assertTrue(client.mark_account_consumed(acct.email))
            with self.assertRaisesRegex(PaymeshMailError, "hết quota"):
                client.pick_account("13", [card], routed_domains=["test.com"])

        domains = {email.rsplit("@", 1)[1] for email in emails}
        self.assertEqual(domains, {"example.com", "test.com"})
        self.assertEqual(len(emails), len(set(emails)))
        self.assertEqual(sum(1 for e in emails if e.endswith("@example.com")), 6)
        self.assertEqual(sum(1 for e in emails if e.endswith("@test.com")), 6)
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    @patch("core.paymesh_mail_client._config_values", return_value=("https://sms.paymesh.cn", 30, 6))
    def test_pick_account_without_routed_keeps_six_quota(self, _config):
        # Sanity: routed_domains=() phải giữ behavior cũ (cap=6).
        from core import paymesh_mail_client as client

        with patch("core.paymesh_mail_client.redeem_cdk") as redeem:
            card = "SECRET-CARD"
            redeem.return_value = PaymeshMailAccount("user@example.com", card, 6)
            client._CONTEXT_CACHE.clear()
            client._SEEN_CODES_BY_CDK.clear()
            client._CDK_LOCKS.clear()
            with tempfile.TemporaryDirectory() as temp_dir:
                client._LEDGER = ProviderCardLedger(Path(temp_dir) / "ledger.json", "paymesh")
                for index in range(6):
                    acct = client.pick_account(str(index), [card])
                    self.assertTrue(acct.email.endswith("@example.com"))
                    self.assertTrue(client.mark_account_consumed(acct.email))
                with self.assertRaisesRegex(PaymeshMailError, "hết quota"):
                    client.pick_account("7", [card])
            client._CONTEXT_CACHE.clear()
            client._SEEN_CODES_BY_CDK.clear()
            client._CDK_LOCKS.clear()
            client._LEDGER = None


if __name__ == "__main__":
    unittest.main()
