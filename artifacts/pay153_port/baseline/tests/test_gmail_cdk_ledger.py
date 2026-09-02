# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
from pathlib import Path

from core.gmail_aliases import build_gmail_alias_plan
from core.gmail_cdk_ledger import GmailCdkLedger, GmailCdkQuotaError


class GmailCdkLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "gmail-cdk-ledger.json"
        self.ledger = GmailCdkLedger(self.path)
        self.variants = [
            "abcdef@gmail.com",
            "a.bcdef@gmail.com",
            "abcde.f@gmail.com",
            "abcdef+one@gmail.com",
            "abcdef+two@gmail.com",
            "abcdef+three@gmail.com",
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reserve_honors_remote_and_configured_quota(self):
        first = self.ledger.reserve("SECRET-CDK", self.variants, "job-1", remote_remaining=2, configured_limit=6)
        second = self.ledger.reserve("SECRET-CDK", self.variants, "job-2", remote_remaining=2, configured_limit=6)

        self.assertEqual(first.email, self.variants[0])
        self.assertEqual(second.email, self.variants[1])
        with self.assertRaises(GmailCdkQuotaError):
            self.ledger.reserve("SECRET-CDK", self.variants, "job-3", remote_remaining=2, configured_limit=6)

    def test_consume_persists_and_release_returns_only_reserved_slot(self):
        first = self.ledger.reserve("SECRET-CDK", self.variants, "job-1", remote_remaining=6, configured_limit=6)
        second = self.ledger.reserve("SECRET-CDK", self.variants, "job-2", remote_remaining=6, configured_limit=6)
        self.ledger.consume(first.email, "job-1")
        self.ledger.release(second.email, "job-2")

        reloaded = GmailCdkLedger(self.path)
        replacement = reloaded.reserve("SECRET-CDK", self.variants, "job-3", remote_remaining=6, configured_limit=6)

        self.assertEqual(replacement.email, self.variants[1])
        self.assertEqual(reloaded.find(first.email).status, "consumed")

    def test_concurrent_reservations_never_return_the_same_slot(self):
        emails = []
        errors = []
        barrier = threading.Barrier(6)

        def reserve(index):
            try:
                barrier.wait()
                slot = self.ledger.reserve(
                    "SECRET-CDK", self.variants, f"job-{index}", remote_remaining=6, configured_limit=6
                )
                emails.append(slot.email)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(emails)), 6)

    def test_persisted_ledger_never_contains_plaintext_cdk(self):
        self.ledger.reserve("SECRET-CDK", self.variants, "job-1", remote_remaining=6, configured_limit=6)

        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-CDK", raw)
        parsed = json.loads(raw)
        self.assertEqual(parsed["version"], 2)

    def test_reconcile_consumes_saved_account_and_releases_terminal_job(self):
        first = self.ledger.reserve("SECRET-CDK", self.variants, "job-1", remote_remaining=6, configured_limit=6)
        second = self.ledger.reserve("SECRET-CDK", self.variants, "job-2", remote_remaining=6, configured_limit=6)

        changed = self.ledger.reconcile(
            account_exists=lambda email: email == first.email,
            job_is_active=lambda job_id: False,
        )

        self.assertEqual(changed, 2)
        self.assertEqual(self.ledger.find(first.email).status, "consumed")
        self.assertIsNone(self.ledger.find(second.email))

    def test_routed_plan_advances_phase_and_never_returns_to_original(self):
        plan = build_gmail_alias_plan(
            "abcdef@gmail.com",
            limit=3,
            routed_domains=["route-one.net", "route-two.org"],
        )
        slots = [
            self.ledger.reserve_plan("SECRET-CDK", plan, f"job-{index}")
            for index in range(6)
        ]

        self.assertEqual([slot.phase for slot in slots], ["original"] * 3 + ["routed"] * 3)
        self.assertEqual(
            [slot.domain for slot in slots[3:]],
            ["route-one.net", "route-one.net", "route-two.org"],
        )
        self.assertTrue(self.ledger.release(slots[0].email, "job-0"))
        with self.assertRaises(GmailCdkQuotaError):
            self.ledger.reserve_plan("SECRET-CDK", plan, "job-overflow")

    def test_find_exposes_only_cdk_fingerprint_for_context_recovery(self):
        slot = self.ledger.reserve("SECRET-CDK", self.variants, "job-1", remote_remaining=6, configured_limit=6)

        found = self.ledger.find(slot.email)

        self.assertTrue(found.cdk_key.startswith("sha256:"))
        self.assertNotIn("SECRET-CDK", repr(found))



if __name__ == "__main__":
    unittest.main()
