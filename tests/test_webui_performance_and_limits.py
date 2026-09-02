import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app_state_db, db
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
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "turb.sqlite3"
            with (
                patch.object(db, "_DEFAULT_SQLITE_PATH", database),
                patch.object(app_state_db, "APP_STATE_DB_PATH", database),
                patch.object(db, "_ACCOUNTS_JSON", Path(temp_dir) / "accounts.json"),
            ):
                db._save_accounts([{"id": 1, "email": "user@example.com"}])

        schedule_refresh.assert_called_once_with("save_accounts")
        render_viewer.assert_not_called()
        write_json.assert_called_once()
        sync_accounts.assert_called_once()
        sync_tokens.assert_called_once()

if __name__ == "__main__":
    unittest.main()
