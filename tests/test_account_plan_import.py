import unittest
from unittest.mock import patch


class AccountPlanImportTests(unittest.TestCase):
    def test_queues_email_list_using_stored_access_tokens(self):
        from core.account_plan_import import queue_imported_plan_checks

        accounts = {
            "known@example.com": {
                "id": 7,
                "email": "known@example.com",
                "access_token": "stored-access-token",
            },
        }
        queued_calls = []

        def enqueue(**kwargs):
            queued_calls.append(kwargs)
            return {"accepted": True, "status": "queued"}

        result = queue_imported_plan_checks(
            "known@example.com\n",
            get_account_by_email=accounts.get,
            enqueue=enqueue,
        )

        self.assertEqual(result["parsed_count"], 1)
        self.assertEqual(result["started_count"], 1)
        self.assertEqual(queued_calls[0]["access_token"], "stored-access-token")

    def test_reports_duplicate_missing_account_and_missing_token_separately(self):
        from core.account_plan_import import queue_imported_plan_checks

        accounts = {
            "known@example.com": {
                "id": 7,
                "email": "known@example.com",
                "access_token": "stored-access-token",
            },
            "no-token@example.com": {
                "id": 8,
                "email": "no-token@example.com",
                "access_token": "",
            },
        }

        result = queue_imported_plan_checks(
            """known@example.com
known@example.com
no-token@example.com
missing@example.com
# comment""",
            get_account_by_email=accounts.get,
            enqueue=lambda **kwargs: {"accepted": True},
        )

        self.assertEqual(result["parsed_count"], 4)
        self.assertEqual(result["started_count"], 1)
        reasons = {item["reason"] for item in result["skipped"]}
        self.assertEqual(reasons, {"duplicate", "missing_access_token", "account_not_found"})

    def test_email_line_is_trimmed_before_lookup(self):
        from core.account_plan_import import queue_imported_plan_checks

        lookup_emails = []

        def get_account(email):
            lookup_emails.append(email)
            return {"id": 7, "email": email, "access_token": "stored-access-token"}

        result = queue_imported_plan_checks(
            "  known@example.com  ",
            get_account_by_email=get_account,
            enqueue=lambda **kwargs: {"accepted": True},
        )

        self.assertEqual(result["started_count"], 1)
        self.assertEqual(lookup_emails, ["known@example.com"])

    def test_reports_free_accounts_and_marks_free_plus_trial_subset(self):
        from core.account_plan_import import build_import_plan_status

        result = build_import_plan_status(
            [
                {
                    "id": 7,
                    "email": "eligible@example.com",
                    "plan_check_status": "success",
                    "plan_check_ok": True,
                    "current_plan_type": "free",
                    "plus_trial_eligible": True,
                    "plan_checked_at": "2026-08-27T10:00:00",
                },
                {
                    "id": 8,
                    "email": "free-no-trial@example.com",
                    "plan_check_status": "success",
                    "plan_check_ok": True,
                    "current_plan_type": "free",
                    "plus_trial_eligible": False,
                },
                {
                    "id": 9,
                    "email": "plus@example.com",
                    "plan_check_status": "success",
                    "plan_check_ok": True,
                    "current_plan_type": "plus",
                    "plus_trial_eligible": False,
                },
                {
                    "id": 10,
                    "email": "expired@example.com",
                    "plan_check_status": "failed",
                    "plan_check_ok": False,
                    "plan_check_error": "AT已过期/失效，请手动查活刷新",
                },
            ]
        )

        self.assertEqual(result["free_plan_count"], 2)
        self.assertEqual(
            {item["email"] for item in result["free_plan_accounts"]},
            {"eligible@example.com", "free-no-trial@example.com"},
        )
        self.assertEqual(result["free_without_trial_count"], 1)
        self.assertEqual(result["free_without_trial_accounts"][0]["email"], "free-no-trial@example.com")
        self.assertEqual(result["free_plus_trial_count"], 1)
        self.assertEqual(result["free_plus_trial_accounts"][0]["email"], "eligible@example.com")
        self.assertEqual(result["not_free_plan_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["items"][3]["classification"], "needs_live_check")


class AccountPlanImportApiTests(unittest.TestCase):
    def setUp(self):
        from webui.app import create_app

        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.db.get_account_by_email")
    def test_import_route_uses_existing_account_token_and_does_not_return_token(
        self, get_account_by_email, enqueue
    ):
        get_account_by_email.return_value = {
            "id": 7,
            "email": "known@example.com",
            "access_token": "stored-access-token",
        }
        enqueue.return_value = {"accepted": True, "status": "queued"}

        response = self.client.post(
            "/api/accounts/check-plan-import",
            headers=self.headers,
            json={"emails": "known@example.com"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 1)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["access_token"], "stored-access-token")

    @patch("webui.app.db.get_account")
    def test_import_status_route_reports_free_accounts_without_secrets(self, get_account):
        get_account.side_effect = [
            {
                "id": 7,
                "email": "eligible@example.com",
                "access_token": "stored-access-token",
                "registration_password": "password-secret",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "plan_check_status": "success",
                "plan_check_ok": True,
                "current_plan_type": "free",
                "plus_trial_eligible": True,
            },
        ]

        response = self.client.get(
            "/api/accounts/check-plan-import-status?account_ids=7",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["free_plan_count"], 1)
        self.assertEqual(payload["free_plan_accounts"][0]["email"], "eligible@example.com")
        self.assertEqual(payload["free_plus_trial_count"], 1)
        self.assertNotIn("stored-access-token", response.get_data(as_text=True))
        self.assertNotIn("password-secret", response.get_data(as_text=True))
        self.assertNotIn("JBSWY3DPEHPK3PXP", response.get_data(as_text=True))

    def test_import_route_rejects_empty_input(self):
        response = self.client.post(
            "/api/accounts/check-plan-import",
            headers=self.headers,
            json={"emails": "\n# comment only\n"},
        )

        self.assertEqual(response.status_code, 400)

    def test_import_route_rejects_non_object_json(self):
        response = self.client.post(
            "/api/accounts/check-plan-import",
            headers=self.headers,
            json=["known@example.com"],
        )

        self.assertEqual(response.status_code, 400)

    def test_account_template_exposes_email_import_plan_panel(self):
        from pathlib import Path

        template = Path("webui/templates/index.html").read_text(encoding="utf-8")

        self.assertIn('id="btnImportPlanEmailsV2"', template)
        self.assertIn('id="importPlanEmails"', template)
        self.assertIn('id="btnCopyFreePlanEmails"', template)
        self.assertIn('id="importPlanResults"', template)
        self.assertIn('id="importPlanResultFilter"', template)
        self.assertIn('id="importPlanResultBody"', template)
        self.assertIn('importedPlanPrecheckItemsFromResult', template)
        self.assertIn('renderImportedPlanRows', template)
        self.assertIn('Tài khoản Free', template)
        self.assertIn('Có Free Trial', template)
        self.assertIn('Free, không có Trial', template)
        self.assertIn('Free không có Trial', template)
        self.assertNotIn('Có gói Free', template)
        self.assertIn('Không phải Free', template)
        self.assertIn('Cần kiểm tra lại', template)
        self.assertIn('Lỗi khi kiểm tra', template)
        self.assertIn('Không tìm thấy trong DB', template)
        self.assertIn('/api/accounts/check-plan-import', template)
        self.assertIn('/api/accounts/check-plan-import-status', template)


if __name__ == "__main__":
    unittest.main()
