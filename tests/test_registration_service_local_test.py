# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_service


class LocalTestRegistrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.jobs_path = root / "jobs.json"
        self.logs_path = root / "logs"
        self.path_patches = (
            patch.object(db, "_JOBS_JSON", self.jobs_path),
            patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            patch.object(db, "_LOG_DIR", self.logs_path),
        )
        for one in self.path_patches:
            one.start()
            self.addCleanup(one.stop)

    def test_db_persists_local_test_job_type_and_alias(self):
        job = db.create_job(
            email_source="reserved_test",
            job_type="local_test",
            email="sampleuser@mail.test",
        )

        reopened = db.get_job(job["id"])
        self.assertEqual(reopened["job_type"], "local_test")
        self.assertEqual(reopened["email_source"], "reserved_test")
        self.assertEqual(reopened["email"], "sampleuser@mail.test")

    def test_submit_local_test_registration_creates_typed_jobs(self):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service,
            "get_executor_workers",
            return_value=2,
        ):
            jobs = registration_service.submit_local_test_registration(
                aliases=["sampleuser@mail.test", "sampleuser@inbox.invalid"],
                workers=2,
            )

        self.assertEqual(len(jobs), 2)
        self.assertEqual([job["job_type"] for job in jobs], ["local_test", "local_test"])
        self.assertEqual([job["email"] for job in jobs], [
            "sampleuser@mail.test",
            "sampleuser@inbox.invalid",
        ])
        self.assertEqual(len(submitted), 2)
        self.assertTrue(all(item[0] is registration_service._run_one_job for item in submitted))

    @patch("core.email_provider.acquire_email")
    @patch("main.run_registration")
    def test_local_test_worker_completes_without_live_registration(
        self,
        run_registration,
        acquire_email,
    ):
        job = db.create_job(
            email_source="reserved_test",
            job_type="local_test",
            email="sampleuser@mail.test",
        )

        registration_service._run_one_job(job["id"], job["log_file"])

        completed = db.get_job(job["id"])
        self.assertEqual(completed["status"], "success")
        self.assertEqual(completed["email"], "sampleuser@mail.test")
        self.assertIsNone(completed.get("account_id"))
        run_registration.assert_not_called()
        acquire_email.assert_not_called()
        log_text = Path(job["log_file"]).read_text(encoding="utf-8")
        self.assertIn("local test", log_text.lower())
        self.assertIn("dry-run", log_text.lower())

    @patch("core.registration_service._release_unconsumed_job_email")
    def test_stopping_orphaned_local_job_does_not_release_provider_email(self, release_email):
        job = db.create_job(
            email_source="reserved_test",
            job_type="local_test",
            email="sampleuser@mail.test",
        )
        db.update_job(job["id"], status="running")

        result = registration_service.request_stop_job(job["id"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(db.get_job(job["id"])["status"], "stopped")
        release_email.assert_not_called()

    def test_local_test_terminal_job_is_not_retryable(self):
        info = registration_service.get_retry_info({
            "id": 9,
            "job_type": "local_test",
            "status": "failed",
            "email": "sampleuser@mail.test",
        })

        self.assertFalse(info["retryable"])
        self.assertIn("local", info["retry_reason"].lower())

    def test_unsupported_email_is_a_terminal_email_pool_failure(self):
        self.assertTrue(
            registration_service._should_disable_failed_registration_email(
                "RuntimeError: about-you 提交失败：This email is not supported."
            )
        )

    def test_deactivated_account_is_a_terminal_email_pool_failure(self):
        self.assertTrue(
            registration_service._should_disable_failed_registration_email(
                "AccountUnusableError: 账号已废（account_deactivated）"
            )
        )


if __name__ == "__main__":
    unittest.main()
