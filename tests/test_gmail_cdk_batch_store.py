import tempfile
import unittest
from pathlib import Path

from core.gmail_cdk_batch_store import (
    GmailCdkBatchStore,
)


class GmailCdkBatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = GmailCdkBatchStore(Path(self.temp_dir.name) / "gmail-cdk-batches.sqlite3")
        self.batch_id = self.store.create_batch(
            ["inventory-1", "inventory-2", "inventory-3", "inventory-4", "inventory-5"],
            capacity=6,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_claims_initial_workers_in_input_order_and_allows_retry_after_fail(self):
        first = self.store.claim(self.batch_id, "job-1")
        second = self.store.claim(self.batch_id, "job-2")
        third = self.store.claim(self.batch_id, "job-3")

        self.assertEqual(
            [first.inventory_id, second.inventory_id, third.inventory_id],
            ["inventory-1", "inventory-2", "inventory-3"],
        )

        # fail() frees the assignment but keeps item active for retry
        self.store.fail(second.assignment_id, reason="redeem failed")

        # Next claim reuses inventory-2 (round-robin with least usage)
        fourth = self.store.claim(self.batch_id, "job-4")
        self.assertEqual(fourth.inventory_id, "inventory-2")

        # Next claim gets inventory-4
        fifth = self.store.claim(self.batch_id, "job-5")
        self.assertEqual(fifth.inventory_id, "inventory-4")

    def test_release_keeps_cdk_active_and_complete_exhausts_capacity(self):
        assignment = self.store.claim(self.batch_id, "job-1")
        self.store.release(assignment.assignment_id)
        replacement = self.store.claim(self.batch_id, "job-2")
        self.assertEqual(replacement.inventory_id, "inventory-1")

        self.store.complete(replacement.assignment_id)
        second = self.store.claim(self.batch_id, "job-3")
        self.assertEqual(second.inventory_id, "inventory-1")
        self.store.complete(second.assignment_id)
        self.assertEqual(self.store.get_assignment(replacement.assignment_id).state, "completed")

    def test_state_survives_reopen(self):
        assignment = self.store.claim(self.batch_id, "job-1")
        self.store.fail(assignment.assignment_id, reason="provider rejected")

        reopened = GmailCdkBatchStore(self.store.path)
        next_assignment = reopened.claim(self.batch_id, "job-2")

        # After fail(), item stays active for retry, so next claim reuses inventory-1
        self.assertEqual(next_assignment.inventory_id, "inventory-1")
        self.assertEqual(reopened.get_item(self.batch_id, "inventory-1").state, "active")


if __name__ == "__main__":
    unittest.main()
