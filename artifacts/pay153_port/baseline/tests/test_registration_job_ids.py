import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class RegistrationJobIdTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        patches = (
            patch.object(db, "_JOBS_JSON", root / "jobs.json"),
            patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            patch.object(db, "_LOG_DIR", root / "logs"),
        )
        for one in patches:
            one.start()
            self.addCleanup(one.stop)

    def test_deleted_job_id_is_not_reused(self):
        first = db.create_job(email_source="qan8_gmail_api")
        self.assertTrue(db.delete_job(first["id"]))

        second = db.create_job(email_source="qan8_gmail_api")

        self.assertEqual(second["id"], first["id"] + 1)

    def test_new_job_id_skips_numeric_qan8_assignment_history(self):
        first = db.create_job(email_source="qan8_gmail_api")
        database = db._active_sqlite_path()
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE qan8_assignments (job_id TEXT NOT NULL UNIQUE)"
            )
            connection.execute(
                "INSERT INTO qan8_assignments(job_id) VALUES (?)", ("827",)
            )
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(db.delete_job(first["id"]))
        next_job = db.create_job(email_source="qan8_gmail_api")

        self.assertEqual(next_job["id"], 828)


if __name__ == "__main__":
    unittest.main()
