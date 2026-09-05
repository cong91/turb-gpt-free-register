import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import email as email_config
from config import proxy as proxy_config
from core import db, registration_service
from core.gmail_batch_store_base import GmailBatchError
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

    @patch("core.gmail_api_url_client.create_registration_batch", return_value="batch-single-alias")
    def test_submit_routes_single_alias_through_canonical_batch(self, create_batch):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service, "get_executor_workers", return_value=2
        ), patch.object(proxy_config, "ROTATING_PROXY_ENABLED", False):
            jobs = registration_service.submit_registration(
                count=2,
                workers=2,
                email_source="gmail_api_url",
                gmail_api_url_aliases_per_email=1,
            )

        create_batch.assert_called_once_with(2, aliases_per_email=1)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(submitted), 2)
        self.assertTrue(
            all(
                job["provider_context"]["gmail_api_url_batch_id"] == "batch-single-alias"
                for job in jobs
            )
        )

    @patch("core.gmail_api_url_client.create_registration_batch", return_value="batch-legacy")
    @patch("core.email_provider.acquire_email", return_value="legacy-alias@gmail.com")
    def test_legacy_job_without_batch_is_upgraded_to_canonical_alias_batch(
        self, acquire_email, create_batch
    ):
        job = db.create_job(email_source="gmail_api_url", provider_context={})

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            registration_service, "_random_display_name", return_value="Test User"
        ), patch(
            "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
        ):
            result = registration_service._prepare_registration_args(job["id"])

        self.assertEqual(result, ("legacy-alias@gmail.com", "Test User", "1990-01-01"))
        create_batch.assert_called_once_with(1, aliases_per_email=1)
        self.assertEqual(
            acquire_email.call_args.kwargs["gmail_api_url_batch_id"],
            "batch-legacy",
        )
        self.assertEqual(
            db.get_job(job["id"])["provider_context"]["gmail_api_url_batch_id"],
            "batch-legacy",
        )

    @patch("core.gmail_api_url_client.create_registration_batch", return_value="batch-fresh")
    @patch(
        "core.email_provider.acquire_email",
        side_effect=[
            RuntimeError("所有邮箱来源均领取失败: ['gmail_api_url']; last=No available Gmail account in batch"),
            "alias+fresh@gmail.com",
        ],
    )
    def test_prepare_refreshes_batch_that_exhausts_during_claim(
        self, acquire_email, create_batch
    ):
        job = db.create_job(
            email_source="gmail_api_url",
            provider_context={
                "gmail_api_url_batch_id": "stale-batch",
                "proxy_lane_id": 1,
            },
        )

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            registration_service, "_random_display_name", return_value="Test User"
        ), patch(
            "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
        ):
            result = registration_service._prepare_registration_args(job["id"])

        self.assertEqual(result, ("alias+fresh@gmail.com", "Test User", "1990-01-01"))
        create_batch.assert_called_once_with(1, aliases_per_email=1)
        self.assertEqual(
            [call.kwargs["gmail_api_url_batch_id"] for call in acquire_email.call_args_list],
            [
                "stale-batch",
                "batch-fresh",
            ],
        )
        self.assertEqual(
            db.get_job(job["id"])["provider_context"]["gmail_api_url_batch_id"],
            "batch-fresh",
        )
        self.assertEqual(db.get_job(job["id"])["provider_context"]["proxy_lane_id"], 1)

    def test_cleanup_allows_gmail_api_url_retry_after_quarantine(self):
        job = db.create_job(email_source="gmail_api_url")

        with patch(
            "core.gmail_api_url_client.has_active_batch_assignment",
            return_value=False,
        ) as has_active:
            allowed = registration_service._registration_cleanup_allows_retry(
                job["id"], cleanup_succeeded=False
            )

        self.assertTrue(allowed)
        has_active.assert_called_once_with(job["id"])

    def test_webui_count_one_with_alias_cap_submits_expanded_jobs(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(12)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.outlook_pool_summary.return_value = {"available": 0}
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 3,
            "alias_available": 36,
        }

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
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 3,
            "alias_available": 36,
        }

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

    def test_webui_alias_count_one_uses_remaining_alias_capacity(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(2)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 5,
            "alias_available": 2,
        }

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {
                    "count": 5,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 1,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 2)
        self.assertEqual(payload["warning"], "")
        service.submit_registration.assert_called_once_with(
            count=2,
            workers=3,
            email_source="gmail_api_url",
        )

    def test_webui_count_three_records_uses_remaining_alias_capacity(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(36)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_source_available": 11,
            "alias_available": 176,
        }

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
        service.submit_registration.assert_called_once_with(
            count=36,
            workers=3,
            email_source="gmail_api_url",
            gmail_api_url_aliases_per_email=12,
        )

    def test_webui_returns_batch_exhaustion_as_bad_request(self):
        service = MagicMock()
        service.submit_registration.side_effect = GmailBatchError(
            "Gmail API URL pool không đủ alias mới"
        )
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_available": 12,
        }

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

        self.assertEqual(status, 400)
        self.assertIn("không đủ alias", payload["error"])
        service.effective_registration_workers.assert_not_called()

    def test_webui_clamps_requested_records_to_unused_pool(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(36)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 3,
            "alias_available": 36,
        }

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
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_available": 0,
        }

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
        self.assertIn("không còn alias khả dụng", payload["error"])
        service.submit_registration.assert_not_called()

    def test_webui_rejects_expanded_job_count_over_limit(self):
        service = MagicMock()
        database = MagicMock()
        database.outlook_pool_summary.return_value = {"available": 0}
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 84,
            "alias_available": 1008,
        }

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
