import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from core import db, registration_service


class PaymeshRegistrationServiceTests(unittest.TestCase):
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

    @patch("core.cdk_inventory_store.CdkInventoryStore.import_cdk")
    def test_submit_persists_round_robin_inventory_assignment_per_job(self, import_cdk):
        inventory_ids = [f"inventory-{index}" for index in range(1, 6)]
        import_cdk.side_effect = [
            (type("Record", (), {"inventory_id": inventory_id})(), True)
            for inventory_id in inventory_ids
        ]
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service, "get_executor_workers", return_value=3
        ), patch.object(email_config, "PAYMESH_ACCOUNTS_PER_CDK", 3):
            jobs = registration_service.submit_registration(
                count=30,
                workers=3,
                email_source="paymesh",
                paymesh_cdks=["CARD-1", "CARD-2", "CARD-3", "CARD-4", "CARD-5"],
                paymesh_routed_domains=["googlemail.com"],
            )

        assigned = [
            db.get_job(job["id"])["provider_context"]["paymesh_inventory_id"]
            for job in jobs
        ]
        self.assertEqual(assigned[:10], inventory_ids * 2)
        self.assertEqual(assigned.count("inventory-5"), 6)
        self.assertEqual(len(submitted), 30)
        for job in jobs:
            persisted = db.get_job(job["id"])
            self.assertNotIn("CARD-1", repr(persisted))

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="user@example.com")
    def test_prepare_registration_args_recovers_assigned_inventory_after_cache_clear(
        self, acquire_email, _birthday
    ):
        job = db.create_job(
            email_source="paymesh",
            provider_context={
                "paymesh_inventory_id": "inventory-4",
                "paymesh_routed_domains": ["googlemail.com"],
            },
        )
        registration_service._JOB_EMAIL_INPUTS.clear()

        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            registration_service._prepare_registration_args(job_id=job["id"])

        acquire_email.assert_called_once_with(
            job_id=job["id"],
            paymesh_inventory_id="inventory-4",
            paymesh_routed_domains=["googlemail.com"],
            email_source="paymesh",
        )

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="user@example.com")
    def test_configured_source_uses_persisted_paymesh_assignment(
        self, acquire_email, _birthday
    ):
        job = db.create_job(
            email_source="paymesh",
            provider_context={"paymesh_inventory_id": "inventory-5"},
        )
        registration_service._set_job_email_inputs(
            job["id"],
            [],
            email_source=None,
            paymesh_inventory_id="inventory-5",
        )

        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            registration_service._prepare_registration_args(job_id=job["id"])

        acquire_email.assert_called_once_with(
            job_id=job["id"],
            gmail_cdks=[],
            paymesh_inventory_id="inventory-5",
        )

    def test_retry_job_preserves_assigned_paymesh_inventory(self):
        source = db.create_job(
            email_source="paymesh",
            provider_context={
                "paymesh_inventory_id": "inventory-5",
                "paymesh_routed_domains": ["googlemail.com"],
            },
        )
        db.update_job(source["id"], status="failed", error="registration failed")
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


if __name__ == "__main__":
    unittest.main()
