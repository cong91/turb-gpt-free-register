import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config import email as email_config
from core import app_state_db, db
from webui.app import create_app


class WebUiPerformanceAndLimitsTests(unittest.TestCase):
    def test_account_page_uses_sql_page_without_python_full_scan(self):
        page = [{"id": 91, "email": "user@example.com", "archived": 0}]
        with (
            patch.object(db, "_filtered_decorated_accounts", side_effect=AssertionError("loaded all accounts")),
            patch.object(db, "_query_collection_page", return_value=(page, 10000, "latest")) as query_page,
        ):
            result = db.list_accounts_page(limit=50, offset=50)

        self.assertEqual([row["id"] for row in result["items"]], [91])
        self.assertEqual(result["total"], 10000)
        query_page.assert_called_once()
        self.assertEqual(query_page.call_args.args[0], "accounts")
        self.assertEqual(query_page.call_args.kwargs["limit"], 50)
        self.assertEqual(query_page.call_args.kwargs["offset"], 50)

    def test_paged_jobs_uses_sql_page_and_aggregated_status_counts(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        page = {
            "items": [{"id": 91, "status": "failed", "job_type": "registration"}],
            "total": 10000,
            "offset": 90,
            "limit": 10,
            "revision": "10000:latest",
        }
        with (
            patch("webui.app.db.list_jobs", side_effect=AssertionError("loaded all jobs")),
            patch("webui.app.db.list_jobs_page", return_value=page) as list_jobs_page,
            patch("webui.app.db.job_status_counts", return_value={"failed": 10000, "active": 0}) as status_counts,
            patch("webui.app.svc.get_retry_info", return_value={"retryable": False}) as get_retry_info,
        ):
            response = client.get("/api/jobs?paged=1&page=10&page_size=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["items"],
            [{"id": 91, "status": "failed", "job_type": "registration"}],
        )
        self.assertEqual(response.json["total"], 10000)
        self.assertEqual(response.json["status_counts"], {"failed": 10000, "active": 0})
        list_jobs_page.assert_called_once_with(limit=10, offset=90)
        status_counts.assert_called_once_with()
        get_retry_info.assert_called_once()

    def test_paged_jobs_enriches_only_visible_rows(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        rows = [{"id": index, "status": "failed", "job_type": "registration"} for index in range(100)]
        with patch(
            "webui.app.db.list_jobs_page",
            return_value={"items": rows[:10], "total": 100, "offset": 0, "limit": 10, "revision": "100:latest"},
        ), patch(
            "webui.app.svc.get_retry_info", return_value={"retryable": False}
        ) as get_retry_info:
            response = client.get("/api/jobs?paged=1&page=1&page_size=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 10)
        self.assertEqual(get_retry_info.call_count, 10)

    def test_plan_check_update_writes_only_the_target_account_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                target_id = db.insert_account(email="target@example.com", access_token="target-token")
                other_id = db.insert_account(email="other@example.com", access_token="other-token")

                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.update_account_plan_check(
                        acc_id=target_id,
                        result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
                    ))

                target = db.get_account(target_id)
                other = db.get_account(other_id)

        self.assertEqual(target["plan_check_status"], "success")
        self.assertEqual(target["current_plan_type"], "free")
        self.assertTrue(target["plus_trial_eligible"])
        self.assertIsNone(other.get("plan_check_status"))

    def test_plan_check_state_transitions_do_not_rewrite_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="target@example.com", access_token="token")
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.claim_account_plan_check(account_id, trigger="batch"))
                    self.assertTrue(db.mark_account_plan_check_running(account_id))

                row = db.get_account(account_id)

        self.assertEqual(row["plan_check_status"], "running")
        self.assertEqual(row["plan_check_trigger"], "batch")

    def test_insert_account_does_not_rewrite_account_or_outlook_collections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                    patch.object(db, "_load_outlook", side_effect=AssertionError("loaded all outlook rows")),
                    patch.object(db, "_save_outlook", side_effect=AssertionError("rewrote all outlook rows")),
                ):
                    account_id = db.insert_account(
                        email="registered@example.com",
                        access_token="token",
                        registration_password="password",
                    )

                row = db.get_account(account_id)

        self.assertEqual(row["email"], "registered@example.com")
        self.assertEqual(row["access_token"], "token")
        self.assertEqual(row["registration_password"], "password")

    def test_get_account_reads_one_sql_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="one@example.com", access_token="token")
                with patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")):
                    row = db.get_account(account_id)

        self.assertEqual(row["email"], "one@example.com")

    def test_liveness_update_writes_only_the_target_account_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="live@example.com", access_token="old-token")
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.update_account_liveness(
                        account_id,
                        {"ok": True, "status": "live", "access_token": "new-token"},
                    ))

                row = db.get_account(account_id)

        self.assertEqual(row["live_check_status"], "live")
        self.assertEqual(row["access_token"], "new-token")

    def test_liveness_state_transitions_do_not_rewrite_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="live@example.com", access_token="token")
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.claim_account_live_check(account_id, trigger="batch"))
                    self.assertTrue(db.mark_account_live_check_running(account_id))

                row = db.get_account(account_id)

        self.assertEqual(row["live_check_status"], "running")
        self.assertEqual(row["live_check_trigger"], "batch")

    def test_totp_state_transitions_do_not_rewrite_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="totp@example.com", access_token="token")
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.claim_account_totp_setup(account_id, trigger="batch"))
                    self.assertTrue(db.mark_account_totp_setup_running(account_id))
                    self.assertTrue(db.update_account_totp_secret(
                        account_id,
                        {"ok": True, "status": "success", "totp_secret": "secret"},
                    ))

                row = db.get_account(account_id)

        self.assertEqual(row["totp_setup_status"], "success")
        self.assertEqual(row["totp_secret"], "secret")

    def test_codex_and_extract_state_transitions_do_not_rewrite_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_schedule_static_viewer_refresh"),
            ):
                account_id = db.insert_account(email="state@example.com", access_token="token")
                with (
                    patch.object(db, "_load_accounts", side_effect=AssertionError("loaded all accounts")),
                    patch.object(db, "_save_accounts", side_effect=AssertionError("rewrote all accounts")),
                ):
                    self.assertTrue(db.claim_account_codex_agent(account_id, trigger="batch"))
                    self.assertTrue(db.mark_account_codex_agent_running(account_id))
                    self.assertTrue(db.update_account_codex_status("state@example.com", "success"))
                    self.assertTrue(db.claim_account_extract(account_id, trigger="batch"))
                    self.assertTrue(db.mark_account_extract_running(account_id))
                    self.assertTrue(db.update_account_extract(account_id, {"status": "failed", "error": "test"}))

                row = db.get_account(account_id)

        self.assertEqual(row["codex_status"], "success")
        self.assertEqual(row["extract_link_status"], "failed")

    def test_summary_reuses_pool_summaries_when_source_is_configured(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        with (
            patch.object(email_config, "EMAIL_SOURCE", "gmail_api_url"),
            patch("webui.app.db.gmail_api_url_email_pool_summary", return_value={"total": 1, "available": 1}) as gmail_summary,
            patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0}) as domain_summary,
            patch("webui.app.db.count_accounts", return_value=0),
        ):
            response = client.get("/api/summary", headers={"X-Auth-Code": "test-auth"})

        self.assertEqual(response.status_code, 200)
        gmail_summary.assert_called_once_with()
        domain_summary.assert_called_once_with()

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

    def test_static_viewer_refresh_scheduler_does_not_spawn_timer_per_write(self):
        timer = Mock()
        timer.is_alive.return_value = True
        with patch.object(db, "_VIEWER_REFRESH_TIMER", timer), patch.object(
            db.threading, "Timer"
        ) as timer_factory:
            db._schedule_static_viewer_refresh("hot-update")

        timer_factory.assert_not_called()

if __name__ == "__main__":
    unittest.main()
