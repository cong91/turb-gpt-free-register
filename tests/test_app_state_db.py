import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app_state_db


class AppStateDbTests(unittest.TestCase):
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
