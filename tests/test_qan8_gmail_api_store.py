import tempfile
import unittest
from pathlib import Path

from core.qan8_gmail_api_store import Qan8GmailApiStore


class Qan8GmailApiStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = Qan8GmailApiStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_batch_creates_effective_lanes_without_orders(self):
        batch = self.store.create_batch(36, requested_workers=5, aliases_per_source=12)

        self.assertEqual(batch["effective_workers"], 5)
        self.assertEqual([row["lane_id"] for row in self.store.list_lanes(batch["batch_id"])], [0, 1, 2, 3, 4])
        self.assertEqual(self.store.list_orders(batch["batch_id"]), [])

    def test_worker_count_is_clamped_to_target_count(self):
        batch = self.store.create_batch(2, requested_workers=5, aliases_per_source=12)

        self.assertEqual(batch["effective_workers"], 2)
        self.assertEqual(len(self.store.list_lanes(batch["batch_id"])), 2)

    def test_order_intent_is_idempotent_and_lease_is_lane_scoped(self):
        batch = self.store.create_batch(5, requested_workers=2, aliases_per_source=2)
        batch_id = batch["batch_id"]

        self.assertTrue(self.store.acquire_lane_lease(batch_id, 0, "owner-a"))
        self.assertFalse(self.store.acquire_lane_lease(batch_id, 0, "owner-b"))
        self.assertTrue(self.store.acquire_lane_lease(batch_id, 1, "owner-c"))

        first = self.store.create_order_intent(batch_id, 0, "out-1", 156)
        second = self.store.create_order_intent(batch_id, 0, "out-1", 156)

        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(self.store.list_orders(batch_id)), 1)

    def test_source_alias_and_assignment_transitions_are_durable(self):
        batch = self.store.create_batch(2, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        source = self.store.create_source_group(
            batch_id,
            0,
            "user@gmail.com",
            "https://mail.example/source",
            ["u.ser@gmail.com", "user+abcde@gmail.com"],
        )

        assignment = self.store.claim_alias(batch_id, 0, 101)
        self.assertIsNotNone(assignment)
        self.assertIsNone(self.store.claim_alias(batch_id, 0, 102))
        context = self.store.get_account_context("u.ser@gmail.com")
        self.assertEqual(context["source_group_id"], source["source_group_id"])
        self.assertEqual(context["code_url"], "https://mail.example/source")

        self.assertTrue(self.store.complete_assignment(101))
        reopened = Qan8GmailApiStore(self.db_path)
        next_assignment = reopened.claim_alias(batch_id, 0, 102)
        self.assertIsNotNone(next_assignment)

    def test_source_ownership_is_exclusive_and_status_reports_active_count(self):
        batch = self.store.create_batch(4, requested_workers=2, aliases_per_source=2)
        batch_id = batch["batch_id"]
        self.store.create_source_group(
            batch_id, 0, "a@gmail.com", "https://mail.example/a", ["a+one@gmail.com"]
        )
        with self.assertRaises(ValueError):
            self.store.create_source_group(
                batch_id, 1, "b@gmail.com", "https://mail.example/a", ["b+one@gmail.com"]
            )
        with self.assertRaises(ValueError):
            self.store.create_source_group(
                batch_id, 1, "a@gmail.com", "https://mail.example/other", ["c+one@gmail.com"]
            )

        status = self.store.batch_status(batch_id)
        self.assertEqual(status["active_sources"], 1)
        self.assertEqual(status["effective_workers"], 2)


if __name__ == "__main__":
    unittest.main()
