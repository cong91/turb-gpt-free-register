from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.cdk_inventory_store import (
    CdkInventoryConflict,
    CdkInventorySchemaError,
    CdkInventoryStore,
)
from core.gmail_aliases import build_gmail_alias_plan


class CdkInventoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "cdk_inventory.sqlite3"
        self.store = CdkInventoryStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialize_creates_versioned_schema_and_is_idempotent(self):
        self.store.initialize()
        self.store.initialize()

        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "cdk_inventory",
                    "cdk_slots",
                    "cdk_leases",
                    "cdk_intents",
                    "cdk_events",
                    "cdk_schema_migrations",
                }.issubset(tables)
            )
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            self.assertTrue({"cdk_events_no_update", "cdk_events_no_delete"}.issubset(triggers))

    def test_import_is_provider_namespaced_and_duplicate_reported(self):
        first, created = self.store.import_cdk("gmail", "secret-cdk", configured_limit=6)
        duplicate, duplicate_created = self.store.import_cdk("gmail", "secret-cdk", configured_limit=6)
        other_provider, other_created = self.store.import_cdk("paymesh", "secret-cdk", configured_limit=6)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.inventory_id, duplicate.inventory_id)
        self.assertTrue(other_created)
        self.assertNotEqual(first.inventory_id, other_provider.inventory_id)
        self.assertTrue(first.fingerprint.startswith("sha256:"))

    def test_job_id_is_text_preserved(self):
        record, _ = self.store.import_cdk("gmail", "secret-cdk")
        reservation = self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "00000000000000000001",
            operation_id="reserve-job-id",
            owner_token="owner-job-id-secret",
        )

        self.assertNotIn("owner-job-id-secret", repr(reservation))
        self.assertNotIn("owner_token", reservation.to_dict())
        with closing(sqlite3.connect(self.path)) as connection:
            value = connection.execute(
                "SELECT job_id FROM cdk_slots WHERE slot_id = ?",
                (reservation.reservation_id,),
            ).fetchone()[0]
        self.assertEqual(value, "00000000000000000001")

    def test_masked_dto_never_exposes_raw_cdk(self):
        raw_cdk = "Secret-Cdk-Do-Not-Leak"
        record, _ = self.store.import_cdk("gmail", raw_cdk)
        public = record.to_dict()

        self.assertNotIn(raw_cdk, repr(record))
        self.assertNotIn(raw_cdk, repr(public))
        self.assertNotIn("raw_cdk", public)
        self.assertEqual(self.store.resolve_raw_cdk(record.inventory_id), raw_cdk)
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute(
                "SELECT raw_cdk FROM cdk_inventory WHERE inventory_id = ?",
                (record.inventory_id,),
            ).fetchone()[0]
        self.assertEqual(stored, raw_cdk)

    def test_provider_remaining_zero_and_unknown_are_distinct(self):
        zero, _ = self.store.import_cdk("gmail", "zero-cdk")
        unknown, _ = self.store.import_cdk("gmail", "unknown-cdk")
        self.assertIsNone(zero.provider_remaining)
        self.assertIsNone(unknown.provider_remaining)

        self.store.update_provider_quota(zero.inventory_id, 0)
        self.assertEqual(self.store.get_inventory(zero.inventory_id).provider_remaining, 0)
        self.assertIsNone(self.store.get_inventory(unknown.inventory_id).provider_remaining)

    def test_future_schema_version_fails_closed(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaises(CdkInventorySchemaError):
            CdkInventoryStore(self.path).initialize()

    def test_sqlite_3504_uses_rollback_journal(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
    def test_reservation_transitions_are_owner_checked_and_idempotent(self):
        record, _ = self.store.import_cdk("gmail", "transition-cdk")
        reservation = self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "job-text-1",
            operation_id="op-reserve-1",
            owner_token="owner-1",
        )
        same = self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "job-text-1",
            operation_id="op-reserve-1",
            owner_token="owner-1",
        )

        self.assertEqual(same.reservation_id, reservation.reservation_id)
        self.assertFalse(
            self.store.consume_reservation(
                reservation.reservation_id,
                operation_id="op-consume-wrong-owner",
                owner_token="wrong-owner",
            )
        )
        self.assertTrue(
            self.store.consume_reservation(
                reservation.reservation_id,
                operation_id="op-consume-1",
                owner_token="owner-1",
                account_id=42,
            )
        )
        self.assertTrue(
            self.store.consume_reservation(
                reservation.reservation_id,
                operation_id="op-consume-1",
                owner_token="wrong-owner",
                account_id=42,
            )
        )
        events, total = self.store.list_events(record.inventory_id)
        self.assertEqual(total, 2)
        self.assertEqual([event.event_type for event in reversed(events)], ["slot_reserved", "slot_consumed"])

    def test_disable_blocks_new_reservations_but_preserves_active(self):
        record, _ = self.store.import_cdk("gmail", "disabled-cdk")
        reservation = self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "job-1",
            operation_id="op-reserve-disabled-1",
            owner_token="owner-1",
        )
        self.store.set_state(record.inventory_id, "disabled")

        with self.assertRaisesRegex(Exception, "not active"):
            self.store.reserve_slot(
                record.inventory_id,
                "b@example.com",
                "job-2",
                operation_id="op-reserve-disabled-2",
                owner_token="owner-2",
            )
        self.assertTrue(
            self.store.consume_reservation(
                reservation.reservation_id,
                operation_id="op-consume-disabled-1",
                owner_token="owner-1",
            )
        )

    def test_operation_id_reuse_for_different_work_is_rejected(self):
        first, _ = self.store.import_cdk("gmail", "operation-first-cdk")
        second, _ = self.store.import_cdk("gmail", "operation-second-cdk")
        reservation = self.store.reserve_slot(
            first.inventory_id,
            "first@example.com",
            "job-first",
            operation_id="shared-reserve-operation",
            owner_token="owner-first",
        )

        with self.assertRaises(CdkInventoryConflict):
            self.store.reserve_slot(
                second.inventory_id,
                "second@example.com",
                "job-second",
                operation_id="shared-reserve-operation",
                owner_token="owner-second",
            )
        self.assertTrue(
            self.store.consume_reservation(
                reservation.reservation_id,
                operation_id="shared-transition-operation",
                owner_token="owner-first",
            )
        )
        other = self.store.reserve_slot(
            second.inventory_id,
            "second@example.com",
            "job-second",
            operation_id="second-reserve-operation",
            owner_token="owner-second",
        )
        with self.assertRaises(CdkInventoryConflict):
            self.store.release_reservation(
                other.reservation_id,
                operation_id="shared-transition-operation",
                owner_token="owner-second",
            )

    def test_six_slot_limit_uses_first_free_email(self):
        record, _ = self.store.import_cdk("gmail", "six-cdk", configured_limit=6)
        emails = [f"user+{index}@example.com" for index in range(7)]
        reservations = [
            self.store.reserve_slot(
                record.inventory_id,
                email,
                f"job-{index}",
                operation_id=f"op-reserve-six-{index}",
                owner_token=f"owner-{index}",
            )
            for index, email in enumerate(emails[:6])
        ]

        self.assertEqual([reservation.email for reservation in reservations], emails[:6])
        self.assertTrue(
            self.store.release_reservation(
                reservations[1].reservation_id,
                operation_id="op-release-six-1",
                owner_token="owner-1",
            )
        )
        replacement = self.store.reserve_first_available_slot(
            record.inventory_id,
            emails,
            "job-replacement",
            operation_id="op-reserve-six-replacement",
            owner_token="owner-replacement",
        )
        self.assertEqual(replacement.email, emails[1])
        with self.assertRaisesRegex(Exception, "exhausted"):
            self.store.reserve_first_available_slot(
                record.inventory_id,
                emails,
                "job-overflow",
                operation_id="op-reserve-six-overflow",
                owner_token="owner-overflow",
            )

    def test_gmail_plan_allocates_original_then_routed_without_phase_regression(self):
        record, _ = self.store.import_cdk("gmail", "routed-cdk", configured_limit=3)
        plan = build_gmail_alias_plan(
            "abcdef@gmail.com",
            limit=3,
            routed_domains=["route-one.net", "route-two.org"],
        )
        reservations = [
            self.store.reserve_gmail_alias(
                record.inventory_id,
                plan.candidates,
                f"job-{index}",
                operation_id=f"op-routed-{index}",
                owner_token=f"owner-{index}",
                routed_domains=plan.routed_domains,
            )
            for index in range(6)
        ]

        self.assertEqual([item.alias_phase for item in reservations], ["original"] * 3 + ["routed"] * 3)
        self.assertEqual(
            [item.alias_domain for item in reservations[3:]],
            ["route-one.net", "route-one.net", "route-two.org"],
        )
        self.assertTrue(self.store.release_reservation(
            reservations[0].reservation_id,
            operation_id="release-original-after-routed",
            owner_token="owner-0",
        ))
        with self.assertRaisesRegex(CdkInventoryConflict, "exhausted"):
            self.store.reserve_gmail_alias(
                record.inventory_id,
                plan.candidates,
                "job-overflow",
                operation_id="op-routed-overflow",
                owner_token="owner-overflow",
                routed_domains=plan.routed_domains,
            )

    def test_schema_one_migrates_to_gmail_phase_columns(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        reopened = CdkInventoryStore(self.path)
        reopened.initialize()

        with closing(sqlite3.connect(self.path)) as connection:
            inventory_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(cdk_inventory)")
            }
            slot_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(cdk_slots)")
            }
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIn("allocation_phase", inventory_columns)
        self.assertIn("routing_domains", inventory_columns)
        self.assertIn("alias_phase", slot_columns)
        self.assertIn("alias_domain", slot_columns)

    def test_event_key_prevents_duplicate_audit_rows(self):
        record, _ = self.store.import_cdk("gmail", "event-key-cdk")
        self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "job-1",
            operation_id="op-event-key",
            owner_token="owner-1",
        )
        events, total = self.store.list_events(record.inventory_id)

        self.assertEqual(total, 1)
        with closing(sqlite3.connect(self.path)) as connection:  # noqa: SIM117
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO cdk_events "
                    "(event_id, inventory_id, sequence, event_type, operation_id, actor, payload) "
                    "VALUES ('event-dupe', ?, 99, 'duplicate', 'op-event-key', 'test', '{}')",
                    (record.inventory_id,),
                )
        self.assertEqual(events[0].operation_id, "op-event-key")

    def test_event_rows_reject_update_and_delete(self):
        record, _ = self.store.import_cdk("gmail", "event-append-cdk")
        self.store.reserve_slot(
            record.inventory_id,
            "a@example.com",
            "job-1",
            operation_id="op-event-append",
            owner_token="owner-1",
        )

        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE cdk_events SET actor = 'changed'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM cdk_events")

    def test_domain_mutation_and_event_roll_back_together(self):
        record, _ = self.store.import_cdk("gmail", "rollback-cdk")
        with patch.object(self.store, "_append_event", side_effect=RuntimeError("event write failed")):  # noqa: SIM117
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                self.store.reserve_slot(
                    record.inventory_id,
                    "a@example.com",
                    "job-1",
                    operation_id="op-rollback",
                    owner_token="owner-1",
                )

        events, total = self.store.list_events(record.inventory_id)
        self.assertEqual(events, [])
        self.assertEqual(total, 0)
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM cdk_slots").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
