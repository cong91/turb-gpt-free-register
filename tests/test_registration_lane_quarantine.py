import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import email as email_config
from core import email_provider, registration_service
from core.gmail_api_url_client import GmailApiUrlError


class RegistrationLaneQuarantineTests(unittest.TestCase):
    def setUp(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def tearDown(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def test_gmail_api_url_602_does_not_cancel_jobs_sharing_proxy_lane(self):
        jobs = [
            {
                "id": 10,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "proxy_lane_id": 0,
                },
            },
            {
                "id": 11,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "proxy_lane_id": 0,
                },
            },
            {
                "id": 12,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "proxy_lane_id": 1,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.add(10)
        registration_service._STOP_EVENTS[10] = threading.Event()

        with (
            patch.object(registration_service.db, "get_job", return_value=jobs[0]),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch(
                "core.gmail_api_url_client.quarantine_code_url",
                return_value=2,
            ) as quarantine_url,
        ):
            result = registration_service.quarantine_provider_lane(
                job_id=10,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        quarantine_url.assert_called_once_with(
            "batch-1",
            "https://mail.example/broken",
            reason="Provider error code=602",
        )
        self.assertFalse(registration_service._STOP_EVENTS[10].is_set())
        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(result["stopping"], 0)
        update_job.assert_not_called()

    def test_qan8_602_quarantines_provider_lane_and_cancels_its_jobs(self):
        jobs = [
            {
                "id": 20,
                "status": "running",
                "email_source": "qan8_gmail_api",
                "provider_context": {
                    "qan8_gmail_api_batch_id": "batch-2",
                    "qan8_gmail_api_lane_id": 1,
                },
            },
            {
                "id": 21,
                "status": "pending",
                "email_source": "qan8_gmail_api",
                "provider_context": {
                    "qan8_gmail_api_batch_id": "batch-2",
                    "qan8_gmail_api_lane_id": 1,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.add(20)
        registration_service._STOP_EVENTS[20] = threading.Event()

        with (
            patch.object(registration_service.db, "get_job", return_value=jobs[0]),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch(
                "core.qan8_gmail_api_allocator.Qan8GmailApiAllocator.quarantine_lane",
                return_value=1,
            ) as quarantine_lane,
        ):
            result = registration_service.quarantine_provider_lane(
                job_id=20,
                source="qan8_gmail_api",
                code_url="https://mail.example/broken",
                provider_batch_id="batch-2",
                provider_lane_id=1,
                reason="Provider error code=602",
            )

        quarantine_lane.assert_called_once_with(
            "batch-2", 1, reason="Provider error code=602"
        )
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["stopping"], 1)
        self.assertEqual({call.args[0] for call in update_job.call_args_list}, {20, 21})

    def test_gmail_api_url_without_batch_does_not_cancel_same_proxy_lane(self):
        jobs = [
            {
                "id": 30,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {"proxy_lane_id": 0},
            },
            {
                "id": 31,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"proxy_lane_id": 0},
            },
        ]
        registration_service._ACTIVE_JOBS.add(30)
        registration_service._STOP_EVENTS[30] = threading.Event()

        with (
            patch.object(registration_service.db, "get_job", return_value=jobs[0]),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service.quarantine_provider_lane(
                job_id=30,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(result["stopping"], 0)
        update_job.assert_not_called()

    def test_both_url_providers_quarantine_on_code_602(self):
        cases = (
            (
                "gmail_api_url",
                SimpleNamespace(email="alias@gmail.com", code_url="https://mail.example/gmail"),
                {},
            ),
            (
                "qan8_gmail_api",
                SimpleNamespace(
                    email="alias@gmail.com",
                    code_url="https://mail.example/qan8",
                    batch_id="batch-qan8",
                    lane_id=2,
                ),
                {"provider_batch_id": "batch-qan8", "provider_lane_id": 2},
            ),
        )

        for source, account, provider_ids in cases:
            with (
                self.subTest(source=source),
                patch.object(email_config, "USE_EMAIL_SERVICE", True),
                patch.object(email_provider, "resolve_email_source", return_value=source),
                patch.object(email_provider, "_get_code_url_account", return_value=account),
                patch(
                    "core.gmail_api_url_client.poll_verification_code",
                    side_effect=GmailApiUrlError("Provider error code=602: expired"),
                ),
                patch.object(
                    registration_service,
                    "quarantine_provider_lane",
                ) as quarantine,
                patch.object(registration_service._THREAD_CTX, "job_id", 77, create=True),
                self.assertRaises(GmailApiUrlError),
            ):
                email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0)

                quarantine.assert_called_once_with(
                    job_id=77,
                    source=source,
                    code_url=account.code_url,
                    provider_batch_id=provider_ids.get("provider_batch_id"),
                    provider_lane_id=provider_ids.get("provider_lane_id"),
                    reason="Provider error code=602: expired",
                )

    def test_timeout_does_not_quarantine_url_lane(self):
        account = SimpleNamespace(email="alias@gmail.com", code_url="https://mail.example/code")
        with (
            patch.object(email_config, "USE_EMAIL_SERVICE", True),
            patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"),
            patch.object(email_provider, "_get_code_url_account", return_value=account),
            patch(
                "core.gmail_api_url_client.poll_verification_code",
                side_effect=GmailApiUrlError("Timeout after 60s waiting for new OTP"),
            ),
            patch.object(registration_service, "quarantine_provider_lane") as quarantine,
            patch.object(registration_service._THREAD_CTX, "job_id", 77, create=True),
            self.assertRaisesRegex(GmailApiUrlError, "Timeout"),
        ):
            email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0)

        quarantine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
