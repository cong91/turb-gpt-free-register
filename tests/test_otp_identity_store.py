# -*- coding: utf-8 -*-
import tempfile
import threading
import unittest
from pathlib import Path

from core.otp_identity_store import OtpIdentityStore


class OtpIdentityStoreTests(unittest.TestCase):
    def test_duplicate_claim_is_rejected_after_reopen_without_plaintext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "otp.sqlite3"
            first = OtpIdentityStore(path)
            fingerprint = first.fingerprint("paymesh", " SECRET-CDK ")

            self.assertTrue(first.claim_if_unseen("paymesh", fingerprint, "id:7"))
            self.assertFalse(first.claim_if_unseen("paymesh", fingerprint, "id:7"))

            reopened = OtpIdentityStore(path)
            self.assertFalse(reopened.claim_if_unseen("paymesh", fingerprint, "id:7"))
            self.assertTrue(reopened.claim_if_unseen("paymesh", fingerprint, "id:8"))

            raw = path.read_bytes()
            self.assertNotIn(b"SECRET-CDK", raw)
            self.assertNotIn(b"id:7", raw)

    def test_provider_and_cdk_fingerprints_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OtpIdentityStore(Path(temp_dir) / "otp.sqlite3")
            paymesh = store.fingerprint("paymesh", "SAME-CDK")
            gmail = store.fingerprint("gmail_123452026", "SAME-CDK")
            other = store.fingerprint("paymesh", "OTHER-CDK")

            self.assertNotEqual(paymesh, gmail)
            self.assertNotEqual(paymesh, other)
            self.assertTrue(store.claim_if_unseen("paymesh", paymesh, "code:123456"))
            self.assertTrue(store.claim_if_unseen("gmail_123452026", gmail, "code:123456"))
            self.assertTrue(store.claim_if_unseen("paymesh", other, "code:123456"))

    def test_claim_with_snapshot_baselines_other_observed_identities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OtpIdentityStore(Path(temp_dir) / "otp.sqlite3")
            fingerprint = store.fingerprint("paymesh", "MAIL-ONE")

            self.assertTrue(store.claim_with_snapshot(
                "paymesh",
                fingerprint,
                claim_identities=["event:id:8", "value:222222"],
                observed_identities=[
                    "event:id:7", "value:111111",
                    "event:id:8", "value:222222",
                ],
            ))
            self.assertFalse(store.claim_if_unseen(
                "paymesh", fingerprint, "event:id:7"
            ))
            self.assertFalse(store.claim_with_snapshot(
                "paymesh",
                fingerprint,
                claim_identities=["event:id:8", "value:222222"],
                observed_identities=["event:id:8", "value:222222"],
            ))

    def test_remember_many_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OtpIdentityStore(Path(temp_dir) / "otp.sqlite3")
            fingerprint = store.fingerprint("paymesh", "MAIL-ONE")

            self.assertEqual(store.remember_many(
                "paymesh", fingerprint, ["id:1:111111", "value:111111"]
            ), 2)
            self.assertEqual(store.remember_many(
                "paymesh", fingerprint, ["id:1:111111", "value:111111"]
            ), 0)

    def test_concurrent_claim_has_one_winner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "otp.sqlite3"
            fingerprint = OtpIdentityStore.fingerprint("paymesh", "MAIL-ONE")
            barrier = threading.Barrier(2)
            results: list[bool] = []

            def claim() -> None:
                store = OtpIdentityStore(path)
                barrier.wait()
                results.append(store.claim_if_unseen(
                    "paymesh", fingerprint, "value:654321"
                ))

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
