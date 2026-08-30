import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8Order,
    Qan8OrderUnknownError,
)
from core.qan8_gmail_api_store import Qan8GmailApiStore


class _FakeClient:
    def __init__(self):
        self.created = []
        self.lookups = []

    def create_order(self, out_order_no, *, quantity=1):
        self.created.append((out_order_no, quantity))
        number = len(self.created)
        return Qan8Order(
            order_no=out_order_no,
            status="completed",
            delivery=f"source{number}@gmail.com----https://mail.example/source/{number}",
        )

    def get_order(self, order_no):
        self.lookups.append(order_no)
        number = next(index for index, item in enumerate(self.created, 1) if item[0] == order_no)
        return Qan8Order(
            order_no=order_no,
            status="completed",
            delivery=f"source{number}@gmail.com----https://mail.example/source/{number}",
        )

    def parse_delivery(self, delivery):
        email, code_url = str(delivery).split("----", 1)
        from core.qan8_gmail_api_client import Qan8SourceRecord

        return [Qan8SourceRecord(email=email, code_url=code_url)]


class Qan8GmailApiAllocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Qan8GmailApiStore(Path(self.temp_dir.name) / "state.sqlite3")
        self.client = _FakeClient()
        self.allocator = Qan8GmailApiAllocator(
            client=self.client,
            store=self.store,
            poll_interval=0,
            order_timeout=1,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_batch_creation_is_lazy(self):
        batch = self.allocator.create_batch(36, requested_workers=3, aliases_per_source=12)

        self.assertEqual(batch["effective_workers"], 3)
        self.assertEqual(len(self.client.created), 0)
        self.assertEqual(self.store.batch_status(batch["batch_id"])["active_sources"], 0)

    def test_three_lanes_get_distinct_sources(self):
        batch = self.allocator.create_batch(3, requested_workers=3, aliases_per_source=12)
        accounts = [
            self.allocator.acquire_account(batch["batch_id"], f"job-{lane}", lane)
            for lane in range(3)
        ]

        self.assertEqual(len(self.client.created), 3)
        self.assertEqual({account.code_url for account in accounts}, {
            "https://mail.example/source/1",
            "https://mail.example/source/2",
            "https://mail.example/source/3",
        })
        self.assertEqual(self.store.batch_status(batch["batch_id"])["active_sources"], 3)

    def test_thirty_six_jobs_exhaust_three_lanes_before_refill(self):
        batch = self.allocator.create_batch(37, requested_workers=3, aliases_per_source=12)
        batch_id = batch["batch_id"]

        for position in range(36):
            lane = position % 3
            job_id = f"job-{position}"
            self.allocator.acquire_account(batch_id, job_id, lane)
            self.assertTrue(self.allocator.complete_account(batch_id, job_id))

        self.assertEqual(len(self.client.created), 3)
        self.assertEqual(
            [self.store.batch_status(batch_id)["active_sources"], self.store.batch_status(batch_id)["orders_placed"]],
            [0, 3],
        )

        account = self.allocator.acquire_account(batch_id, "job-36", 0)

        self.assertEqual(account.code_url, "https://mail.example/source/4")
        self.assertEqual(len(self.client.created), 4)

    def test_only_exhausted_lane_is_refilled(self):
        batch = self.allocator.create_batch(35, requested_workers=3, aliases_per_source=12)
        batch_id = batch["batch_id"]
        for lane, count in ((0, 12), (1, 11), (2, 11)):
            for ordinal in range(count):
                job_id = f"lane-{lane}-job-{ordinal}"
                self.allocator.acquire_account(batch_id, job_id, lane)
                self.allocator.complete_account(batch_id, job_id)

        before = {
            lane["lane_id"]: lane["current_source_group_id"]
            for lane in self.store.list_lanes(batch_id)
        }
        self.allocator.acquire_account(batch_id, "lane-0-refill", 0)

        after = {
            lane["lane_id"]: lane["current_source_group_id"]
            for lane in self.store.list_lanes(batch_id)
        }
        self.assertNotEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[2], before[2])
        self.assertEqual(len(self.client.created), 4)

    def test_five_workers_map_to_five_lanes_and_sources(self):
        batch = self.allocator.create_batch(5, requested_workers=5, aliases_per_source=12)
        batch_id = batch["batch_id"]
        for lane in range(5):
            self.allocator.acquire_account(batch_id, f"job-{lane}", lane)

        self.assertEqual([lane["lane_id"] for lane in self.store.list_lanes(batch_id)], [0, 1, 2, 3, 4])
        self.assertEqual(self.store.batch_status(batch_id)["active_sources"], 5)
        self.assertEqual(len({row["current_source_group_id"] for row in self.store.list_lanes(batch_id)}), 5)

    def test_aliases_are_local_and_resolve_to_the_source_context(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)
        account = self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        source = self.store.get_current_source(batch["batch_id"], 0)
        aliases = self.store.list_source_aliases(source["source_group_id"])
        context = self.allocator.get_account_context(account.email)

        self.assertEqual(len(aliases), 12)
        self.assertNotIn("source1@gmail.com", [item["alias"] for item in aliases])
        self.assertEqual(context.code_url, account.code_url)

    @patch("core.db.record_gmail_api_url_email")
    def test_purchased_source_is_mirrored_to_gmail_api_url_pool(self, record_email):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        record_email.assert_called_once_with(
            "source1@gmail.com",
            "https://mail.example/source/1",
            status="used",
            note="QAN8 purchased source",
        )

    def test_unknown_order_is_looked_up_before_repeating_purchase(self):
        original_create = self.client.create_order
        first = {"value": True}

        def create_with_unknown(out_order_no, *, quantity=1):
            if first["value"]:
                first["value"] = False
                from core.qan8_gmail_api_client import Qan8OrderUnknownError

                self.client.created.append((out_order_no, quantity))
                raise Qan8OrderUnknownError("unknown out order")
            return original_create(out_order_no, quantity=quantity)

        self.client.create_order = create_with_unknown
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        self.assertTrue(account.email.endswith("@gmail.com"))
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.lookups), 1)

    def test_unknown_order_intent_is_not_reposted_after_lookup_failure(self):
        def create_with_unknown(out_order_no, *, quantity=1):
            self.client.created.append((out_order_no, quantity))
            from core.qan8_gmail_api_client import Qan8OrderUnknownError

            raise Qan8OrderUnknownError("unknown out order")

        def lookup_failed(order_no):
            self.client.lookups.append(order_no)
            raise RuntimeError("lookup unavailable")

        self.client.create_order = create_with_unknown
        self.client.get_order = lookup_failed
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)
        batch_id = batch["batch_id"]

        with self.assertRaises(Qan8OrderUnknownError):
            self.allocator.acquire_account(batch_id, "job-1", 0)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.store.list_orders(batch_id)[0]["status"], "unknown")

        with self.assertRaises(Qan8OrderUnknownError):
            self.allocator.acquire_account(batch_id, "job-2", 0)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(len(self.client.lookups), 2)

    def test_processing_order_timeout_uses_persisted_wall_clock(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)
        batch_id = batch["batch_id"]
        row = self.store.create_order_intent(batch_id, 0, "out-processing", 156)
        self.store.update_order(batch_id, "out-processing", status="processing")
        row = self.store.get_order(batch_id, "out-processing")

        with patch(
            "core.qan8_gmail_api_allocator.time.time",
            return_value=float(row["updated_at"]) + 2,
        ), self.assertRaisesRegex(RuntimeError, "polling timed out"):
            self.allocator._obtain_order(row, deadline=None)

        self.assertGreater(float(row["updated_at"]), time.time() - 10)

    def test_invalid_delivery_is_recorded_before_assignment_can_start(self):
        class InvalidDeliveryClient(_FakeClient):
            def create_order(self, out_order_no, *, quantity=1):
                self.created.append((out_order_no, quantity))
                return Qan8Order(
                    order_no=out_order_no,
                    status="completed",
                    delivery="not-a-gmail-source",
                )

            def parse_delivery(self, delivery):
                raise Qan8DeliveryError("invalid delivery")

        allocator = Qan8GmailApiAllocator(
            client=InvalidDeliveryClient(),
            store=self.store,
            poll_interval=0,
            order_timeout=1,
        )
        batch = allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        with self.assertRaisesRegex(RuntimeError, "delivery rejected"):
            allocator.acquire_account(batch["batch_id"], "job-1", 0)
        self.assertEqual(
            self.store.list_orders(batch["batch_id"])[0]["status"],
            "delivery_unparsed",
        )


if __name__ == "__main__":
    unittest.main()
