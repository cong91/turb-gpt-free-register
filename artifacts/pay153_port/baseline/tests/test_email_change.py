import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import app_state_db, db, email_change
from core.email_change import EmailChangeInput, parse_email_change_inputs
from core.gmail_api_url_client import GmailApiUrlAccount, poll_verification_code
from webui.app import create_app


class EmailChangeInputTests(unittest.TestCase):
    def test_pairs_credentials_with_gmail_api_url_by_line(self):
        result = parse_email_change_inputs(
            "old@example.com|secret|JBSWY3DPEHPK3PXP\n",
            "new@example.com----https://mail.example/otp/1\n",
        )

        self.assertEqual(
            result,
            [
                EmailChangeInput(
                    old_email="old@example.com",
                    password="secret",
                    totp_secret="JBSWY3DPEHPK3PXP",
                    new_email="new@example.com",
                    code_url="https://mail.example/otp/1",
                    gmail_source_email="new@example.com",
                )
            ],
        )

    def test_quota_defaults_to_one_and_expands_one_gmail_source_up_to_six(self):
        credentials = "\n".join(
            f"old{i}@example.com|secret|JBSWY3DPEHPK3PXP" for i in range(6)
        )
        result = parse_email_change_inputs(
            credentials,
            "targetaccount@gmail.com----https://mail.example/otp/1",
            quota=6,
        )

        self.assertEqual(len(result), 6)
        self.assertEqual(result[0].new_email, "targetaccount@gmail.com")
        self.assertEqual(len({item.new_email for item in result}), 6)
        self.assertEqual({item.code_url for item in result}, {"https://mail.example/otp/1"})
        self.assertEqual({item.gmail_source_email for item in result}, {"targetaccount@gmail.com"})

    def test_quota_rejects_values_outside_one_to_six(self):
        for quota in (0, 7):
            with self.subTest(quota=quota), self.assertRaisesRegex(ValueError, "between 1 and 6"):
                parse_email_change_inputs(
                    "old@example.com|secret|JBSWY3DPEHPK3PXP",
                    "targetaccount@gmail.com----https://mail.example/otp/1",
                    quota=quota,
                )

    def test_quota_above_one_requires_enough_credentials_and_a_gmail_source(self):
        with self.assertRaisesRegex(ValueError, "2 credential lines"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "targetaccount@gmail.com----https://mail.example/otp/1",
                quota=2,
            )
        with self.assertRaisesRegex(ValueError, "Gmail source"):
            parse_email_change_inputs(
                "old1@example.com|secret|JBSWY3DPEHPK3PXP\nold2@example.com|secret|JBSWY3DPEHPK3PXP",
                "target@example.com----https://mail.example/otp/1",
                quota=2,
            )

    def test_preserves_pipe_in_password_and_rejects_mismatched_counts(self):
        result = parse_email_change_inputs(
            "old@example.com|pa|ss|JBSWY3DPEHPK3PXP",
            "new@example.com----https://mail.example/otp/1",
        )
        self.assertEqual(result[0].password, "pa|ss")

        with self.assertRaisesRegex(ValueError, "require 1 credential lines"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP\nsecond@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1",
            )
        with self.assertRaisesRegex(ValueError, "credential line 1"):
            parse_email_change_inputs(
                "broken credential\nold@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1\nsecond@example.com----https://mail.example/otp/2",
            )
        with self.assertRaisesRegex(ValueError, "code URL must be unique"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP\nsecond@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1\nsecond-new@example.com----https://mail.example/otp/1",
            )

    def test_rejects_invalid_target_email_and_non_http_otp_url(self):
        with self.assertRaisesRegex(ValueError, "target email"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "not-an-email----https://mail.example/otp/1",
            )
        with self.assertRaisesRegex(ValueError, "HTTP"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----file:///otp/1",
            )
        with self.assertRaisesRegex(ValueError, "public host"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----http://127.0.0.1:8080/otp/1",
            )

    def test_classifies_recorded_settings_and_verification_states(self):
        classify = email_change.classify_email_change_state
        self.assertEqual(
            classify({"url": "https://chatgpt.com/settings", "body": "Account", "inputs": []}),
            "settings",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://chatgpt.com/",
                    "body": "Enter verification code",
                    "inputs": [{"id": "verification_code", "autocomplete": "one-time-code"}],
                }
            ),
            "email_otp",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://auth.openai.com/mfa/totp",
                    "body": "Enter the code from your authenticator app",
                    "inputs": [{"name": "code", "autocomplete": "one-time-code"}],
                }
            ),
            "totp",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://chatgpt.com/auth/login",
                    "body": "Log in",
                    "inputs": [{"name": "email", "type": "email"}],
                }
            ),
            "unknown",
        )

    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch(
        "core.gmail_api_url_client._fetch_code_once",
        side_effect=[(0, "111111"), (0, "222222")],
    )
    def test_gmail_api_polling_does_not_log_otp_values(self, _fetch, _sleep):
        account = GmailApiUrlAccount("new@example.com", "https://mail.example/otp/1")

        with self.assertLogs("core.gmail_api_url_client", level="INFO") as captured:
            code = poll_verification_code(
                account,
                max_wait=5,
                poll_interval=0,
                after_ts=1.0,
                before_code="111111",
            )

        self.assertEqual(code, "222222")
        output = "\n".join(captured.output)
        self.assertNotIn("111111", output)
        self.assertNotIn("222222", output)

    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch("core.gmail_api_url_client._fetch_code_once", return_value=(0, "222222"))
    def test_explicit_empty_baseline_accepts_first_new_otp(self, fetch, _sleep):
        account = GmailApiUrlAccount("new@example.com", "https://mail.example/otp/1")

        code = poll_verification_code(
            account,
            max_wait=0.01,
            poll_interval=0,
            after_ts=1.0,
            before_code=None,
        )

        self.assertEqual(code, "222222")
        fetch.assert_called_once()


class EmailChangeBrowserWorkflowTests(unittest.TestCase):
    def test_change_workflow_logs_in_polls_new_mail_and_confirms_success(self):
        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        with (
            patch.object(email_change, "_login_chatgpt_with_credentials") as login,
            patch.object(email_change, "_open_settings_account"),
            patch.object(email_change, "_submit_email_change_request") as request_change,
            patch.object(email_change, "snapshot_verification_code", return_value="111111") as snapshot,
            patch.object(email_change, "poll_new_email_otp", return_value="654321") as poll,
            patch.object(email_change, "_submit_email_verification_code") as submit_code,
            patch.object(email_change, "_wait_for_email_change_success") as wait_success,
            patch.object(email_change.time, "time", return_value=100.0),
        ):
            result = email_change.change_email_in_browser(object(), item)

        self.assertEqual(result, {"ok": True, "old_email": item.old_email, "new_email": item.new_email})
        login.assert_called_once()
        request_change.assert_called_once()
        snapshot.assert_called_once_with(item)
        poll.assert_called_once_with(item, after_ts=100.0, before_code="111111")
        submit_code.assert_called_once_with(unittest.mock.ANY, "654321")
        wait_success.assert_called_once()


class AccountEmailPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
        self.stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"))
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"))
        self.stack.enter_context(patch.object(db, "_TOKENS_TXT", root / "tokens.txt"))
        self.stack.enter_context(patch.object(db, "_VIEWER_HTML", root / "viewer.html"))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_update_account_email_rewrites_account_identity_and_copy_line(self):
        db.insert_account(
            email="old@example.com",
            access_token="access-token",
            registration_password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

        self.assertTrue(db.update_account_email("old@example.com", "new@example.com"))
        self.assertIsNone(db.get_account_by_email("old@example.com"))
        account = db.get_account_by_email("new@example.com")
        self.assertEqual(account["email"], "new@example.com")
        self.assertIn("new@example.com", account["copy_line"])
        self.assertEqual(account["registration_password"], "secret")

    def test_update_account_email_rewrites_registered_email_export(self):
        db.import_codex_credential_accounts([
            {
                "email": "old@example.com",
                "registration_password": "secret",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        ])

        self.assertTrue(db.update_account_email("old@example.com", "new@example.com"))
        export = (Path(db._ACCOUNTS_TXT)).read_text(encoding="utf-8")
        self.assertIn("new@example.com", export)
        self.assertNotIn("old@example.com", export)

    def test_update_account_email_rejects_existing_target(self):
        db.insert_account(email="old@example.com", access_token="old-token")
        db.insert_account(email="new@example.com", access_token="new-token")

        with self.assertRaisesRegex(ValueError, "already exists"):
            db.update_account_email("old@example.com", "new@example.com")

    def test_personal_info_change_batch_resolves_export_rows_from_persistence(self):
        app_state_path = Path(self.temp_dir.name) / "app-state.sqlite3"
        with patch.object(app_state_db, "APP_STATE_DB_PATH", app_state_path):
            account_id = db.insert_account(
                email="changed@example.com",
                access_token="access-token",
                registration_password="password",
                totp_secret="NEWSECRET",
            )
            batch = db.save_personal_info_change_batch(
                "batch-1",
                "twofa",
                [
                    {
                        "ok": True,
                        "account_id": account_id,
                        "email": "changed@example.com",
                        "change_status": "success",
                        "new_totp_secret": "MUST-NOT-BE-STORED",
                    },
                    {
                        "ok": False,
                        "email": "failed@example.com",
                        "change_status": "failed",
                        "error": "browser failed",
                    },
                ],
            )

            rows = db.get_personal_info_change_export_rows("batch-1")

        self.assertEqual(batch["exportable_count"], 1)
        self.assertEqual([row["email"] for row in rows], ["changed@example.com"])
        self.assertNotIn("MUST-NOT-BE-STORED", str(batch))


class BrowserEmailChangeRunnerTests(unittest.TestCase):
    def test_runner_cleans_profile_and_updates_persistence_after_success(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        driver = Mock()
        profile = Mock(driver=driver, provider="roxy")
        with (
            patch("config.proxy.ROTATING_PROXY_ENABLED", False),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile),
            patch.object(
                browser_email_change,
                "change_email_in_browser",
                return_value={"ok": True, "old_email": item.old_email, "new_email": item.new_email},
            ),
            patch.object(browser_email_change.db, "update_account_email", return_value=True) as update_email,
            patch.object(
                browser_email_change.db,
                "get_account_by_email",
                return_value={"id": 77, "email": item.new_email},
            ),
            patch.object(browser_email_change.db, "get_gmail_api_url_email_by_email", return_value={"email": item.new_email}),
            patch.object(browser_email_change.db, "release_gmail_api_url_email") as mark_used,
        ):
            result = browser_email_change.run_email_change(item)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["account_id"], 77)
        update_email.assert_called_once_with(item.old_email, item.new_email)
        mark_used.assert_called_once_with(item.new_email, "used", "account email changed")
        profile.close.assert_called_once_with()
        profile.cleanup.assert_called_once_with()

    def test_runner_does_not_persist_when_browser_reports_failure(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        driver = Mock()
        profile = Mock(driver=driver, provider="roxy")
        with (
            patch("config.proxy.ROTATING_PROXY_ENABLED", False),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile),
            patch.object(browser_email_change, "change_email_in_browser", return_value={"ok": False, "error": "verification failed"}),
            patch.object(browser_email_change.db, "update_account_email") as update_email,
        ):
            result = browser_email_change.run_email_change(item)

        self.assertFalse(result["ok"])
        self.assertFalse(result["persisted"])
        update_email.assert_not_called()

    def test_batch_serializes_accounts_that_share_one_gmail_api_url(self):
        from core import browser_email_change

        items = [
            EmailChangeInput(
                old_email=f"old{i}@example.com",
                password="secret",
                totp_secret="JBSWY3DPEHPK3PXP",
                new_email=f"new{i}@gmail.com",
                code_url="https://mail.example/otp/shared" if i < 3 else "https://mail.example/otp/other",
            )
            for i in range(4)
        ]
        active: dict[str, int] = {}
        peak: dict[str, int] = {}
        lock = threading.Lock()

        def run_one(item, proxy_lane_id=None):
            with lock:
                active[item.code_url] = active.get(item.code_url, 0) + 1
                peak[item.code_url] = max(peak.get(item.code_url, 0), active[item.code_url])
            time.sleep(0.02)
            with lock:
                active[item.code_url] -= 1
            return {"ok": True, "old_email": item.old_email, "new_email": item.new_email}

        with patch.object(browser_email_change, "run_email_change", side_effect=run_one):
            results = browser_email_change.run_email_change_batch(items, workers=4)

        self.assertEqual(peak["https://mail.example/otp/shared"], 1)
        self.assertEqual([result["old_email"] for result in results], [item.old_email for item in items])


class EmailChangeApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.email_change_api.db.save_personal_info_change_batch")
    @patch("webui.email_change_api.run_email_change_batch")
    def test_route_parses_inputs_and_persists_export_batch(self, run_batch, save_batch):
        run_batch.return_value = [{
            "ok": True,
            "old_email": "old@example.com",
            "new_email": "new@example.com",
            "account_id": 7,
        }]
        save_batch.return_value = {"batch_id": "a" * 32, "exportable_count": 1}

        response = self.client.post(
            "/api/accounts/change-email",
            json={
                "credentials": "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "gmail_api": "new@example.com----https://mail.example/otp/1",
                "workers": 2,
            },
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        items = run_batch.call_args.args[0]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].new_email, "new@example.com")
        self.assertEqual(run_batch.call_args.kwargs["workers"], 2)
        payload = response.get_json()
        self.assertRegex(payload["change_batch_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["exportable_count"], 1)
        save_batch.assert_called_once()
        self.assertEqual(save_batch.call_args.args[1], "email")

    @patch("webui.email_change_api.db.save_personal_info_change_batch", return_value={"batch_id": "c" * 32, "exportable_count": 0})
    @patch("webui.email_change_api.run_email_change_batch")
    def test_route_expands_requested_quota_per_gmail_api_record(self, run_batch, _save_batch):
        run_batch.return_value = [
            {"ok": True, "old_email": f"old{i}@example.com", "new_email": f"new{i}@gmail.com"}
            for i in range(3)
        ]
        credentials = "\n".join(
            f"old{i}@example.com|secret|JBSWY3DPEHPK3PXP" for i in range(3)
        )

        response = self.client.post(
            "/api/accounts/change-email",
            json={
                "credentials": credentials,
                "gmail_api": "targetaccount@gmail.com----https://mail.example/otp/1",
                "quota": 3,
                "workers": 4,
            },
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        items = run_batch.call_args.args[0]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].new_email, "targetaccount@gmail.com")

    def test_route_rejects_cross_origin_request(self):
        response = self.client.post(
            "/api/accounts/change-email",
            json={"credentials": "x", "gmail_api": "y"},
            headers={"Origin": "https://attacker.example"},
        )

        self.assertEqual(response.status_code, 403)

    def test_email_change_is_a_dashboard_tab(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("emailChangeForm", page)
        self.assertIn("exportChangedAccounts", page)
        self.assertIn("Thông tin cá nhân", page)
        self.assertIn('data-personal-mode="email"', page)
        self.assertIn('data-personal-mode="twofa"', page)
        self.assertIn('data-personal-panel="twofa"', page)
        self.assertIn("personalResultBody", page)
        self.assertIn('data-tab="email-change"', page)
        self.assertIn('id="tab-email-change"', page)
        self.assertNotIn('href="/email-change"', page)

        standalone_page = self.client.get("/email-change")
        self.assertEqual(standalone_page.status_code, 404)

    def test_personal_info_export_uses_db_batch_instead_of_account_ids(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(
            encoding="utf-8"
        )
        export_block = script.split("exportButton.addEventListener", 1)[1]
        success_path = export_block.split("const blob = await response.blob();", 1)[0]

        self.assertNotIn("exportAccountIds", script)
        self.assertIn("batch_id: exportBatchId", export_block)
        self.assertNotIn("account_ids", export_block)
        self.assertIn("if (!response.ok)", success_path)
        self.assertIn("const payload = await response.json().catch(() => ({}));", success_path)
        self.assertNotIn("const payload = await response.json().catch(() => ({}));\n      if (!response.ok)", success_path)
        self.assertIn("const blob = await response.blob();", export_block)
        self.assertIn("setTimeout(() =>", export_block)

    @patch("webui.email_change_api.db.get_personal_info_change_export_rows")
    @patch("webui.email_change_api.db.get_personal_info_change_batch")
    def test_export_route_downloads_db_backed_batch_in_modern_format(self, get_batch, get_rows):
        get_batch.return_value = {"batch_id": "batch-1", "mode": "email", "exportable_count": 2}
        get_rows.return_value = [
            {
                "email": "new-one@example.com",
                "registration_password": "pass-one",
                "totp_secret": "TOTPONE",
            },
            {
                "email": "new-two@example.com",
                "registration_password": "pass-two",
                "totp_secret": "TOTPTWO",
            },
        ]

        response = self.client.post(
            "/api/accounts/change-email/export",
            json={"batch_id": "batch-1"},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            response.get_data(as_text=True),
            "new-one@example.com | pass-one | TOTPONE\n"
            "new-two@example.com | pass-two | TOTPTWO\n",
        )
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        get_batch.assert_called_once_with("batch-1")
        get_rows.assert_called_once_with("batch-1")

    @patch("webui.email_change_api.db.get_personal_info_change_batch", return_value=None)
    def test_export_route_rejects_unknown_batch(self, _get_batch):
        response = self.client.post(
            "/api/accounts/change-email/export",
            json={"batch_id": "missing-batch"},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 404)

    @patch("webui.email_change_api.run_email_change_batch")
    def test_route_rejects_oversized_batch(self, run_batch):
        credentials = "\n".join(
            f"user{i}@example.com|secret|JBSWY3DPEHPK3PXP" for i in range(51)
        )
        gmail_api = "\n".join(
            f"new{i}@example.com----https://mail.example/otp/{i}" for i in range(51)
        )
        response = self.client.post(
            "/api/accounts/change-email",
            json={"credentials": credentials, "gmail_api": gmail_api},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        run_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
