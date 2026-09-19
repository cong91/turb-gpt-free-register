import threading
import unittest
from unittest.mock import patch

from core import registration_service
from core.bamboommo_client import BambooMmoError


class RegistrationProviderStopTests(unittest.TestCase):
    def setUp(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def tearDown(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def test_terminal_qan8_checkout_failure_stops_the_submitted_batch(self):
        batch_id = "batch-stop"
        jobs = [
            {
                "id": 2124,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": batch_id},
            },
            {
                "id": 2136,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": batch_id},
            },
            {
                "id": 2148,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": batch_id},
            },
            {
                "id": 9999,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "other-batch"},
            },
        ]
        registration_service._ACTIVE_JOBS.update({2124, 2148})
        registration_service._STOP_EVENTS[2124] = threading.Event()
        registration_service._STOP_EVENTS[2148] = threading.Event()

        with (
            patch.object(registration_service.db, "get_job", return_value=jobs[0]),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                2124,
                "QAN8 HTTP 429: {'code': 'CHECKOUT_BLOCKED', 'message': 'Request blocked by security policy'}",
            )

        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["stopping"], 1)
        self.assertFalse(registration_service._STOP_EVENTS[2124].is_set())
        self.assertTrue(registration_service._STOP_EVENTS[2148].is_set())
        self.assertEqual(
            [call.args[0] for call in update_job.call_args_list],
            [2136, 2148],
        )
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_non_terminal_provider_failure_does_not_stop_a_batch(self):
        current = {
            "id": 1,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-1"},
        }
        event = threading.Event()
        registration_service._STOP_EVENTS[1] = event
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs") as list_jobs,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                1,
                "QAN8 HTTP 503: temporary upstream failure",
            )

        self.assertEqual(result, {"matched": 0, "cancelled": 0, "stopping": 0})
        self.assertFalse(event.is_set())
        list_jobs.assert_not_called()

    def test_out_of_stock_stops_pending_jobs(self):
        current = {
            "id": 7,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-stock"},
        }
        jobs = [
            current,
            {
                "id": 8,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "batch-stock"},
            },
        ]
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                7,
                "QAN8 HTTP 409: code=OUT_OF_STOCK; stock unavailable",
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["stopped"], 0)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_insufficient_balance_stops_pending_gmail_api_url_jobs(self):
        current = {
            "id": 31,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-balance"},
        }
        jobs = [
            current,
            {
                "id": 32,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "batch-balance"},
            },
        ]
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                31,
                "QAN8 HTTP 409: {'code': 'INSUFFICIENT_BALANCE', 'message': 'Insufficient API member balance'}",
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_insufficient_balance_stops_otpmail_batch(self):
        current = {
            "id": 41,
            "status": "running",
            "email_source": "otpmail",
            "provider_context": {"registration_batch_id": "batch-otp"},
        }
        jobs = [
            current,
            {
                "id": 42,
                "status": "pending",
                "email_source": "otpmail",
                "provider_context": {"registration_batch_id": "batch-otp"},
            },
        ]
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                41,
                "OTPGmail 请求失败: HTTP 400; Insufficient balance, please recharge",
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_insufficient_balance_stops_bamboommo_batch(self):
        current = {
            "id": 51,
            "status": "running",
            "email_source": "bamboommo",
            "provider_context": {"registration_batch_id": "batch-bamboo"},
        }
        jobs = [
            current,
            {
                "id": 52,
                "status": "pending",
                "email_source": "bamboommo",
                "provider_context": {"registration_batch_id": "batch-bamboo"},
            },
        ]
        error = BambooMmoError(
            "BambooMMO API error ORDER_NOT_ENOUGH_MONEY: Tài khoản không đủ số dư",
            resource_key="ORDER_NOT_ENOUGH_MONEY",
        )
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                51,
                error,
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")


    def test_gmail_source_pool_exhaustion_stops_pending_jobs(self):
        current = {
            "id": 61,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-pool"},
        }
        jobs = [
            current,
            {
                "id": 62,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "batch-pool"},
            },
            {
                "id": 63,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "batch-pool"},
            },
        ]
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                61,
                "GmailApiUrlError: No Gmail API URL source available",
            )

        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_email_source_alloc_failure_stops_pending_jobs(self):
        current = {
            "id": 71,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-alloc"},
        }
        jobs = [
            current,
            {
                "id": 72,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"registration_batch_id": "batch-alloc"},
            },
        ]
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                71,
                "RuntimeError: 所有邮箱来源均领取失败: ['gmail_api_url']; last=No Gmail API URL source available",
            )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(update_job.call_args_list[0].kwargs["status"], "cancelled")

    def test_transient_session_timeout_does_not_stop_batch(self):
        current = {
            "id": 81,
            "status": "running",
            "email_source": "gmail_api_url",
            "provider_context": {"registration_batch_id": "batch-session"},
        }
        with (
            patch.object(registration_service.db, "get_job", return_value=current),
            patch.object(registration_service.db, "list_jobs") as list_jobs,
        ):
            result = registration_service._stop_registration_batch_on_provider_failure(
                81,
                "RuntimeError: 等待 /api/auth/session accessToken 超时，最后响应: session 暂无 accessToken",
            )

        self.assertEqual(result, {"matched": 0, "cancelled": 0, "stopping": 0})
        list_jobs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
