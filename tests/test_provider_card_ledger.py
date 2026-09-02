import tempfile
import unittest
from pathlib import Path

from core.provider_card_ledger import ProviderCardLedger, ProviderCardQuotaError


class ProviderCardLedgerTests(unittest.TestCase):
    def test_ledger_hashes_raw_card_and_persists_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.json"
            ledger = ProviderCardLedger(path, provider_name="paymesh")
            card = "MAIL-SECRET-CARD"

            slot = ledger.reserve(
                card,
                ["user+pm01@example.com"],
                "17",
                remote_remaining=6,
                configured_limit=6,
            )

            self.assertEqual(slot.email, "user+pm01@example.com")
            self.assertNotIn(card, path.read_text(encoding="utf-8"))
            self.assertTrue(ledger.consume(slot.email, "17"))
            self.assertEqual(ledger.find(slot.email).status, "consumed")

    def test_card_quota_raises_after_all_variants_are_reserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", provider_name="paymesh")
            ledger.reserve(
                "MAIL-ONE",
                ["user+pm01@example.com"],
                "1",
                remote_remaining=1,
                configured_limit=1,
            )

            with self.assertRaisesRegex(ProviderCardQuotaError, "hết quota"):
                ledger.reserve(
                    "MAIL-ONE",
                    ["user+pm01@example.com"],
                    "2",
                    remote_remaining=1,
                    configured_limit=1,
                )

    def test_filtered_variant_list_still_respects_existing_card_capacity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", provider_name="paymesh")
            first = ledger.reserve(
                "MAIL-ONE",
                ["user+one@example.com"],
                "1",
                remote_remaining=6,
                configured_limit=6,
            )
            self.assertTrue(ledger.consume(first.email, "1"))

            second = ledger.reserve(
                "MAIL-ONE",
                ["user+two@example.com"],
                "2",
                remote_remaining=6,
                configured_limit=6,
            )

            self.assertEqual(second.email, "user+two@example.com")

    def test_blocked_card_rejects_future_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", provider_name="paymesh")
            first = ledger.reserve(
                "MAIL-BLOCKED",
                ["user+one@example.com"],
                "1",
                remote_remaining=6,
                configured_limit=6,
            )
            self.assertTrue(ledger.block_card(first.email, "1", "terms_rejected"))

            with self.assertRaisesRegex(ProviderCardQuotaError, "bị chặn"):
                ledger.reserve(
                    "MAIL-BLOCKED",
                    ["user+two@example.com"],
                    "2",
                    remote_remaining=6,
                    configured_limit=6,
                )

    def test_reserve_skips_alias_already_used_by_another_card(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProviderCardLedger(Path(temp_dir) / "ledger.json", provider_name="paymesh")
            first = ledger.reserve(
                "MAIL-ONE",
                ["user+one@example.com"],
                "1",
                remote_remaining=6,
                configured_limit=6,
            )
            self.assertTrue(ledger.consume(first.email, "1"))

            second = ledger.reserve(
                "MAIL-TWO",
                ["user+one@example.com", "user+two@example.com"],
                "2",
                remote_remaining=6,
                configured_limit=6,
            )

            self.assertEqual(second.email, "user+two@example.com")


if __name__ == "__main__":
    unittest.main()
