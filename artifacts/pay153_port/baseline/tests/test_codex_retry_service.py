import unittest
from unittest.mock import patch

from core import codex_retry_service


class CodexRetryServiceTests(unittest.TestCase):
    def test_reconcile_marks_persisted_retrying_without_live_worker_as_failed(self):
        rows = [
            {"email": "live@example.com", "codex_status": "retrying"},
            {"email": "stale@example.com", "codex_status": "retrying"},
            {"email": "done@example.com", "codex_status": "success"},
        ]

        with (
            patch.object(codex_retry_service, "active_retrying_emails", return_value={"live@example.com"}),
            patch.object(codex_retry_service.db, "list_accounts", return_value=rows),
            patch.object(codex_retry_service.db, "update_account_codex_status", return_value=True) as update,
        ):
            result = codex_retry_service.reconcile_persisted_retrying_statuses()

        self.assertEqual(result, {"reset": 1, "active": 1})
        update.assert_called_once_with(
            "stale@example.com",
            "failed",
            "WebUI khởi động lại; trạng thái retrying cũ không còn worker",
        )


if __name__ == "__main__":
    unittest.main()
