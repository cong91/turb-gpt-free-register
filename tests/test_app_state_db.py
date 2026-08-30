import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app_state_db, db


class AppStateDbTests(unittest.TestCase):
    def test_connection_reasserts_rollback_journal_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            initial = sqlite3.connect(path)
            try:
                self.assertEqual(initial.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower(), "wal")
            finally:
                initial.close()

            connection = app_state_db.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()

    def test_core_database_path_tracks_canonical_turb_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "turb.sqlite3"
            with (
                patch.object(app_state_db, "APP_STATE_DB_PATH", database),
                patch.object(db, "_DEFAULT_SQLITE_PATH", database),
            ):
                self.assertEqual(db._active_sqlite_path().resolve(), database.resolve())
                self.assertEqual(Path(db.storage_paths()["sqlite"]).resolve(), database.resolve())

    def test_default_core_path_does_not_wait_for_migration_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "turb.sqlite3"
            with patch.object(db, "_DEFAULT_SQLITE_PATH", database):
                self.assertEqual(db._active_sqlite_path().resolve(), database.resolve())

    def test_central_initialization_does_not_read_stale_json_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "turb.sqlite3"
            with (
                patch.object(app_state_db, "APP_STATE_DB_PATH", database),
                patch.object(db, "_DEFAULT_SQLITE_PATH", database),
                patch.object(db, "_SQLITE_READY", False),
                patch.object(db, "_SQLITE_READY_PATH", None),
                patch.object(db, "_read_json", side_effect=AssertionError("central init read an export")),
            ):
                db._ensure_sqlite()

            connection = sqlite3.connect(database)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_ready_flag_rechecks_missing_core_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "turb.sqlite3"
            with (
                patch.object(db, "_DEFAULT_SQLITE_PATH", database),
                patch.object(db, "_SQLITE_READY", False),
                patch.object(db, "_SQLITE_READY_PATH", None),
                patch.object(db, "_ACCOUNTS_JSON", database.with_name("accounts.json")),
            ):
                db._ensure_sqlite()
                connection = sqlite3.connect(database)
                try:
                    connection.execute("DROP TABLE accounts")
                    connection.commit()
                finally:
                    connection.close()
                self.assertFalse(db._sqlite_schema_ready(database))
                db._ensure_sqlite()
                self.assertTrue(db._sqlite_schema_ready(database))

    def test_schema_and_named_documents_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            connection = app_state_db.connect(path)
            app_state_db.ensure_schema(connection)
            app_state_db.set_component_version(connection, "example", 3)
            connection.close()

            reopened = app_state_db.connect(path)
            self.assertEqual(app_state_db.get_component_version(reopened, "example"), 3)
            reopened.execute(
                "INSERT INTO app_documents(document_key, payload_json) VALUES (?, ?)",
                ("named:test", json.dumps({"state": "active"})),
            )
            reopened.close()

            checked = app_state_db.connect(path)
            row = checked.execute(
                "SELECT payload_json FROM app_documents WHERE document_key = 'named:test'"
            ).fetchone()
            self.assertEqual(json.loads(row[0]), {"state": "active"})
            checked.close()

    def test_database_remains_source_of_truth_when_export_is_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "app_state.sqlite3"
            export = root / "accounts.json"
            with patch.object(app_state_db, "APP_STATE_DB_PATH", database):
                app_state_db.set_document(export, [{"id": 1, "state": "active"}])
                export.write_text('[{"id": 99, "state": "tampered"}]', encoding="utf-8")

                self.assertEqual(
                    app_state_db.get_document(export, []),
                    [{"id": 1, "state": "active"}],
                )

    def test_missing_database_document_does_not_import_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "app_state.sqlite3"
            export = root / "注册任务.json"
            export.write_text('[{"id": 1, "status": "running"}]', encoding="utf-8")

            with patch.object(app_state_db, "APP_STATE_DB_PATH", database):
                self.assertEqual(app_state_db.get_document(export, []), [])

    def test_rewriting_unchanged_document_preserves_update_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "app_state.sqlite3"
            export = root / "accounts.json"
            payload = [{"id": 1, "state": "active"}]

            with patch.object(app_state_db, "APP_STATE_DB_PATH", database):
                app_state_db.set_document(export, payload)
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "UPDATE app_documents SET updated_at = ?",
                        ("2000-01-01 00:00:00",),
                    )
                    connection.commit()
                finally:
                    connection.close()
                app_state_db.set_document(export, payload)

                connection = sqlite3.connect(database)
                try:
                    updated_at = connection.execute(
                        "SELECT updated_at FROM app_documents"
                    ).fetchone()[0]
                finally:
                    connection.close()

            self.assertEqual(updated_at, "2000-01-01 00:00:00")


if __name__ == "__main__":
    unittest.main()
