import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import call, patch

from core.gmail_aliases import generate_gmail_dual_domain_aliases
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8GmailApiError,
    Qan8Order,
    Qan8OrderUnknownError,
)
from core.qan8_gmail_api_store import Qan8GmailApiStore


class _FakeClient:
    def __init__(self):
        self.created = []
        self.lookups = []
        self.proxy_url = ""
        self.seen_proxy_urls = []

    def create_order(self, out_order_no, *, quantity=1):
        self.created.append((out_order_no, quantity))
        self.seen_proxy_urls.append(self.proxy_url)
        number = len(self.created)
        return Qan8Order(
            order_no=out_order_no,
            status="completed",
            delivery=f"source{number}@gmail.com----https://mail.example/source/{number}",
        )

    def get_order(self, order_no):
        self.lookups.append(order_no)
        self.seen_proxy_urls.append(self.proxy_url)
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
        self.record_source_patcher = patch("core.db.record_gmail_api_url_email")
        self.record_source = self.record_source_patcher.start()
        self.addCleanup(self.record_source_patcher.stop)
        self.source_failed_patcher = patch(
            "core.db.is_gmail_api_url_code_url_failed", return_value=False
        )
        self.source_failed = self.source_failed_patcher.start()
        self.addCleanup(self.source_failed_patcher.stop)
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

    def test_provision_lease_covers_long_provider_timeout(self):
        allocator = Qan8GmailApiAllocator(
            client=self.client,
            store=self.store,
            poll_interval=0,
            order_timeout=600,
        )

        self.assertEqual(allocator._provision_lease_seconds(), 660)

    def test_provider_rejection_terminalizes_pending_order(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=1)
        order = self.store.create_order_intent(batch["batch_id"], 0, "out-quota", 156)

        def reject(*_args, **_kwargs):
            raise Qan8GmailApiError("insufficient balance")

        self.client.create_order = reject

        with self.assertRaisesRegex(Qan8GmailApiError, "insufficient balance"):
            self.allocator._obtain_order(order, None)

        self.assertEqual(
            self.store.get_order(batch["batch_id"], "out-quota")["status"],
            "failed",
        )

    def test_busy_lane_honors_stop_check(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=12)
        batch_id = batch["batch_id"]
        self.allocator.acquire_account(batch_id, "owner", 0)

        stopped = []

        def stop_check():
            stopped.append(True)
            raise RuntimeError("stop requested")

        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            self.allocator.acquire_account(batch_id, "waiting", 0, stop_check=stop_check)
        self.assertEqual(stopped, [True])

    def test_stop_after_lane_lease_skips_paid_purchase(self):
        self.client.proxy_url = "http://proxy.example:8080"
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)
        acquired = False
        original_acquire = self.store.acquire_lane_lease

        def acquire_lane_lease(*args, **kwargs):
            nonlocal acquired
            result = original_acquire(*args, **kwargs)
            acquired = bool(result)
            return result

        def stop_check():
            if acquired:
                raise RuntimeError("stop requested after lease")

        with patch.object(
            self.store,
            "acquire_lane_lease",
            side_effect=acquire_lane_lease,
        ), self.assertRaisesRegex(RuntimeError, "stop requested after lease"):
            self.allocator._purchase_source(
                batch,
                0,
                None,
                stop_check=stop_check,
            )

        self.assertEqual(self.client.created, [])
        self.assertEqual(self.store.list_orders(batch["batch_id"]), [])

    def test_stop_after_purchase_does_not_claim_materialized_alias(self):
        self.client.proxy_url = "http://proxy.example:8080"
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=1)

        def stop_check():
            if self.client.created:
                raise RuntimeError("stop requested after purchase")

        with self.assertRaisesRegex(RuntimeError, "stop requested after purchase"):
            self.allocator.acquire_account(
                batch["batch_id"],
                "stopped-job",
                0,
                stop_check=stop_check,
            )

        self.assertEqual(len(self.client.created), 1)
        self.assertIsNone(self.store.get_assignment("stopped-job"))
        self.assertIsNotNone(self.store.get_current_source(batch["batch_id"], 0))

    def test_busy_lane_waits_after_order_timeout_by_default(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=12)
        batch_id = batch["batch_id"]
        self.allocator.acquire_account(batch_id, "owner", 0)

        clock = [0.0]
        sleeps = []

        def monotonic():
            return clock[0]

        def sleep(_seconds):
            sleeps.append(True)
            clock[0] += 2.0
            if len(sleeps) == 2:
                self.assertTrue(self.allocator.complete_account(batch_id, "owner"))

        with patch("core.qan8_gmail_api_allocator.time.monotonic", side_effect=monotonic), patch(
            "core.qan8_gmail_api_allocator.time.sleep", side_effect=sleep
        ):
            account = self.allocator.acquire_account(batch_id, "waiting", 0)

        self.assertEqual(account.job_id, "waiting")
        self.assertEqual(len(sleeps), 2)
        self.assertGreater(clock[0], self.allocator.order_timeout)

    def test_busy_lane_honors_explicit_wait_timeout(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=12)
        batch_id = batch["batch_id"]
        self.allocator.acquire_account(batch_id, "owner", 0)

        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(_seconds):
            clock[0] += 2.0

        with patch("core.qan8_gmail_api_allocator.time.monotonic", side_effect=monotonic), patch(
            "core.qan8_gmail_api_allocator.time.sleep", side_effect=sleep
        ), self.assertRaisesRegex(RuntimeError, "QAN8 lane is busy"):
            self.allocator.acquire_account(batch_id, "waiting", 0, wait_timeout=1)

    def test_purchase_uses_one_nordvpn_route_for_the_full_cycle(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        @contextmanager
        def proxy_context(*, owner_id):
            self.assertTrue(owner_id.startswith("qan8-api:"))
            yield "socks5://127.0.0.1:25000"

        with patch("core.nordvpn_wireguard.proxy_for_qan8_api", side_effect=proxy_context), \
             patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True):
            self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        self.assertEqual(self.client.seen_proxy_urls, ["socks5://127.0.0.1:25000"])
        self.assertEqual(self.client.proxy_url, "")

    def test_explicit_qan8_proxy_skips_nordvpn_route(self):
        self.client.proxy_url = "http://proxy.example:8080"
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        with patch("core.nordvpn_wireguard.proxy_for_qan8_api") as wireguard_proxy:
            self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        wireguard_proxy.assert_not_called()
        self.assertEqual(self.client.seen_proxy_urls, ["http://proxy.example:8080"])

    def test_three_lanes_get_distinct_sources(self):
        # Three lanes are justified only when the target needs three full
        # sources; a three-job target must stay on one lazy lane.
        batch = self.allocator.create_batch(36, requested_workers=3, aliases_per_source=12)
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

    def test_small_batch_uses_one_source_and_reuses_alias_capacity(self):
        batch = self.allocator.create_batch(6, requested_workers=3, aliases_per_source=12)
        batch_id = batch["batch_id"]
        self.assertEqual(batch["effective_workers"], 1)

        accounts = []
        for ordinal in range(2):
            job_id = f"job-{ordinal}"
            account = self.allocator.acquire_account(batch_id, job_id, 0)
            accounts.append(account)
            source = self.store.get_current_source(batch_id, 0)
            self.assertIsNotNone(source)
            aliases = self.store.list_source_aliases(source["source_group_id"])
            self.assertEqual(account.email, aliases[ordinal]["alias"])
            self.assertTrue(self.allocator.complete_account(batch_id, job_id))

        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(accounts[0].code_url, accounts[1].code_url)
        self.assertNotEqual(accounts[0].email, accounts[1].email)

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
        batch = self.allocator.create_batch(60, requested_workers=5, aliases_per_source=12)
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

    def test_failed_aliases_exhaust_source_and_trigger_next_source(self):
        batch = self.allocator.create_batch(3, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]

        first = self.allocator.acquire_account(batch_id, "job-failed-1", 0)
        self.assertTrue(
            self.allocator.release_account(
                first.email,
                status="failed",
                reason="registration blocked",
            )
        )
        second = self.allocator.acquire_account(batch_id, "job-failed-2", 0)
        self.assertNotEqual(first.email, second.email)
        self.assertTrue(
            self.allocator.release_account(
                second.email,
                status="failed",
                reason="registration blocked",
            )
        )

        third = self.allocator.acquire_account(batch_id, "job-next-source", 0)

        self.assertEqual(third.code_url, "https://mail.example/source/2")
        self.assertEqual(len(self.client.created), 2)

    def test_fail_account_discards_only_the_failed_alias(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]

        failed = self.allocator.acquire_account(batch_id, "job-failed", 0)
        self.assertTrue(
            self.allocator.fail_account(batch_id, "job-failed", reason="registration blocked")
        )

        next_account = self.allocator.acquire_account(batch_id, "job-next", 0)

        self.assertNotEqual(next_account.email, failed.email)
        self.assertEqual(next_account.code_url, failed.code_url)
        self.assertEqual(len(self.client.created), 1)

    def test_provider_602_retires_source_and_purchases_replacement(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        failed = self.allocator.acquire_account(batch_id, "job-broken", 0)

        self.assertTrue(
            self.allocator.release_account(
                failed.email,
                status="available",
                reason="Provider error code=602",
            )
        )
        context = self.store.get_account_context(failed.email)
        self.assertIsNotNone(context)
        self.assertEqual(context["source_state"], "retired")
        self.assertEqual(self.store.get_lane(batch_id, 0)["state"], "active")
        self.assertEqual(
            {row["state"] for row in self.store.list_source_aliases(context["source_group_id"])},
            {"failed"},
        )

        replacement = self.allocator.acquire_account(batch_id, "job-after-602", 0)
        self.assertEqual(replacement.code_url, "https://mail.example/source/2")
        self.assertEqual(len(self.client.created), 2)

    def test_provider_602_message_variants_retire_source(self):
        for index, reason in enumerate(
            ("Provider error code 602", "Provider status: 602"),
            start=1,
        ):
            with self.subTest(reason=reason):
                batch = self.allocator.create_batch(
                    1,
                    requested_workers=1,
                    aliases_per_source=1,
                )
                account = self.allocator.acquire_account(
                    batch["batch_id"],
                    f"job-602-variant-{index}",
                    0,
                )

                self.assertTrue(
                    self.allocator.release_account(
                        account.email,
                        status="available",
                        reason=reason,
                    )
                )
                context = self.store.get_account_context(account.email)
                self.assertIsNotNone(context)
                self.assertEqual(context["source_state"], "retired")
                self.assertEqual(
                    {
                        row["state"]
                        for row in self.store.list_source_aliases(
                            context["source_group_id"]
                        )
                    },
                    {"failed"},
                )

    def test_stale_source_without_claimable_aliases_is_replaced(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=1)
        batch_id = batch["batch_id"]
        stale = self.store.create_source_group(
            batch_id,
            0,
            "stale@gmail.com",
            "https://mail.example/stale",
            ["stale+one@gmail.com"],
        )
        with self.store._transaction() as connection:
            connection.execute(
                "UPDATE qan8_aliases SET state = 'failed' WHERE source_group_id = ?",
                (stale["source_group_id"],),
            )

        replacement = self.allocator.acquire_account(
            batch_id,
            "job-after-stale-source",
            0,
            wait_timeout=0,
        )

        self.assertEqual(replacement.code_url, "https://mail.example/source/1")
        self.assertEqual(
            self.store.get_source_group(stale["source_group_id"])["state"],
            "exhausted",
        )

    def test_purchased_source_is_mirrored_to_gmail_api_url_pool(self):
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-1", 0)

        self.record_source.assert_called_once_with(
            "source1@gmail.com",
            "https://mail.example/source/1",
            status="used",
            note="QAN8 purchased source",
            sqlite_path=self.store.path,
        )
        q8_assignment = self.store.get_assignment("job-1")
        self.assertIsNotNone(q8_assignment)
        gmail_assignment = self.allocator.gmail_store.get_assignment(q8_assignment["assignment_id"])
        self.assertIsNotNone(gmail_assignment)
        self.assertEqual(gmail_assignment.inventory_id, f"{account.email}----{account.code_url}")
        self.assertTrue(self.allocator.complete_account(batch["batch_id"], "job-1"))
        self.assertEqual(self.allocator.gmail_store.get_assignment(q8_assignment["assignment_id"]).state, "completed")

    def test_qan8_reuses_existing_available_gmail_alias_items(self):
        aliases = generate_gmail_dual_domain_aliases("source1@gmail.com", limit=12)
        gmail_batch = self.allocator.gmail_store.create_batch_multi([{
            "source_email": "source1@gmail.com",
            "code_url": "https://mail.example/source/1",
            "aliases": aliases,
        }])
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-reuse", 0)

        source = self.store.get_current_source(batch["batch_id"], 0)
        refs = {
            row["gmail_batch_id"]
            for row in self.store.list_source_aliases(source["source_group_id"])
        }
        self.assertEqual(refs, {gmail_batch})
        self.assertEqual(account.code_url, "https://mail.example/source/1")
        self.assertEqual(self.allocator.gmail_store.batch_status(gmail_batch)["active_assignments"], 1)

    def test_legacy_capacity_rows_are_deduplicated_before_qan8_link(self):
        gmail_store = GmailApiUrlBatchStore(self.store.path)
        gmail_store.create_batch(
            [("legacy-source@gmail.com", "https://mail.example/legacy")],
            capacity=3,
        )
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-legacy", 0)

        self.assertEqual(account.email, "legacy-source@gmail.com")
        self.assertEqual(account.code_url, "https://mail.example/legacy")
        self.assertEqual(len(self.client.created), 0)
        source = self.store.get_current_source(batch["batch_id"], 0)
        self.assertEqual(
            len(self.store.list_source_aliases(source["source_group_id"])),
            1,
        )

    def test_canonical_replenishment_stops_after_repeated_no_progress(self):
        """Terminal source outcomes cannot trigger unbounded paid orders."""
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)
        canonical_batch = self.allocator.gmail_store.create_empty_batch()
        orders: list[dict] = []

        def purchase_without_alias(*_args, **_kwargs):
            orders.append({"status": "source_failed", "sequence": len(orders)})

        with patch.object(
            self.allocator,
            "_materialize_raw_gmail_source",
            return_value=False,
        ), patch.object(
            self.allocator,
            "_purchase_source",
            side_effect=purchase_without_alias,
        ), patch.object(
            self.allocator.store,
            "list_orders",
            side_effect=lambda _batch_id: list(orders),
        ), self.assertRaisesRegex(RuntimeError, "không thể bổ sung Gmail API source"):
            self.allocator.acquire_gmail_api_account(
                batch["batch_id"],
                canonical_batch,
                "job-no-progress",
                0,
            )

        self.assertEqual(len(orders), 3)

    def test_purchase_skips_source_with_globally_exhausted_aliases(self):
        gmail_store = GmailApiUrlBatchStore(self.store.path)
        source_aliases = generate_gmail_dual_domain_aliases("source1@gmail.com", limit=12)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "source1@gmail.com",
                "code_url": "https://mail.example/source/1",
                "aliases": source_aliases,
            },
        ])
        for index in range(12):
            assignment = gmail_store.claim(gmail_batch, f"gmail-job-{index}")
            self.assertTrue(gmail_store.complete(assignment.assignment_id))

        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-after-exhausted", 0)

        self.assertEqual(account.code_url, "https://mail.example/source/2")
        self.assertEqual(len(self.client.created), 2)
        self.record_source.assert_any_call(
            "source1@gmail.com",
            "https://mail.example/source/1",
            status="exhausted",
            note="QAN8 source has no globally available Gmail aliases",
            sqlite_path=self.store.path,
        )

    def test_purchase_skips_source_with_quarantined_code_url(self):
        self.source_failed.side_effect = (
            lambda code_url, **_kwargs: str(code_url).endswith("/source/1")
        )
        batch = self.allocator.create_batch(1, requested_workers=1, aliases_per_source=12)

        account = self.allocator.acquire_account(batch["batch_id"], "job-after-602", 0)

        self.assertEqual(account.code_url, "https://mail.example/source/2")
        self.assertEqual(
            self.store.list_orders(batch["batch_id"])[0]["status"],
            "source_failed",
        )
        self.assertNotIn(
            call(
                "source1@gmail.com",
                "https://mail.example/source/1",
                status="used",
                note="QAN8 purchased source",
                sqlite_path=self.store.path,
            ),
            self.record_source.call_args_list,
        )

    def test_existing_quarantined_source_is_not_reused_after_restart(self):
        batch = self.allocator.create_batch(2, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        failed = self.allocator.acquire_account(batch_id, "job-before-restart", 0)
        self.source_failed.side_effect = (
            lambda code_url, **_kwargs: code_url == failed.code_url
        )

        replacement = self.allocator.acquire_account(batch_id, "job-after-restart", 0)

        self.assertEqual(replacement.code_url, "https://mail.example/source/2")
        self.assertEqual(self.store.get_account_context(failed.email)["source_state"], "retired")
        self.assertEqual(self.store.get_assignment("job-before-restart")["state"], "failed")

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

        self.assertTrue(account.email.endswith(("@gmail.com", "@googlemail.com")))
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
