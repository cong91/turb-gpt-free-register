import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import email as email_config
from config import proxy as proxy_config
from core import app_state_db, db, registration_service
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.gmail_batch_store_base import GmailBatchError
from webui.registration_jobs_api import create_registration_jobs


class GmailApiUrlRegistrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        state_path = root / "turb.sqlite3"
        patches = (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
            patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
            patch.object(db, "_JOBS_JSON", root / "jobs.json"),
            patch.object(db, "_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            patch.object(db, "_LOG_DIR", root / "logs"),
            patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", root / "gmail-pool.json"),
            patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", root / "gmail-pool.txt"),
            patch.object(db, "_SQLITE_PATH", state_path),
            patch.object(db, "_DEFAULT_SQLITE_PATH", state_path),
            patch.object(db, "_SQLITE_READY", False),
            patch.object(db, "_SQLITE_READY_PATH", None),
            patch.object(app_state_db, "APP_STATE_DB_PATH", state_path),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        registration_service._JOB_EMAIL_INPUTS.clear()
        self.addCleanup(registration_service._JOB_EMAIL_INPUTS.clear)

    def _submit_without_workers(self, *, count: int, workers: int = 3):
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(
            registration_service, "get_executor", return_value=ImmediateExecutor()
        ), patch.object(
            registration_service, "get_executor_workers", return_value=workers
        ), patch.object(proxy_config, "ROTATING_PROXY_ENABLED", False):
            jobs = registration_service.submit_registration(
                count=count,
                workers=workers,
                email_source="gmail_api_url",
                gmail_api_url_aliases_per_email=12,
            )
        return jobs, submitted

    def test_webui_source_count_expands_to_jobs_and_records_lane_plan(self):
        jobs, submitted = self._submit_without_workers(count=36, workers=3)

        self.assertEqual(len(jobs), 36)
        self.assertEqual(len(submitted), 3)
        store = GmailApiUrlBatchStore(Path(self.temp_dir.name) / "turb.sqlite3")
        batch_ids = [
            job["provider_context"]["gmail_api_url_batch_id"] for job in jobs
        ]
        self.assertEqual(len(set(batch_ids)), 3)
        self.assertEqual(
            [store.batch_provision_plan(batch_id) for batch_id in dict.fromkeys(batch_ids)],
            [
                {"target_count": 12, "aliases_per_source": 12, "desired_sources": 1},
                {"target_count": 12, "aliases_per_source": 12, "desired_sources": 1},
                {"target_count": 12, "aliases_per_source": 12, "desired_sources": 1},
            ],
        )
        contexts = [job["provider_context"] for job in jobs]
        self.assertTrue(all(context["gmail_api_url_lane_count"] == 3 for context in contexts))
        self.assertEqual(
            [context["gmail_api_url_lane_id"] for context in contexts[:6]],
            [0, 0, 0, 0, 0, 0],
        )
        self.assertEqual(
            [context["gmail_api_url_lane_id"] for context in contexts[12:18]],
            [1, 1, 1, 1, 1, 1],
        )
        self.assertEqual(
            [context["gmail_api_url_lane_id"] for context in contexts[24:30]],
            [2, 2, 2, 2, 2, 2],
        )
        self.assertTrue(all(
            fn is registration_service._run_gmail_api_url_lane
            and len(args) == 1
            and len(args[0]) == 12
            for fn, args in submitted
        ))
        self.assertTrue(all(job["email_source"] == "gmail_api_url" for job in jobs))

    def test_lane_plan_preserves_source_groups_when_jobs_exceed_workers(self):
        positions, sources = registration_service._plan_gmail_api_url_lanes(60, 3, 12)

        self.assertEqual([len(lane) for lane in positions], [24, 24, 12])
        self.assertEqual(sources, [2, 2, 1])
        self.assertEqual(sorted(index for lane in positions for index in lane), list(range(60)))

    def test_submit_creates_one_batch_per_lane_for_five_source_groups(self):
        jobs, submitted = self._submit_without_workers(count=60, workers=3)
        store = GmailApiUrlBatchStore(Path(self.temp_dir.name) / "turb.sqlite3")

        lane_batches = {}
        for job in jobs:
            context = job["provider_context"]
            lane_batches.setdefault(context["gmail_api_url_lane_id"], set()).add(
                context["gmail_api_url_batch_id"]
            )
        self.assertEqual(
            {lane: len(batch_ids) for lane, batch_ids in lane_batches.items()},
            {0: 1, 1: 1, 2: 1},
        )
        plans = [
            store.batch_provision_plan(next(iter(lane_batches[lane])))
            for lane in range(3)
        ]
        self.assertEqual(
            plans,
            [
                {"target_count": 24, "aliases_per_source": 12, "desired_sources": 2},
                {"target_count": 24, "aliases_per_source": 12, "desired_sources": 2},
                {"target_count": 12, "aliases_per_source": 12, "desired_sources": 1},
            ],
        )
        self.assertEqual([len(args[0]) for _fn, args in submitted], [24, 24, 12])

    def test_gmail_lane_runner_is_sequential(self):
        order = []

        with patch.object(
            registration_service,
            "_run_one_job",
            side_effect=lambda job_id, _log_file: order.append(job_id),
        ):
            registration_service._run_gmail_api_url_lane(
                [(101, "a.log"), (102, "b.log"), (103, "c.log")]
            )

        self.assertEqual(order, [101, 102, 103])

    def test_legacy_gmail_job_gets_one_lazy_twelve_alias_batch(self):
        job = db.create_job(email_source="gmail_api_url", provider_context={})

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            registration_service, "_random_display_name", return_value="Test User"
        ), patch(
            "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
        ), patch(
            "core.email_provider.acquire_email", return_value="alias@gmail.com"
        ) as acquire_email:
            result = registration_service._prepare_registration_args(job["id"])

        self.assertEqual(result, ("alias@gmail.com", "Test User", "1990-01-01"))
        kwargs = acquire_email.call_args.kwargs
        self.assertEqual(kwargs["email_source"], "gmail_api_url")
        self.assertEqual(kwargs["gmail_api_url_aliases_per_source"], 12)
        batch_id = kwargs["gmail_api_url_batch_id"]
        self.assertEqual(
            GmailApiUrlBatchStore(Path(self.temp_dir.name) / "turb.sqlite3").batch_provision_plan(
                batch_id
            ),
            {
                "target_count": 1,
                "aliases_per_source": 12,
                "desired_sources": 1,
            },
        )

    def test_webui_count_means_source_groups_and_alias_input_is_ignored(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(36)]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_available": 0,
        }

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ), patch.object(email_config, "QAN8_API_BASE", "https://shop.qan8.com"), patch.object(
            email_config, "QAN8_API_KEY", "key"
        ), patch.object(email_config, "QAN8_GMAIL_SKU_ID", "42"):
            payload, status = create_registration_jobs(
                {
                    "count": 3,
                    "workers": 3,
                    "email_source": "gmail_api_url",
                    "gmail_api_url_alias_count": 1,
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

    def test_sub2api_registration_count_is_not_multiplied_by_alias_capacity(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": index} for index in range(30)]
        service.effective_registration_workers.return_value = 5
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_available": 0,
        }
        automation_context = {
            "sub2api_automation_kind": "registration",
            "sub2api_automation_request_id": "request-30",
        }

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ), patch.object(email_config, "QAN8_API_BASE", "https://shop.qan8.com"), patch.object(
            email_config, "QAN8_API_KEY", "key"
        ), patch.object(email_config, "QAN8_GMAIL_SKU_ID", "42"):
            payload, status = create_registration_jobs(
                {
                    "count": 30,
                    "workers": 5,
                    "email_source": "gmail_api_url",
                },
                service=service,
                database=database,
                automation_context=automation_context,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 30)
        service.submit_registration.assert_called_once_with(
            count=30,
            workers=5,
            email_source="gmail_api_url",
            gmail_api_url_aliases_per_email=12,
            automation_context=automation_context,
        )

    def test_webui_requires_purchase_configuration_when_local_aliases_are_insufficient(self):
        service = MagicMock()
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 0,
            "alias_available": 0,
        }

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ), patch.object(email_config, "QAN8_API_BASE", ""), patch.object(
            email_config, "QAN8_API_KEY", ""
        ), patch.object(email_config, "QAN8_GMAIL_SKU_ID", ""):
            payload, status = create_registration_jobs(
                {"count": 1, "workers": 1, "email_source": "gmail_api_url"},
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertIn("shop.qan8.com", payload["error"])
        service.submit_registration.assert_not_called()

    def test_webui_returns_batch_exhaustion_as_bad_request(self):
        service = MagicMock()
        service.submit_registration.side_effect = GmailBatchError(
            "Gmail API URL pool không đủ alias mới"
        )
        database = MagicMock()
        database.gmail_api_url_email_pool_summary.return_value = {
            "available": 1,
            "alias_available": 12,
        }

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_api_url"
        ):
            payload, status = create_registration_jobs(
                {"count": 1, "workers": 1, "email_source": "gmail_api_url"},
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertIn("không đủ alias", payload["error"])

    def test_obsolete_qan8_source_is_rejected_by_webui(self):
        service = MagicMock()
        database = MagicMock()

        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            payload, status = create_registration_jobs(
                {"count": 1, "workers": 1, "email_source": "qan8_gmail_api"},
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "邮箱来源不支持")
        service.submit_registration.assert_not_called()

    def test_qan8_is_not_a_registration_source_but_historical_rows_normalize(self):
        from core.email_provider import is_valid_email_source, normalize_email_source

        self.assertFalse(is_valid_email_source("qan8_gmail_api"))
        self.assertEqual(normalize_email_source("qan8_gmail_api"), "gmail_api_url")


if __name__ == "__main__":
    unittest.main()
