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

    def test_update_job_writes_only_the_target_row(self):
        first = db.create_job(email_source="qan8_gmail_api")
        second = db.create_job(email_source="qan8_gmail_api")

        with (
            patch.object(db, "_load_jobs", side_effect=AssertionError("loaded all jobs")),
            patch.object(db, "_save_jobs", side_effect=AssertionError("rewrote all jobs")),
        ):
            db.update_job(
                first["id"],
                status="running",
                email="first@example.com",
                network_identity={"ip": "203.0.113.5"},
            )

        jobs = {row["id"]: row for row in db.list_jobs(limit=10)}
        self.assertEqual(jobs[first["id"]]["status"], "running")
        self.assertEqual(jobs[first["id"]]["email"], "first@example.com")
        self.assertEqual(jobs[first["id"]]["network_identity"], {"ip": "203.0.113.5"})
        self.assertEqual(jobs[second["id"]]["status"], "pending")

    def test_create_job_does_not_rewrite_existing_jobs(self):
        with patch.object(db, "_load_jobs", side_effect=AssertionError("loaded all jobs")), patch.object(
            db, "_save_jobs", side_effect=AssertionError("rewrote all jobs")
        ):
            created = db.create_job(email_source="qan8_gmail_api")

        self.assertEqual(db.get_job(created["id"])["email_source"], "qan8_gmail_api")

    def test_create_retry_job_does_not_rewrite_existing_jobs(self):
        source = db.create_job(email_source="qan8_gmail_api")
        db.update_job(source["id"], status="failed")

        with patch.object(db, "_load_jobs", side_effect=AssertionError("loaded all jobs")), patch.object(
            db, "_save_jobs", side_effect=AssertionError("rewrote all jobs")
        ):
            retry, created = db.create_retry_job(
                source["id"],
                job_type="registration",
                email_source="qan8_gmail_api",
            )

        self.assertTrue(created)
        self.assertEqual(retry["parent_job_id"], source["id"])


if __name__ == "__main__":
    unittest.main()
