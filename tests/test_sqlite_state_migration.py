from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import sqlite_state_migration
from core.sqlite_state_migration import (
    SqliteStateConflict,
    SqliteStateMigrationError,
    audit_databases,
    migrate_databases,
)


def _create_fixture(path: Path, schema: str, rows: list[tuple[object, ...]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        connection.executemany(
            "INSERT INTO provider_state(state_id, payload) VALUES (?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _create_migration_sources(app_state: Path, turb: Path, *, app_accounts: bool = False) -> None:
    app_schema = """
        CREATE TABLE provider_state (
            state_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        );
    """
    if app_accounts:
        app_schema += """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
        """
    app_connection = sqlite3.connect(app_state)
    try:
        app_connection.executescript(app_schema)
        app_connection.execute(
            "INSERT INTO provider_state(state_id, payload) VALUES (?, ?)",
            (1, "provider-state"),
        )
        if app_accounts:
            app_connection.execute(
                "INSERT INTO accounts(id, email, payload) VALUES (?, ?, ?)",
                (1, "same@example.test", "same-row"),
            )
        app_connection.commit()
    finally:
        app_connection.close()

    turb_connection = sqlite3.connect(turb)
    try:
        turb_connection.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE email_pool (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE registration_jobs (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE codex_accounts (
                id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE codex_agent_accounts (
                account_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE ignored_runtime_table (
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE INDEX idx_accounts_email ON accounts(email);
            """
        )
        turb_connection.executemany(
            "INSERT INTO accounts(id, email, payload) VALUES (?, ?, ?)",
            [(1, "same@example.test", "same-row")],
        )
        turb_connection.execute(
            "INSERT INTO email_pool(id, email, payload) VALUES (?, ?, ?)",
            (2, "pool@example.test", "pool-row"),
        )
        turb_connection.execute(
            "INSERT INTO registration_jobs(id, email, payload) VALUES (?, ?, ?)",
            (3, "job@example.test", "job-row"),
        )
        turb_connection.execute(
            "INSERT INTO codex_accounts(id, filename, email, payload) VALUES (?, ?, ?, ?)",
            (4, "codex-4.json", "codex@example.test", "codex-row"),
        )
        turb_connection.execute(
            "INSERT INTO codex_agent_accounts(account_id, email, payload) VALUES (?, ?, ?)",
            (5, "agent@example.test", "agent-row"),
        )
        turb_connection.execute(
            "INSERT INTO ignored_runtime_table(id, payload) VALUES (?, ?)",
            (6, "must-not-copy"),
        )
        turb_connection.commit()
    finally:
        turb_connection.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AuditTests(unittest.TestCase):
    def test_audit_reports_schema_counts_and_digests_without_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            secret_marker = "secret-payload-must-not-be-reported"
            schema = """
                CREATE TABLE provider_state (
                    state_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL
                );
            """
            _create_fixture(app_state, schema, [(1, secret_marker)])
            _create_fixture(turb, schema, [(2, "another-secret")])

            report = audit_databases(app_state, turb)
            rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(report["format_version"], 1)
            self.assertEqual(report["sources"]["app_state"]["tables"]["provider_state"]["row_count"], 1)
            self.assertEqual(report["sources"]["turb"]["tables"]["provider_state"]["row_count"], 1)
            self.assertRegex(
                report["sources"]["app_state"]["tables"]["provider_state"]["row_digest"],
                r"^[0-9a-f]{64}$",
            )
            self.assertNotIn(secret_marker, rendered)
            self.assertNotIn("another-secret", rendered)


class MigrationTests(unittest.TestCase):
    def test_migration_creates_backup_target_and_imports_authoritative_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "app_state.migrated.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb)
            app_hash = _file_sha256(app_state)
            turb_hash = _file_sha256(turb)

            report = migrate_databases(app_state, turb, target, backup_dir)

            self.assertEqual(report["action"], "migrate")
            self.assertEqual(report["validation"]["integrity_check"], "ok")
            self.assertEqual(_file_sha256(app_state), app_hash)
            self.assertEqual(_file_sha256(turb), turb_hash)
            self.assertTrue((backup_dir / "app_state.snapshot.sqlite3").is_file())
            self.assertTrue((backup_dir / "turb.snapshot.sqlite3").is_file())
            connection = sqlite3.connect(target)
            try:
                for table in (
                    "provider_state",
                    "accounts",
                    "email_pool",
                    "registration_jobs",
                    "codex_accounts",
                    "codex_agent_accounts",
                ):
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                    )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ignored_runtime_table'"
                    ).fetchone()
                )
                self.assertRegex(
                    connection.execute(
                        "SELECT applied_at FROM app_state_migrations WHERE migration_key=?",
                        ("migration:application_state:1",),
                    ).fetchone()[0],
                    r"^20\d\d-",
                )
            finally:
                connection.close()

    def test_dry_run_does_not_create_target_or_backup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb)

            report = migrate_databases(app_state, turb, target, backup_dir, dry_run=True)

            self.assertEqual(report["action"], "dry-run")
            self.assertFalse(target.exists())
            self.assertFalse(backup_dir.exists())

    def test_identical_duplicate_row_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb, app_accounts=True)

            migrate_databases(app_state, turb, target, backup_dir)

            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            finally:
                connection.close()

    def test_conflicting_duplicate_rolls_back_generated_target_without_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb, app_accounts=True)
            connection = sqlite3.connect(turb)
            try:
                connection.execute(
                    "UPDATE accounts SET payload=? WHERE id=?",
                    ("different-secret-like-row", 1),
                )
                connection.commit()
            finally:
                connection.close()
            before = _file_sha256(turb)

            with self.assertRaises(SqliteStateConflict) as raised:
                migrate_databases(app_state, turb, target, backup_dir)

            self.assertNotIn("different-secret-like-row", str(raised.exception))
            self.assertIn("accounts", str(raised.exception))
            self.assertFalse(target.exists())
            self.assertEqual(_file_sha256(turb), before)

    def test_schema_mismatch_and_existing_target_fail_before_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb, app_accounts=True)
            connection = sqlite3.connect(app_state)
            try:
                connection.execute("ALTER TABLE accounts ADD COLUMN incompatible TEXT")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(SqliteStateConflict):
                migrate_databases(app_state, turb, target, backup_dir)
            self.assertFalse(target.exists())

            target.write_bytes(b"existing-target")
            with self.assertRaises(SqliteStateMigrationError):
                migrate_databases(app_state, turb, target, root / "other-backups")

    def test_foreign_key_failure_rolls_back_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb)
            connection = sqlite3.connect(turb)
            try:
                connection.execute("DROP TABLE codex_agent_accounts")
                connection.execute(
                    "CREATE TABLE codex_agent_accounts (account_id INTEGER PRIMARY KEY, email TEXT NOT NULL, "
                    "payload TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(id))"
                )
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "INSERT INTO codex_agent_accounts(account_id, email, payload) VALUES (?, ?, ?)",
                    (999, "broken@example.test", "broken-row"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(SqliteStateMigrationError):
                migrate_databases(app_state, turb, target, backup_dir)

            self.assertFalse(target.exists())

    def test_duplicate_snapshot_names_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            turb_dir = root / "turb"
            app_dir.mkdir()
            turb_dir.mkdir()
            app_state = app_dir / "state.sqlite3"
            turb = turb_dir / "state.sqlite3"
            _create_migration_sources(app_state, turb)

            with self.assertRaises(SqliteStateMigrationError):
                migrate_databases(
                    app_state,
                    turb,
                    root / "target.sqlite3",
                    root / "backups",
                )

            self.assertFalse((root / "backups").exists())

    def test_partial_target_is_removed_when_target_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_state = root / "app_state.sqlite3"
            turb = root / "turb.sqlite3"
            target = root / "target.sqlite3"
            backup_dir = root / "backups"
            _create_migration_sources(app_state, turb)
            real_backup = sqlite_state_migration._backup_database
            calls = 0

            def fail_on_target_backup(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if destination == target:
                    target.write_bytes(b"partial-target")
                    raise OSError("simulated target backup failure")
                real_backup(source, destination)

            with (
                patch("core.sqlite_state_migration._backup_database", side_effect=fail_on_target_backup),
                self.assertRaises(OSError),
            ):
                migrate_databases(app_state, turb, target, backup_dir)

            self.assertEqual(calls, 3)
            self.assertFalse(target.exists())



if __name__ == "__main__":
    unittest.main()
