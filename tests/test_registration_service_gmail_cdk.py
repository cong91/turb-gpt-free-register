# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from config import proxy as proxy_config
from config import register as register_config
from core import db, registration_service


class GmailCdkRegistrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for one in (
            patch.object(db, "_JOBS_JSON", root / "jobs.json"),
            patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            patch.object(db, "_LOG_DIR", root / "logs"),
        ):
            one.start()
            self.addCleanup(one.stop)
        registration_service._JOB_EMAIL_INPUTS.clear()
        self.addCleanup(registration_service._JOB_EMAIL_INPUTS.clear)

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="abcdef@gmail.com")
    def test_prepare_registration_args_passes_job_cdks_to_email_provider(self, acquire_email, _birthday):
        registration_service._set_job_email_inputs(
            17,
            ["CDK-ONE", "CDK-TWO"],
            email_source="gmail_123452026",
        )
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            email, name, birthday = registration_service._prepare_registration_args(job_id=17)

        self.assertEqual((email, name, birthday), ("abcdef@gmail.com", "Alice", "1990-01-01"))
        acquire_email.assert_called_once_with(
            job_id=17,
            gmail_cdks=["CDK-ONE", "CDK-TWO"],
            email_source="gmail_123452026",
        )
        registration_service._clear_job_email_inputs(17)

    def test_job_email_inputs_are_removed_when_job_deactivates(self):
        registration_service._set_job_email_inputs(
            21,
            ["CDK-ONE"],
            email_source="gmail_123452026",
        )
        registration_service._activate_job(21)

        registration_service._deactivate_job(21)

        self.assertEqual(registration_service._get_job_email_inputs(21), {
            "email_source": None,
            "gmail_cdks": [],
            "gmail_routed_domains": [],
            "gmail_batch_id": None,
            "gmail_api_url_batch_id": None,
            "paymesh_cdks": [],
            "paymesh_routed_domains": [],
        })

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="abcdef@route-one.net")
    def test_prepare_registration_args_passes_routed_domains(self, acquire_email, _birthday):
        registration_service._set_job_email_inputs(
            23,
            ["CDK-ONE"],
            email_source="gmail_123452026",
            gmail_routed_domains=["route-one.net", "route-two.org"],
        )
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            registration_service._prepare_registration_args(job_id=23)

        acquire_email.assert_called_once_with(
            job_id=23,
            gmail_cdks=["CDK-ONE"],
            gmail_routed_domains=["route-one.net", "route-two.org"],
            email_source="gmail_123452026",
        )
        registration_service._clear_job_email_inputs(23)

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="abcdef@route-one.net")
    def test_prepare_registration_args_recovers_batch_after_restart(self, acquire_email, _birthday):
        job = db.create_job(
            email_source="gmail_123452026",
            provider_context={
                "gmail_batch_id": "batch-restart",
                "gmail_routed_domains": ["route-one.net"],
            },
        )
        registration_service._JOB_EMAIL_INPUTS.clear()
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            registration_service._prepare_registration_args(job_id=job["id"])

        acquire_email.assert_called_once_with(
            job_id=job["id"],
            gmail_batch_id="batch-restart",
            gmail_routed_domains=["route-one.net"],
            email_source="gmail_123452026",
        )


    @patch("core.gmail_123452026_client.create_registration_batch", return_value="batch-123")
    def test_submit_persists_batch_context_without_raw_cdks(self, create_batch):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service, "get_executor_workers", return_value=3
        ):
            jobs = registration_service.submit_registration(
                count=2,
                workers=3,
                email_source="gmail_123452026",
                gmail_cdks=["SECRET-ONE", "SECRET-TWO"],
                gmail_routed_domains=["route-one.net"],
            )

        create_batch.assert_called_once_with(
            ["SECRET-ONE", "SECRET-TWO"],
            routed_domains=["route-one.net"],
        )
        self.assertEqual(len(submitted), 2)
        for job in jobs:
            persisted = db.get_job(job["id"])
            self.assertEqual(persisted["provider_context"], {
                "gmail_batch_id": "batch-123",
                "gmail_routed_domains": ["route-one.net"],
            })
            self.assertNotIn("SECRET-ONE", repr(persisted))

    def test_submit_persists_rotating_proxy_lane_per_worker_slot(self):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(proxy_config, "ROTATING_PROXY_ENABLED", True), patch.object(
            registration_service, "get_executor", return_value=ImmediateExecutor()
        ), patch.object(registration_service, "get_executor_workers", return_value=2):
            jobs = registration_service.submit_registration(
                count=4,
                workers=2,
                email_source="outlook",
            )

        self.assertEqual(len(submitted), 4)
        self.assertEqual(
            [db.get_job(job["id"])["provider_context"]["proxy_lane_id"] for job in jobs],
            [0, 1, 0, 1],
        )

    def test_retry_job_copies_persisted_gmail_batch_context(self):
        source = db.create_job(
            email_source="gmail_123452026",
            provider_context={
                "gmail_batch_id": "batch-retry",
                "gmail_routed_domains": ["route-one.net"],
            },
        )
        db.update_job(source["id"], status="failed", error="provider failed")
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()):
            result = registration_service.retry_job(source["id"], workers=1)

        self.assertTrue(result["ok"])
        retried = db.get_job(result["job"]["id"])
        self.assertEqual(retried["provider_context"], source["provider_context"])
        self.assertEqual(len(submitted), 1)

    def test_job_inputs_recover_persisted_batch_context_after_cache_clear(self):
        job = db.create_job(
            email_source="gmail_123452026",
            provider_context={
                "gmail_batch_id": "batch-restart",
                "gmail_routed_domains": ["route-one.net"],
            },
        )
        registration_service._JOB_EMAIL_INPUTS.clear()

        self.assertEqual(registration_service._get_job_email_inputs(job["id"]), {
            "email_source": "gmail_123452026",
            "gmail_cdks": [],
            "gmail_routed_domains": ["route-one.net"],
            "gmail_batch_id": "batch-restart",
            "gmail_api_url_batch_id": None,
            "paymesh_cdks": [],
            "paymesh_routed_domains": [],
        })


if __name__ == "__main__":
    unittest.main()
