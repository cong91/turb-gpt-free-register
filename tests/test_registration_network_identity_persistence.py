# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_service


class RegistrationNetworkIdentityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for target, value in (
            ("_JOBS_JSON", root / "jobs.json"),
            ("_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            ("_LOG_DIR", root / "logs"),
        ):
            patcher = patch.object(db, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, result):
        job = db.create_job(email_source="paymesh")
        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("user@example.com", "Test User", "1990-01-01"),
        ), patch("main.run_registration", return_value=result), patch.object(
            registration_service, "_release_unconsumed_job_email"
        ), patch.object(registration_service, "_disable_job_email"):
            registration_service._run_one_job(job["id"], job["log_file"])
        return db.get_job(job["id"])

    def test_success_job_persists_network_identity(self):
        identity = {"browser_egress_ip": "8.8.8.8", "verified": True}
        job = self._run({
            "success": True,
            "email": "user@example.com",
            "account_id": 17,
            "network_identity": identity,
        })

        self.assertEqual(job["status"], "success")
        self.assertEqual(job["network_identity"], identity)

    def test_failed_job_persists_network_identity(self):
        identity = {"tunnel_egress_ip": "8.8.8.8", "verified": False}
        job = self._run({
            "success": False,
            "email": "user@example.com",
            "error": "browser mismatch",
            "network_identity": identity,
        })

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["network_identity"], identity)


if __name__ == "__main__":
    unittest.main()
