import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


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
                "credentials": "old@example.com----secret----JBSWY3DPEHPK3PXP",
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
        self.assertEqual(items[0].password, "secret")
        self.assertEqual(items[0].totp_secret, "JBSWY3DPEHPK3PXP")
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
        self.assertIn("personalRunning", page)
        self.assertIn("personalPending", page)
        self.assertIn('data-tab="email-change"', page)
        self.assertIn('id="tab-email-change"', page)
        self.assertNotIn('href="/email-change"', page)

        self.assertIn('id="loginCodexAfterChange" type="checkbox"', page)
        self.assertIn('name="login_codex_after_change"', page)
        self.assertIn("Sau khi đổi thành công, đăng nhập Codex ngay trong cùng phiên trình duyệt", page)
        self.assertIn("Chỉ chạy cho tài khoản đổi thành công", page)
        self.assertIn('id="personalInfoCodexResults"', page)
        self.assertIn("Kết quả đăng nhập Codex", page)
        self.assertIn("login_codex_after_change: codexOption", (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(encoding="utf-8"))

        standalone_page = self.client.get("/email-change")
        self.assertEqual(standalone_page.status_code, 404)

    def test_codex_checkbox_retains_server_availability_after_request(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("const codexAvailable = Boolean(loginCodexAfterChange && !loginCodexAfterChange.disabled);", script)
        self.assertIn("loginCodexAfterChange.disabled = !codexAvailable", script)

    def test_personal_info_export_uses_db_batch_instead_of_account_ids(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(
            encoding="utf-8"
        )
        export_block = script.split("exportButton.addEventListener", 1)[1]
        success_path = export_block.split("const blob = await response.blob();", 1)[0]

        self.assertNotIn("exportAccountIds", script)
        self.assertIn("/api/accounts/change-twofa-status?batch_id=", script)
        self.assertIn("pollTwofaProgress", script)
        self.assertIn("batch_id: exportBatchId", export_block)
        self.assertNotIn("account_ids", export_block)
        self.assertIn("if (!response.ok)", success_path)
        self.assertIn("const payload = await response.json().catch(() => ({}));", success_path)
        self.assertNotIn("const payload = await response.json().catch(() => ({}));\n      if (!response.ok)", success_path)
        self.assertIn("const blob = await response.blob();", export_block)
        self.assertIn("setTimeout(() =>", export_block)

    def test_twofa_validation_errors_are_visible_with_server_line_details(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("const showRequestError = (message) => {", script)
        self.assertIn("resultPanel.hidden = false;", script)
        self.assertIn("resultBody.innerHTML = '';", script)
        self.assertIn("showRequestError(isTwofa ? 'Hãy nhập danh sách tài khoản cần đổi 2FA.'", script)
        self.assertIn("showRequestError(error instanceof Error ? error.message : 'Không thể gửi yêu cầu');", script)

    def test_twofa_results_expose_manual_retry_and_poll_again(self):
        script = (Path(__file__).resolve().parents[1] / "webui" / "static" / "email_change.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("data-twofa-retry-index", script)
        self.assertIn("/api/accounts/change-twofa-retry", script)
        self.assertIn("body: JSON.stringify({ batch_id: batchId, index, credentials })", script)
        self.assertIn("await pollTwofaProgress(batchId);", script)
        self.assertIn("if (isTwofa && payload.batch_id)", script)
        self.assertIn("Sẽ tự động thử đọc lại...", script)
        self.assertIn("statusCode >= 400 && statusCode < 500", script)
        self.assertIn("result.retryable !== false", script)
        self.assertNotIn("password: ", script)
        self.assertNotIn("totp_secret: ", script)

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
