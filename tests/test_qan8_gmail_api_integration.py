import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import email as email_config
from config import register as register_config
from core import db, email_provider, registration_service
from webui import config_editor
from webui.app import create_app
from webui.email_source_validation import validate_email_sources
from webui.registration_jobs_api import create_registration_jobs


class Qan8GmailApiProviderIntegrationTests(unittest.TestCase):
    def test_parse_sources_accepts_qan8_provider(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook, qan8_gmail_api, outlook"),
            ["outlook", "qan8_gmail_api"],
        )

    @patch("core.email_provider.Qan8GmailApiAllocator", create=True)
    def test_acquire_uses_the_job_lane(self, allocator_factory):
        allocator_factory.return_value.acquire_account.return_value = SimpleNamespace(
            email="alias+one@gmail.com"
        )

        email = email_provider.acquire_email(
            job_id=7,
            email_source="qan8_gmail_api",
            qan8_gmail_api_batch_id="batch-1",
            qan8_gmail_api_lane_id=2,
        )

        self.assertEqual(email, "alias+one@gmail.com")
        allocator_factory.return_value.acquire_account.assert_called_once_with(
            batch_id="batch-1",
            job_id=7,
            lane_id=2,
        )

    @patch("core.email_provider.Qan8GmailApiAllocator", create=True)
    def test_resolve_recognizes_qan8_alias_before_other_pools(self, allocator_factory):
        allocator_factory.return_value.get_account_context.return_value = SimpleNamespace(
            email="alias+one@gmail.com",
            code_url="https://qan8.test/code",
        )

        self.assertEqual(
            email_provider.resolve_email_source("alias+one@gmail.com"),
            "qan8_gmail_api",
        )

    @patch("core.gmail_api_url_client.poll_verification_code", return_value="654321")
    @patch("core.email_provider.Qan8GmailApiAllocator", create=True)
    @patch("core.email_provider.resolve_email_source", return_value="qan8_gmail_api")
    def test_wait_for_otp_reuses_qan8_mailbox_and_stale_guard(self, _resolve, allocator_factory, poll):
        allocator_factory.return_value.get_account_context.return_value = SimpleNamespace(
            email="alias+one@gmail.com",
            code_url="https://qan8.test/code",
        )

        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            code = email_provider.wait_for_otp(
                "alias+one@gmail.com",
                after_ts=12.0,
                before_code="111111",
            )

        self.assertEqual(code, "654321")
        self.assertEqual(poll.call_args.args[0].email, "alias+one@gmail.com")
        self.assertEqual(poll.call_args.args[0].code_url, "https://qan8.test/code")
        self.assertEqual(poll.call_args.kwargs["after_ts"], 12.0)
        self.assertEqual(poll.call_args.kwargs["before_code"], "111111")

    def test_qan8_alias_persists_otp_in_canonical_source_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            patches = (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", root / "gmail.json"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", root / "gmail.txt"),
            )
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            source_url = "https://qan8.test/source-code"
            db.import_gmail_api_url_emails(
                [{"email": "source@gmail.com", "code_url": source_url}]
            )

            response = SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 0, "data": {"code": "246810"}},
            )
            allocator = SimpleNamespace(
                get_account_context=lambda _email: SimpleNamespace(
                    email="alias+one@gmail.com",
                    code_url=source_url,
                )
            )
            with (
                patch.object(email_config, "USE_EMAIL_SERVICE", True),
                patch.object(email_provider, "resolve_email_source", return_value="qan8_gmail_api"),
                patch.object(email_provider, "Qan8GmailApiAllocator", return_value=allocator),
                patch("core.gmail_api_url_client.requests.get", return_value=response),
            ):
                code = email_provider.wait_for_otp(
                    "alias+one@gmail.com",
                    after_ts=123.0,
                )
                email_provider.acknowledge_verification_code(
                    "alias+one@gmail.com",
                    code,
                    stage="registration_email_otp",
                )

            self.assertEqual(code, "246810")
            self.assertEqual(db.get_gmail_api_url_last_otp(source_url), "246810")

    @patch("core.email_provider.Qan8GmailApiAllocator", create=True)
    @patch("core.email_provider.resolve_email_source", return_value="qan8_gmail_api")
    def test_release_and_consume_use_qan8_assignment_state(self, _resolve, allocator_factory):
        allocator = allocator_factory.return_value

        self.assertTrue(
            email_provider.release_email_if_unconsumed(
                "alias+one@gmail.com", note="registration failed"
            )
        )
        self.assertTrue(email_provider.mark_email_consumed("alias+one@gmail.com"))

        allocator.release_account.assert_any_call(
            "alias+one@gmail.com", status="available", reason="registration failed"
        )
        allocator.release_account.assert_any_call(
            "alias+one@gmail.com", status="used", reason=""
        )

    @patch("core.email_provider.Qan8GmailApiAllocator", create=True)
    @patch("core.outlook_client.pick_account")
    def test_explicit_qan8_source_does_not_fallback(self, _outlook, allocator_factory):
        allocator_factory.return_value.acquire_account.side_effect = RuntimeError("lane busy")

        with self.assertRaisesRegex(RuntimeError, "qan8_gmail_api"):
            email_provider.acquire_email(
                job_id=7,
                email_source="qan8_gmail_api",
                qan8_gmail_api_batch_id="batch-1",
                qan8_gmail_api_lane_id=0,
            )

        _outlook.assert_not_called()


class Qan8GmailApiRegistrationIntegrationTests(unittest.TestCase):
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
    @patch("core.email_provider.acquire_email", return_value="alias+one@gmail.com")
    def test_prepare_registration_args_forwards_batch_and_lane(
        self, acquire_email, _birthday
    ):
        registration_service._set_job_email_inputs(
            17,
            [],
            email_source="qan8_gmail_api",
            qan8_gmail_api_batch_id="batch-1",
            qan8_gmail_api_lane_id=2,
        )
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            result = registration_service._prepare_registration_args(job_id=17)

        self.assertEqual(result, ("alias+one@gmail.com", "Alice", "1990-01-01"))
        acquire_email.assert_called_once_with(
            job_id=17,
            email_source="qan8_gmail_api",
            qan8_gmail_api_batch_id="batch-1",
            qan8_gmail_api_lane_id=2,
        )

    @patch("core.qan8_gmail_api_allocator.Qan8GmailApiAllocator")
    def test_submit_creates_one_lazy_batch_and_persists_lane_per_job(self, allocator_factory):
        allocator_factory.return_value.create_batch.return_value = {
            "batch_id": "batch-1",
            "effective_workers": 3,
        }

        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(
            registration_service, "get_executor", return_value=ImmediateExecutor()
        ), patch.object(registration_service, "get_executor_workers", return_value=3):
            jobs = registration_service.submit_registration(
                count=5,
                workers=3,
                email_source="qan8_gmail_api",
                qan8_aliases_per_source=12,
            )

        allocator_factory.return_value.create_batch.assert_called_once_with(
            5, requested_workers=3, aliases_per_source=12
        )
        self.assertEqual(len(submitted), 5)
        self.assertEqual(
            [
                (
                    job["provider_context"]["qan8_gmail_api_lane_id"],
                    job["provider_context"]["qan8_gmail_api_batch_id"],
                )
                for job in jobs
            ],
            [(0, "batch-1"), (1, "batch-1"), (2, "batch-1"), (0, "batch-1"), (1, "batch-1")],
        )

    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="qan8_gmail_api")
    @patch("main.run_registration", return_value={"success": True, "email": "alias+one@gmail.com"})
    def test_successful_registration_consumes_the_qan8_alias(
        self, _run_registration, resolve_source, mark_consumed
    ):
        job = db.create_job(
            email_source="qan8_gmail_api",
            provider_context={
                "qan8_gmail_api_batch_id": "batch-1",
                "qan8_gmail_api_lane_id": 0,
            },
        )
        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("alias+one@gmail.com", "Alice", "1990-01-01"),
        ):
            registration_service._run_one_job(job["id"], job["log_file"])

        resolve_source.assert_called_once_with("alias+one@gmail.com")
        mark_consumed.assert_called_once_with("alias+one@gmail.com")
        self.assertEqual(db.get_job(job["id"])["status"], "success")

    def test_webui_expands_qan8_sources_into_lane_jobs(self):
        service = MagicMock()
        service.submit_registration.return_value = [
            {
                "id": index,
                "provider_context": {
                    "qan8_gmail_api_batch_id": "batch-1",
                    "qan8_gmail_api_lane_id": index % 3,
                },
            }
            for index in range(36)
        ]
        service.effective_registration_workers.return_value = 3
        database = MagicMock()

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "qan8_gmail_api"
        ), patch.object(email_config, "QAN8_API_BASE", "https://shop.qan8.com", create=True), patch.object(
            email_config, "QAN8_API_KEY", "key", create=True
        ), patch.object(email_config, "QAN8_GMAIL_SKU_ID", "42", create=True):
            payload, status = create_registration_jobs(
                {
                    "count": 3,
                    "workers": 3,
                    "email_source": "qan8_gmail_api",
                    "qan8_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 36)
        self.assertEqual(
            [
                sum(
                    job["provider_context"]["qan8_gmail_api_lane_id"] == lane
                    for job in payload["jobs"]
                )
                for lane in range(3)
            ],
            [12, 12, 12],
        )
        self.assertEqual(payload["warning"], "")
        self.assertEqual(
            payload["qan8"],
            {
                "batch_id": "batch-1",
                "target_count": 36,
                "effective_workers": 3,
                "aliases_per_source": 12,
                "active_sources": 0,
                "orders_placed": 0,
                "lifetime_sources_purchased": 0,
            },
        )
        self.assertNotIn("api_key", str(payload))
        self.assertNotIn("code_url", str(payload))
        service.submit_registration.assert_called_once_with(
            count=36,
            workers=3,
            email_source="qan8_gmail_api",
            qan8_aliases_per_source=12,
        )
        database.gmail_api_url_email_pool_summary.assert_not_called()

    def test_webui_rejects_qan8_expanded_count_over_limit(self):
        service = MagicMock()
        database = MagicMock()

        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "qan8_gmail_api"
        ), patch.object(email_config, "QAN8_API_BASE", "https://shop.qan8.com", create=True), patch.object(
            email_config, "QAN8_API_KEY", "key", create=True
        ), patch.object(email_config, "QAN8_GMAIL_SKU_ID", "156", create=True):
            payload, status = create_registration_jobs(
                {
                    "count": 84,
                    "workers": 3,
                    "email_source": "qan8_gmail_api",
                    "qan8_alias_count": 12,
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 400)
        self.assertIn("1008", payload["error"])
        service.submit_registration.assert_not_called()

    @patch(
        "webui.app.svc.qan8_batch_status",
        return_value={
            "batch_id": "batch-1",
            "target_count": 36,
            "effective_workers": 3,
            "aliases_per_source": 12,
            "active_sources": 3,
            "orders_placed": 3,
            "lifetime_sources_purchased": 3,
            "remaining_aliases": 24,
        },
    )
    def test_qan8_status_endpoint_is_authenticated_and_secret_free(self, status):
        client = create_app(auth_code="test-auth").test_client()

        unauthorized = client.get("/api/qan8/batches/batch-1")
        self.assertIn(unauthorized.status_code, {401, 403})
        status.assert_not_called()

        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        response = client.get("/api/qan8/batches/batch-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["active_sources"], 3)
        self.assertNotIn("api_key", str(response.get_json()))
        self.assertNotIn("code_url", str(response.get_json()))
        status.assert_called_once_with("batch-1")

    def test_qan8_validation_requires_credentials_but_does_not_call_api(self):
        config = SimpleNamespace(
            QAN8_API_BASE="https://shop.qan8.com",
            QAN8_API_KEY="",
            QAN8_GMAIL_SKU_ID="42",
        )
        self.assertIn("API Key", validate_email_sources(["qan8_gmail_api"], config))
        config.QAN8_API_KEY = "key"
        config.QAN8_GMAIL_SKU_ID = ""
        self.assertIn("SKU", validate_email_sources(["qan8_gmail_api"], config))

    def test_qan8_configuration_and_registration_controls_are_exposed(self):
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["QAN8_API_KEY"]["storage"], "sqlite")
        self.assertTrue(fields["QAN8_API_KEY"]["secret"])
        self.assertEqual(fields["QAN8_GMAIL_SKU_ID"]["storage"], "sqlite")
        self.assertEqual(fields["QAN8_ORDER_TIMEOUT"]["type"], "int")

        source = (
            Path(__file__).resolve().parent.parent / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('value="qan8_gmail_api"', source)
        self.assertIn('id="qan8AliasCountV2"', source)
        self.assertIn("qan8_alias_count", source)
        self.assertIn("Mỗi luồng = một lane", source)


if __name__ == "__main__":
    unittest.main()
