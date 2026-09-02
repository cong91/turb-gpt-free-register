import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import email as email_config
from config import proxy as proxy_config
from core import db, registration_service
from webui.registration_jobs_api import create_registration_jobs


class GmailApiUrlRegistrationServiceTests(unittest.TestCase):
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

    @patch("core.gmail_api_url_client.create_registration_batch", return_value="batch-gmail-api")
    def test_submit_creates_one_job_per_requested_alias(self, create_batch):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service, "get_executor_workers", return_value=3
        ), patch.object(proxy_config, "ROTATING_PROXY_ENABLED", False):
            jobs = registration_service.submit_registration(
                count=12,
                workers=3,
                email_source="gmail_api_url",
                gmail_api_url_aliases_per_email=12,
            )

        create_batch.assert_called_once_with(12, aliases_per_email=12)
        self.assertEqual(len(jobs), 12)
        self.assertEqual(len(submitted), 12)
        for job in jobs:
            persisted = db.get_job(job["id"])
            self.assertEqual(
                persisted["provider_context"]["gmail_api_url_batch_id"],
                "batch-gmail-api",
            )
            self.assertFalse(persisted["email"])
            self.assertNotIn("alias", persisted["provider_context"])

    def test_webui_count_one_with_alias_cap_submits_expanded_jobs(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(12)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.outlook_pool_summary.return_value = {"available": 0}
        database.gmail_api_url_email_pool_summary.return_value = {"available": 3}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 1,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 12)
        self.assertEqual(payload["warning"], "")
        service.submit_registration.assert_called_once_with(
            count=12,
            workers=3,
            email_source="gmail_api_url",
            gmail_api_url_aliases_per_email=12,
        )

    def test_webui_count_three_records_expands_all_aliases(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(36)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {"available": 3}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 3,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 36)
        self.assertEqual(payload["warning"], "")
        service.submit_registration.assert_called_once_with(
            count=36,
            workers=3,
            email_source="gmail_api_url",
            gmail_api_url_aliases_per_email=12,
        )

    def test_webui_clamps_requested_records_to_unused_pool(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(36)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {"available": 3}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 5,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 36)
        self.assertEqual(payload["warning"], "")
        service.submit_registration.assert_called_once_with(
            count=36,
            workers=3,
            email_source="gmail_api_url",
            gmail_api_url_aliases_per_email=12,
        )

    def test_webui_rejects_empty_gmail_api_url_pool(self):
        service = MagicMock()
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {"available": 0}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 3,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertIn("không còn bản ghi chưa dùng", payload["error"])
        service.submit_registration.assert_not_called()

    def test_webui_rejects_expanded_job_count_over_limit(self):
        service = MagicMock()
        database = MagicMock()
        database.outlook_pool_summary.return_value = {"available": 0}
        database.gmail_api_url_email_pool_summary.return_value = {"available": 84}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 84,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertIn("1008", payload["error"])
        service.submit_registration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
