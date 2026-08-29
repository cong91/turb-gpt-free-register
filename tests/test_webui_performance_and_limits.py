# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from config import email as email_config
from core import db
from webui.registration_jobs_api import create_registration_jobs
from webui.app import create_app


class WebUiPerformanceAndLimitsTests(unittest.TestCase):
    def test_paged_jobs_enriches_only_visible_rows(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        rows = [{"id": index, "status": "failed", "job_type": "registration"} for index in range(100)]
        with patch("webui.app.db.list_jobs", return_value=rows), patch(
            "webui.app.svc.get_retry_info", return_value={"retryable": False}
        ) as get_retry_info:
            response = client.get("/api/jobs?paged=1&page=1&page_size=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 10)
        self.assertEqual(get_retry_info.call_count, 10)

    @patch("core.db._render_static_viewer")
    @patch("core.db._schedule_static_viewer_refresh")
    @patch("core.db._sync_tokens_txt")
    @patch("core.db._sync_accounts_txt")
    @patch("core.db._write_json")
    def test_account_save_defers_static_viewer_render(
        self,
        write_json,
        sync_accounts,
        sync_tokens,
        schedule_refresh,
        render_viewer,
    ):
        db._save_accounts([{"id": 1, "email": "user@example.com"}])

        schedule_refresh.assert_called_once_with("save_accounts")
        render_viewer.assert_not_called()
        write_json.assert_called_once()
        sync_accounts.assert_called_once()
        sync_tokens.assert_called_once()

    def test_registration_job_count_allows_1000_reserved_test_tasks(self):
        service = MagicMock()
        service.submit_local_test_registration.return_value = [{"id": index} for index in range(1000)]
        database = MagicMock()

        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            payload, status = create_registration_jobs(
                {
                    "count": 1000,
                    "workers": 4,
                    "email_source": "local_test",
                    "local_test_base": "sampleuser",
                    "local_test_domains": ["mail.test"],
                },
                service=service,
                database=database,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["submitted"], 1000)
        service.submit_local_test_registration.assert_called_once()


if __name__ == "__main__":
    unittest.main()
