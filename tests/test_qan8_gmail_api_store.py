import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
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

        self.assertEqual(batch["effective_workers"], 3)
        self.assertEqual([row["lane_id"] for row in self.store.list_lanes(batch["batch_id"])], [0, 1, 2])
        self.assertEqual(self.store.list_orders(batch["batch_id"]), [])

    def test_alias_identity_is_unique_across_different_code_urls(self):
        first = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        self.store.create_source_group(
            first["batch_id"],
            0,
            "source-one@gmail.com",
            "https://mail.example/one",
            ["same+alias@gmail.com"],
        )
        second = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)

        with self.assertRaisesRegex(ValueError, "shared ledger"):
            self.store.create_source_group(
                second["batch_id"],
                0,
                "source-two@gmail.com",
                "https://mail.example/two",
                ["same+alias@gmail.com"],
            )

    def test_canonical_alias_cannot_be_relinked_by_qan8_store(self):
        """QAN8 provenance cannot bypass canonical Gmail alias ownership."""
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_store.create_batch_multi([
            {
                "source_email": "source-one@gmail.com",
                "code_url": "https://mail.example/one",
                "aliases": ["same+alias@gmail.com"],
            }
        ])
        second = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)

        with self.assertRaisesRegex(ValueError, "shared ledger"):
            self.store.create_source_group(
                second["batch_id"],
                0,
                "source-two@gmail.com",
                "https://mail.example/two",
                ["same+alias@gmail.com"],
            )

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM qan8_aliases").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM gmail_api_url_batch_items "
                    "WHERE lower(email) = 'same+alias@gmail.com'"
                ).fetchone()[0],
                1,
            )

    def test_legacy_migration_does_not_release_active_canonical_assignment(self):
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "running-source@gmail.com",
                "code_url": "https://mail.example/running",
                "aliases": ["running+alias@gmail.com"],
            }
        ])
        qan8_batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        self.store.create_source_group(
            qan8_batch["batch_id"],
            0,
            "running-source@gmail.com",
            "https://mail.example/running",
            ["running+alias@gmail.com"],
            gmail_batch_id=gmail_batch,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE qan8_aliases SET gmail_batch_id = NULL, gmail_inventory_id = NULL"
            )
            connection.commit()

        assignment = gmail_store.claim(gmail_batch, "running-job")
        self.assertIsNotNone(assignment)

        Qan8GmailApiStore(self.db_path)

        current = gmail_store.find_active_assignment_for_job("running-job")
        self.assertIsNotNone(current)
        self.assertEqual(current.assignment_id, assignment.assignment_id)

    def test_legacy_migration_reconciles_completed_overlap(self):
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "completed-source@gmail.com",
                "code_url": "https://mail.example/completed",
                "aliases": ["completed+alias@gmail.com"],
            }
        ])
        qan8_batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        self.store.create_source_group(
            qan8_batch["batch_id"],
            0,
            "completed-source@gmail.com",
            "https://mail.example/completed",
            ["completed+alias@gmail.com"],
            gmail_batch_id=gmail_batch,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE qan8_aliases SET gmail_batch_id = NULL, gmail_inventory_id = NULL"
            )
            connection.execute(
                "CREATE TABLE registration_jobs (id INTEGER PRIMARY KEY, status TEXT)"
            )
            connection.execute(
                "INSERT INTO registration_jobs(id, status) VALUES (7, 'success')"
            )
            connection.commit()

        assignment = gmail_store.claim(gmail_batch, "7")
        self.assertIsNotNone(assignment)
        result = self.store.migrate_all_aliases_to_gmail_ledger()

        self.assertEqual(result["terminalized_assignments"], 1)
        self.assertIsNone(gmail_store.find_active_assignment_for_job("7"))

    def test_supplied_gmail_alias_ref_must_match_alias_and_code_url(self):
        """A stale canonical ref cannot attach the wrong mailbox to QAN8."""
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "source-one@gmail.com",
                "code_url": "https://mail.example/one",
                "aliases": ["a+one@gmail.com"],
            }
        ])
        qan8_batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)

        with self.assertRaisesRegex(ValueError, "does not match alias/code_url"):
            self.store.create_source_group(
                qan8_batch["batch_id"],
                0,
                "source-two@gmail.com",
                "https://mail.example/two",
                ["b+one@gmail.com"],
                gmail_alias_refs={
                    "b+one@gmail.com": (
                        gmail_batch,
                        "a+one@gmail.com----https://mail.example/one",
                    )
                },
            )

        self.assertEqual(self.store.list_source_aliases("missing"), [])

    def test_worker_count_is_clamped_to_required_source_count(self):
        batch = self.store.create_batch(2, requested_workers=5, aliases_per_source=12)

        self.assertEqual(batch["effective_workers"], 1)
        self.assertEqual(len(self.store.list_lanes(batch["batch_id"])), 1)

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
        linked_alias = next(
            item
            for item in self.store.list_source_aliases(source["source_group_id"])
            if item["alias"] == assignment["alias"]
        )
        self.assertIsNotNone(linked_alias["gmail_batch_id"])
        self.assertIsNotNone(linked_alias["gmail_inventory_id"])
        self.assertIsNotNone(
            GmailApiUrlBatchStore(self.db_path).get_assignment(assignment["assignment_id"])
        )
        context = self.store.get_account_context("u.ser@gmail.com")
        self.assertEqual(context["source_group_id"], source["source_group_id"])
        self.assertEqual(context["code_url"], "https://mail.example/source")

        self.assertTrue(self.store.complete_assignment(101))
        reopened = Qan8GmailApiStore(self.db_path)
        next_assignment = reopened.claim_alias(batch_id, 0, 102)
        self.assertIsNotNone(next_assignment)

    def test_failed_assignment_is_not_reused(self):
        batch = self.store.create_batch(2, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        self.store.create_source_group(
            batch_id,
            0,
            "source@gmail.com",
            "https://mail.example/source",
            ["source+one@gmail.com", "source+two@gmail.com"],
        )

        failed = self.store.claim_alias(batch_id, 0, "job-failed")
        self.assertIsNotNone(failed)
        self.assertTrue(self.store.fail_assignment("job-failed", reason="registration failed"))

        next_assignment = self.store.claim_alias(batch_id, 0, "job-next")

        self.assertIsNotNone(next_assignment)
        self.assertNotEqual(next_assignment["alias"], failed["alias"])
        self.assertEqual(
            self.store.get_account_context(failed["alias"])["alias_state"],
            "failed",
        )

    def test_claim_skips_alias_consumed_by_gmail_api_url_batch(self):
        code_url = "https://mail.example/shared-source"
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "source@gmail.com",
                "code_url": code_url,
                "aliases": ["source+used@gmail.com"],
            },
        ])
        gmail_assignment = gmail_store.claim(gmail_batch, "gmail-job")
        self.assertTrue(gmail_store.complete(gmail_assignment.assignment_id))

        qan8_batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=2)
        self.store.create_source_group(
            qan8_batch["batch_id"],
            0,
            "source@gmail.com",
            code_url,
            ["source+used@gmail.com", "source+free@gmail.com"],
        )

        assignment = self.store.claim_alias(qan8_batch["batch_id"], 0, "qan8-job")

        self.assertIsNotNone(assignment)
        self.assertEqual(assignment["alias"], "source+free@gmail.com")

    def test_source_creation_inherits_terminal_gmail_alias_state(self):
        code_url = "https://mail.example/inherited-state"
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_batch = gmail_store.create_batch_multi([
            {
                "source_email": "source@gmail.com",
                "code_url": code_url,
                "aliases": ["source+used@gmail.com"],
            },
        ])
        gmail_assignment = gmail_store.claim(gmail_batch, "gmail-job")
        self.assertTrue(gmail_store.complete(gmail_assignment.assignment_id))

        qan8_batch = self.store.create_batch(2, requested_workers=1, aliases_per_source=2)
        source = self.store.create_source_group(
            qan8_batch["batch_id"],
            0,
            "source@gmail.com",
            code_url,
            ["source+used@gmail.com", "source+free@gmail.com"],
        )
        aliases = {
            row["alias"]: row["state"]
            for row in self.store.list_source_aliases(source["source_group_id"])
        }

        self.assertEqual(aliases["source+used@gmail.com"], "consumed")
        self.assertEqual(aliases["source+free@gmail.com"], "available")
        self.assertEqual(self.store.get_source_group(source["source_group_id"])["completed_count"], 1)

    def test_gmail_quarantine_retires_matching_qan8_sources(self):
        code_url = "https://mail.example/quarantined"
        batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=2)
        source = self.store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            code_url,
            ["source+one@gmail.com", "source+two@gmail.com"],
        )
        gmail_store = GmailApiUrlBatchStore(self.db_path)

        self.assertEqual(gmail_store.quarantine_code_url(code_url, reason="Provider error code=602"), 2)

        self.assertEqual(self.store.get_source_group(source["source_group_id"])["state"], "retired")
        self.assertIsNone(self.store.get_current_source(batch["batch_id"], 0))
        self.assertEqual(
            {row["state"] for row in self.store.list_source_aliases(source["source_group_id"])},
            {"failed"},
        )

    def test_migrate_all_aliases_links_legacy_qan8_aliases_and_normalizes_states(self):
        batch = self.store.create_batch(3, requested_workers=1, aliases_per_source=3)
        source = self.store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            "https://mail.example/migration",
            ["source+one@gmail.com", "source+two@gmail.com", "source+three@gmail.com"],
        )
        completed = self.store.claim_alias(batch["batch_id"], 0, "completed-job")
        self.assertIsNotNone(completed)
        self.assertTrue(self.store.complete_assignment("completed-job"))
        failed = self.store.claim_alias(batch["batch_id"], 0, "failed-job")
        self.assertIsNotNone(failed)
        self.assertTrue(self.store.fail_assignment("failed-job", "registration failed"))

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE qan8_aliases SET gmail_batch_id = NULL, gmail_inventory_id = NULL"
            )
            connection.commit()

        result = self.store.migrate_all_aliases_to_gmail_ledger()
        aliases = self.store.list_source_aliases(source["source_group_id"])

        self.assertEqual(result["aliases"], 3)
        self.assertEqual(result["linked"], 3)
        self.assertTrue(all(row["gmail_batch_id"] and row["gmail_inventory_id"] for row in aliases))
        self.assertEqual(
            {row["state"] for row in aliases if row["alias"] == completed["alias"]},
            {"consumed"},
        )
        self.assertEqual(
            {row["state"] for row in aliases if row["alias"] == failed["alias"]},
            {"failed"},
        )
        self.assertEqual(self.store.get_source_group(source["source_group_id"])["state"], "active")

    def test_migration_releases_stale_gmail_assignment_for_available_alias(self):
        code_url = "https://mail.example/stale-migration"
        batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        source = self.store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            code_url,
            ["source+stale@gmail.com"],
        )
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        alias_row = self.store.list_source_aliases(source["source_group_id"])[0]
        stale = gmail_store.claim(alias_row["gmail_batch_id"], "orphaned-job")

        result = self.store.migrate_all_aliases_to_gmail_ledger()
        alias = self.store.list_source_aliases(source["source_group_id"])[0]

        self.assertEqual(result["released_stale_assignments"], 1)
        self.assertEqual(gmail_store.get_assignment(stale.assignment_id).state, "released")
        self.assertEqual(alias["state"], "available")
        self.assertTrue(alias["gmail_batch_id"])
        self.assertTrue(alias["gmail_inventory_id"])

    def test_migration_releases_orphaned_active_alias(self):
        batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        source = self.store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            "https://mail.example/orphaned-active",
            ["source+orphaned@gmail.com"],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE qan8_aliases SET state = 'active', gmail_batch_id = NULL, gmail_inventory_id = NULL "
                "WHERE source_group_id = ?",
                (source["source_group_id"],),
            )
            connection.commit()

        self.store.migrate_all_aliases_to_gmail_ledger()

        alias = self.store.list_source_aliases(source["source_group_id"])[0]
        self.assertEqual(alias["state"], "available")

    def test_migration_terminalizes_stale_gmail_assignment_for_failed_alias(self):
        code_url = "https://mail.example/failed-migration"
        batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        source = self.store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            code_url,
            ["source+failed@gmail.com"],
        )
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        alias_row = self.store.list_source_aliases(source["source_group_id"])[0]
        stale = gmail_store.claim(alias_row["gmail_batch_id"], "orphaned-failed-job")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE qan8_aliases SET state = 'failed', gmail_batch_id = NULL, gmail_inventory_id = NULL"
            )
            connection.commit()

        result = self.store.migrate_all_aliases_to_gmail_ledger()
        alias = self.store.list_source_aliases(source["source_group_id"])[0]

        self.assertEqual(result["terminalized_assignments"], 1)
        self.assertEqual(gmail_store.get_assignment(stale.assignment_id).state, "failed")
        self.assertEqual(alias["state"], "failed")
        self.assertEqual(self.store.get_source_group(source["source_group_id"])["state"], "exhausted")

    def test_migration_quarantines_root_collision_and_continues(self):
        """A legacy root collision must not abort migration of other aliases."""
        gmail_store = GmailApiUrlBatchStore(self.db_path)
        gmail_store.create_batch_multi([
            {
                "source_email": "canonical-owner@gmail.com",
                "code_url": "https://mail.example/canonical-owner",
                "aliases": ["same@gmail.com"],
            }
        ])
        batch = self.store.create_batch(2, requested_workers=1, aliases_per_source=2)
        source_group_id = uuid.uuid4().hex
        now = time.time()
        aliases = ["s.ame@gmail.com", "safe+legacy@gmail.com"]
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO qan8_sources("
                "source_group_id, batch_id, lane_id, source_email, code_url, capacity, created_at) "
                "VALUES (?, ?, 0, ?, ?, 2, ?)",
                (
                    source_group_id,
                    batch["batch_id"],
                    "legacy-source@gmail.com",
                    "https://mail.example/legacy",
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO qan8_aliases("
                "alias_id, source_group_id, alias, ordinal, state, created_at) "
                "VALUES (?, ?, ?, ?, 'available', ?)",
                (
                    (uuid.uuid4().hex, source_group_id, alias, ordinal, now)
                    for ordinal, alias in enumerate(aliases)
                ),
            )
            connection.execute(
                "UPDATE qan8_lanes SET current_source_group_id = ? "
                "WHERE batch_id = ? AND lane_id = 0",
                (source_group_id, batch["batch_id"]),
            )
            connection.commit()

        result = self.store.migrate_all_aliases_to_gmail_ledger()
        migrated = self.store.list_source_aliases(source_group_id)

        self.assertEqual(result["aliases"], 2)
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["conflicted_aliases"], 1)
        collision = next(row for row in migrated if row["alias"] == "s.ame@gmail.com")
        self.assertEqual(collision["state"], "failed")
        self.assertIsNone(collision["gmail_batch_id"])
        safe = next(row for row in migrated if row["alias"] == "safe+legacy@gmail.com")
        self.assertEqual(safe["state"], "available")
        self.assertTrue(safe["gmail_batch_id"])
        self.assertTrue(safe["gmail_inventory_id"])
        self.assertEqual(self.store.get_source_group(source_group_id)["state"], "active")

    def test_migration_links_unlinked_legacy_alias_without_canonical_row(self):
        """Historical QAN8 rows with no canonical item are linked on migration."""
        batch = self.store.create_batch(1, requested_workers=1, aliases_per_source=1)
        source_group_id = uuid.uuid4().hex
        now = time.time()
        alias = "legacyonly123@gmail.com"
        code_url = "https://mail.example/legacy-only"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO qan8_sources("
                "source_group_id, batch_id, lane_id, source_email, code_url, capacity, created_at) "
                "VALUES (?, ?, 0, ?, ?, 1, ?)",
                (
                    source_group_id,
                    batch["batch_id"],
                    "legacy-source-only@gmail.com",
                    code_url,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO qan8_aliases("
                "alias_id, source_group_id, alias, ordinal, state, created_at) "
                "VALUES (?, ?, ?, 0, 'available', ?)",
                (uuid.uuid4().hex, source_group_id, alias, now),
            )
            connection.commit()

        result = self.store.migrate_all_aliases_to_gmail_ledger()
        migrated = self.store.list_source_aliases(source_group_id)[0]

        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["conflicted_aliases"], 0)
        self.assertEqual(migrated["state"], "available")
        self.assertTrue(migrated["gmail_batch_id"])
        self.assertTrue(migrated["gmail_inventory_id"])

    def test_source_alias_usage_reports_consumed_failed_and_active_slots(self):
        batch = self.store.create_batch(3, requested_workers=1, aliases_per_source=3)
        batch_id = batch["batch_id"]
        source_email = "source@gmail.com"
        code_url = "https://mail.example/source"
        self.store.create_source_group(
            batch_id,
            0,
            source_email,
            code_url,
            [
                "source+one@gmail.com",
                "source+two@gmail.com",
                "source+three@gmail.com",
            ],
        )

        completed = self.store.claim_alias(batch_id, 0, "job-completed")
        self.assertIsNotNone(completed)
        self.assertTrue(self.store.complete_assignment("job-completed"))
        failed = self.store.claim_alias(batch_id, 0, "job-failed")
        self.assertIsNotNone(failed)
        self.assertTrue(self.store.fail_assignment("job-failed", reason="registration failed"))
        active = self.store.claim_alias(batch_id, 0, "job-active")
        self.assertIsNotNone(active)

        usage = self.store.alias_usage_for_source(source_email, code_url)

        self.assertEqual(
            usage,
            {"total": 3, "available": 0, "used": 1, "failed": 1, "reserved": 1},
        )

    def test_read_only_alias_usage_does_not_create_qan8_schema(self):
        empty_path = Path(self.temp_dir.name) / "empty.sqlite3"

        store = Qan8GmailApiStore(empty_path, initialize_schema=False)

        self.assertIsNone(
            store.alias_usage_for_source("source@gmail.com", "https://mail.example/source")
        )
        with closing(sqlite3.connect(empty_path)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'qan8_%'"
            ).fetchall()
        self.assertEqual(tables, [])

    def test_exhausted_failed_source_is_removed_from_lane(self):
        batch = self.store.create_batch(3, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        source = self.store.create_source_group(
            batch_id,
            0,
            "source@gmail.com",
            "https://mail.example/source",
            ["source+one@gmail.com", "source+two@gmail.com"],
        )

        first = self.store.claim_alias(batch_id, 0, "job-failed-1")
        self.assertIsNotNone(first)
        self.assertTrue(self.store.fail_assignment("job-failed-1", reason="blocked"))
        second = self.store.claim_alias(batch_id, 0, "job-failed-2")
        self.assertIsNotNone(second)
        self.assertTrue(self.store.fail_assignment("job-failed-2", reason="blocked"))

        self.assertEqual(
            self.store.get_source_group(source["source_group_id"])["state"],
            "exhausted",
        )
        self.assertIsNone(self.store.get_current_source(batch_id, 0))

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

    def test_quarantine_lane_retires_current_source_and_allows_replacement(self):
        batch = self.store.create_batch(3, requested_workers=1, aliases_per_source=2)
        batch_id = batch["batch_id"]
        source = self.store.create_source_group(
            batch_id,
            0,
            "broken@gmail.com",
            "https://mail.example/broken",
            ["broken+one@gmail.com", "broken+two@gmail.com"],
        )
        assignment = self.store.claim_alias(batch_id, 0, "job-broken")

        assert assignment is not None
        assert self.store.quarantine_lane(batch_id, 0, "Provider error code=602") == 1
        self.assertEqual(self.store.get_lane(batch_id, 0)["state"], "active")
        self.assertIsNone(self.store.get_current_source(batch_id, 0))
        self.assertEqual(self.store.get_source_group(source["source_group_id"])["state"], "retired")
        self.assertEqual(
            {row["state"] for row in self.store.list_source_aliases(source["source_group_id"])},
            {"failed"},
        )
        self.assertEqual(self.store.get_assignment("job-broken")["state"], "failed")
        replacement = self.store.create_source_group(
            batch_id,
            0,
            "healthy@gmail.com",
            "https://mail.example/healthy",
            ["healthy+one@gmail.com"],
        )
        next_assignment = self.store.claim_alias(batch_id, 0, "job-next")

        self.assertEqual(replacement["state"], "active")
        self.assertEqual(next_assignment["alias"], "healthy+one@gmail.com")


if __name__ == "__main__":
    unittest.main()
