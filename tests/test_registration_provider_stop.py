import threading
import unittest
from unittest.mock import patch

from core import registration_service


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


if __name__ == "__main__":
    unittest.main()
