import unittest
from unittest.mock import Mock, patch

from core import registration_service


class RegistrationRecoveryTests(unittest.TestCase):
    def test_reconciliation_stops_orphaned_jobs_and_finishes_gmail_assignments(self):
        jobs = [
            {
                "id": 120,
                "status": "running",
                "email_source": "gmail_api_url",
                "email": "unfinished@example.com",
            },
            {
                "id": 121,
                "status": "failed",
                "email_source": "gmail_api_url",
                "email": "saved@example.com",
            },
            {
                "id": 122,
                "status": "failed",
                "email_source": "gmail_api_url",
                "email": "legacy-source@example.com",
            },
            {
                "id": 123,
                "status": "running",
                "email_source": "gmail_api_url",
                "email": "unassigned@example.com",
            },
        ]
        store = Mock()
        store.find_active_assignment_for_job.side_effect = [
            Mock(assignment_id="assignment-120"),
            Mock(assignment_id="assignment-121"),
            Mock(assignment_id="assignment-122"),
            None,
        ]

        with patch.object(registration_service, "_ACTIVE_JOBS", set()), patch.object(
            registration_service.db, "list_jobs", return_value=jobs
        ), patch.object(registration_service.db, "update_job") as update_job, patch.object(
            registration_service, "_account_for_job", side_effect=[None, {"id": 1}, {"id": 2}, None]
        ), patch.object(
            registration_service, "_release_unconsumed_job_email"
        ) as release_email, patch(
            "core.gmail_api_url_batch_store.GmailApiUrlBatchStore",
            return_value=store,
        ):
            result = registration_service.reconcile_interrupted_registration_jobs()

        self.assertEqual(result["stopped_jobs"], 2)
        self.assertEqual(result["failed_gmail_assignments"], 1)
        self.assertEqual(result["completed_gmail_assignments"], 2)
        self.assertEqual(update_job.call_count, 2)
        release_email.assert_called_once_with(
            "unassigned@example.com",
            "WebUI restarted before registration completed",
        )
        store.discard.assert_called_once_with(
            "assignment-120",
            reason="WebUI restarted before registration completed",
        )
        self.assertEqual(store.complete.call_count, 2)
        store.complete.assert_any_call("assignment-121")
        store.complete.assert_any_call("assignment-122")

    def test_reconciliation_keeps_email_for_orphaned_job_with_saved_account(self):
        job = {
            "id": 130,
            "status": "running",
            "email_source": "gmail_api_url",
            "email": "saved@example.com",
        }
        store = Mock()
        store.find_active_assignment_for_job.return_value = None

        with patch.object(registration_service, "_ACTIVE_JOBS", set()), patch.object(
            registration_service.db, "list_jobs", return_value=[job]
        ), patch.object(registration_service.db, "update_job"), patch.object(
            registration_service, "_account_for_job", return_value={"id": 1}
        ), patch.object(
            registration_service, "_release_unconsumed_job_email"
        ) as release_email, patch(
            "core.gmail_api_url_batch_store.GmailApiUrlBatchStore",
            return_value=store,
        ):
            result = registration_service.reconcile_interrupted_registration_jobs()

        self.assertEqual(result["stopped_jobs"], 1)
        release_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
