# -*- coding: utf-8 -*-
from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from queue import Empty

from core.cdk_inventory_store import (
    CdkInventoryBusy,
    CdkInventoryConflict,
    CdkInventorySchemaError,
    CdkInventoryStore,
)


def _reserve_final_slot(path: str, inventory_id: str, owner: str, start, results) -> None:
    store = CdkInventoryStore(path, busy_timeout_ms=2000)
    start.wait()
    try:
        reservation = store.reserve_slot(
            inventory_id,
            f"{owner}@example.com",
            owner,
            operation_id=f"reserve-{owner}",
            owner_token=owner,
        )
    except CdkInventoryConflict:
        results.put(("conflict", owner))
    except Exception as exc:
        results.put(("error", type(exc).__name__))
    else:
        results.put(("success", reservation.email))


def _take_over_expired_lease(path: str, inventory_id: str, owner: str, start, results) -> None:
    store = CdkInventoryStore(path, busy_timeout_ms=2000)
    start.wait()
    try:
        lease = store.acquire_lease(inventory_id, owner_token=owner, ttl_seconds=30)
    except CdkInventoryConflict:
        results.put(("conflict", owner))
    except Exception as exc:
        results.put(("error", type(exc).__name__))
    else:
        results.put(("success", lease.fencing_token))


class CdkInventoryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "cdk_inventory.sqlite3"
        self.store = CdkInventoryStore(self.path, busy_timeout_ms=250)
        self.record, _ = self.store.import_cdk("paymesh", "concurrency-cdk", configured_limit=1)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _run_processes(target, args_factory):
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(target=target, args=(*args_factory(index), start, results))
            for index in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(2)
                raise AssertionError("inventory contention worker did not finish")
            if process.exitcode != 0:
                raise AssertionError(f"inventory contention worker exited {process.exitcode}")
        output = []
        for _ in processes:
            try:
                output.append(results.get(timeout=2))
            except Empty as exc:
                raise AssertionError("inventory contention worker returned no result") from exc
        results.close()
        results.join_thread()
        return output

    def test_processes_contending_for_final_slot_yield_one_success(self):
        output = self._run_processes(
            _reserve_final_slot,
            lambda index: (str(self.path), self.record.inventory_id, f"owner-{index}"),
        )

        self.assertEqual([status for status, _ in output].count("success"), 1)
        self.assertEqual([status for status, _ in output].count("conflict"), 1)
        self.assertNotIn("error", [status for status, _ in output])

    def test_locked_database_surfaces_retryable_error_within_timeout(self):
        locker = sqlite3.connect(self.path, isolation_level=None)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with self.assertRaises(CdkInventoryBusy):
                self.store._write(
                    lambda connection: connection.execute(
                        "UPDATE cdk_inventory SET updated_at = CURRENT_TIMESTAMP WHERE inventory_id = ?",
                        (self.record.inventory_id,),
                    )
                )
            elapsed = time.monotonic() - started
        finally:
            locker.rollback()
            locker.close()

        self.assertLess(elapsed, 1.5)

    def test_non_busy_operational_error_is_not_retried(self):
        calls = 0

        def invalid_sql(connection):
            nonlocal calls
            calls += 1
            return connection.execute("UPDATE table_that_does_not_exist SET value = 1")

        with self.assertRaises(sqlite3.OperationalError):
            self.store._write(invalid_sql)
        self.assertEqual(calls, 1)

    def test_paymesh_lease_blocks_second_owner_until_release(self):
        first = self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="owner-1",
            ttl_seconds=30,
        )
        self.assertNotIn("owner-1", repr(first))
        self.assertNotIn(first.fencing_token, repr(first))
        with self.assertRaises(CdkInventoryConflict):
            self.store.acquire_lease(
                self.record.inventory_id,
                owner_token="owner-2",
                ttl_seconds=30,
            )

        self.assertTrue(
            self.store.release_lease(
                first.lease_id,
                owner_token="owner-1",
                fencing_token=first.fencing_token,
            )
        )
        second = self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="owner-2",
            ttl_seconds=30,
        )
        self.assertNotEqual(first.fencing_token, second.fencing_token)

    def test_expired_lease_has_one_takeover_winner(self):
        first = self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="expired-owner",
            ttl_seconds=1,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE cdk_leases SET expires_at = ? WHERE lease_id = ?",
                (time.time() - 1, first.lease_id),
            )
            connection.commit()

        output = self._run_processes(
            _take_over_expired_lease,
            lambda index: (str(self.path), self.record.inventory_id, f"takeover-{index}"),
        )
        self.assertEqual([status for status, _ in output].count("success"), 1)
        self.assertEqual([status for status, _ in output].count("conflict"), 1)

    def test_live_heartbeat_prevents_expiry(self):
        lease = self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="live-owner",
            ttl_seconds=1,
        )
        heartbeat = self.store.heartbeat_lease(
            lease.lease_id,
            owner_token="live-owner",
            fencing_token=lease.fencing_token,
            ttl_seconds=30,
        )

        self.assertGreater(heartbeat.expires_at, lease.expires_at)
        with self.assertRaises(CdkInventoryConflict):
            self.store.acquire_lease(
                self.record.inventory_id,
                owner_token="blocked-owner",
                ttl_seconds=30,
            )

    def test_stale_fencing_token_cannot_complete_or_release(self):
        first = self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="first-owner",
            ttl_seconds=1,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE cdk_leases SET expires_at = ? WHERE lease_id = ?",
                (time.time() - 1, first.lease_id),
            )
            connection.commit()
        self.store.acquire_lease(
            self.record.inventory_id,
            owner_token="new-owner",
            ttl_seconds=30,
        )

        self.assertFalse(
            self.store.release_lease(
                first.lease_id,
                owner_token="first-owner",
                fencing_token=first.fencing_token,
            )
        )
        with self.assertRaises(CdkInventoryConflict):
            self.store.assert_active_lease(
                first.lease_id,
                owner_token="first-owner",
                fencing_token=first.fencing_token,
            )

    def test_persistent_wal_on_unsafe_runtime_fails_or_converts_closed(self):
        wal_path = Path(self.temp_dir.name) / "unsafe-wal.sqlite3"
        with closing(sqlite3.connect(wal_path)) as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()

        unsafe_store = CdkInventoryStore(wal_path)
        try:
            unsafe_store.initialize()
        except CdkInventorySchemaError:
            pass
        else:
            with closing(sqlite3.connect(wal_path)) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")


if __name__ == "__main__":
    unittest.main()
