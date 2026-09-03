import unittest
from threading import Event
from unittest.mock import MagicMock, patch


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

    def test_missing_account_with_credentials_is_saved_logged_in_and_checked(self):
        from core.account_plan_import import queue_imported_plan_checks

        login_finished = Event()
        enqueue_calls = []

        def insert_account(**kwargs):
            self.assertEqual(kwargs["email"], "missing@example.com")
            self.assertEqual(kwargs["registration_password"], "password")
            self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
            return 9

        def login_and_save(**kwargs):
            self.assertEqual(kwargs["account_id"], 9)
            self.assertEqual(kwargs["password"], "password")
            return {"ok": True}

        def enqueue(**kwargs):
            enqueue_calls.append(kwargs)
            login_finished.set()
            return {"accepted": True, "status": "queued"}

        with (
            patch("core.account_plan_import.db.mark_account_plan_login_pending", return_value=True),
            patch(
                "core.account_plan_import.db.get_account",
                return_value={"id": 9, "email": "missing@example.com", "access_token": "saved-token"},
            ),
        ):
            result = queue_imported_plan_checks(
                "missing@example.com | password | JBSWY3DPEHPK3PXP",
                get_account_by_email=lambda _email: None,
                enqueue=enqueue,
                insert_account=insert_account,
                login_and_save=login_and_save,
            )
            self.assertEqual(result["started_count"], 1)
            self.assertEqual(result["login_started_count"], 1)
            self.assertEqual(result["started"][0]["status"], "login_queued")
            self.assertEqual(result["skipped_count"], 0)
            self.assertTrue(login_finished.wait(2), "login worker did not enqueue plan check")

        self.assertEqual(enqueue_calls[0]["account_id"], 9)
        self.assertEqual(enqueue_calls[0]["access_token"], "saved-token")

    def test_login_pending_is_reported_as_pending(self):
        from core.account_plan_import import build_import_plan_status

        result = build_import_plan_status([
            {
                "id": 9,
                "email": "missing@example.com",
                "plan_check_status": "login_pending",
                "plan_check_ok": None,
            }
        ])

        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["items"][0]["classification"], "pending")

    def test_tokenless_existing_account_uses_imported_credentials_before_login(self):
        from core.account_plan_import import queue_imported_plan_checks

        login_finished = Event()
        credential_updates = []

        def login_and_save(**kwargs):
            self.assertEqual(kwargs["account_id"], 7)
            self.assertEqual(kwargs["password"], "new-password")
            self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
            return {"ok": True}

        with (
            patch("core.account_plan_import.db.mark_account_plan_login_pending", return_value=True),
            patch(
                "core.account_plan_import.db.update_account_login_credentials",
                side_effect=lambda account_id, **kwargs: credential_updates.append((account_id, kwargs)) or True,
            ),
            patch(
                "core.account_plan_import.db.get_account",
                return_value={"id": 7, "email": "known@example.com", "access_token": "saved-token"},
            ),
        ):
            result = queue_imported_plan_checks(
                "known@example.com | new-password | JBSWY3DPEHPK3PXP",
                get_account_by_email=lambda _email: {"id": 7, "email": "known@example.com", "access_token": ""},
                enqueue=lambda **_kwargs: login_finished.set() or {"accepted": True},
                login_and_save=login_and_save,
            )
            self.assertEqual(result["started_count"], 1)
            self.assertTrue(login_finished.wait(2), "login worker did not enqueue plan check")

        self.assertEqual(credential_updates, [
            (7, {"password": "new-password", "totp_secret": "JBSWY3DPEHPK3PXP"}),
        ])

    def test_selected_login_network_mode_is_forwarded_to_login_worker(self):
        from core.account_plan_import import queue_imported_plan_checks

        login_finished = Event()
        login_modes = []

        def login_and_save(**kwargs):
            login_modes.append(kwargs["network_mode"])
            return {"ok": True}

        with (
            patch("core.account_plan_import.db.mark_account_plan_login_pending", return_value=True),
            patch(
                "core.account_plan_import.db.get_account",
                return_value={"id": 7, "email": "known@example.com", "access_token": "saved-token"},
            ),
        ):
            result = queue_imported_plan_checks(
                "known@example.com | password | JBSWY3DPEHPK3PXP",
                get_account_by_email=lambda _email: {"id": 7, "email": "known@example.com", "access_token": ""},
                enqueue=lambda **_kwargs: login_finished.set() or {"accepted": True},
                login_and_save=login_and_save,
                login_network_mode="rotating_proxy",
            )

        self.assertEqual(result["login_network_mode"], "rotating_proxy")
        self.assertTrue(login_finished.wait(2), "login worker did not finish")
        self.assertEqual(login_modes, ["rotating_proxy"])

    def test_import_rejects_unknown_login_network_mode(self):
        from core.account_plan_import import queue_imported_plan_checks

        with self.assertRaises(ValueError):
            queue_imported_plan_checks(
                "known@example.com",
                get_account_by_email=lambda _email: {"id": 7, "email": "known@example.com", "access_token": "token"},
                enqueue=lambda **_kwargs: {"accepted": True},
                login_network_mode="unknown",
            )

    def test_login_route_failure_is_reported_before_account_is_saved(self):
        from core.account_plan_import import queue_imported_plan_checks

        insert_account = MagicMock()

        def preflight(_mode):
            raise RuntimeError("wireproxy unavailable")

        result = queue_imported_plan_checks(
            "missing@example.com | password | JBSWY3DPEHPK3PXP",
            get_account_by_email=lambda _email: None,
            enqueue=lambda **_kwargs: {"accepted": True},
            insert_account=insert_account,
            preflight_login_network=preflight,
            login_network_mode="nord_wire",
        )

        self.assertEqual(result["started_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("wireproxy unavailable", result["failed"][0]["reason"])
        insert_account.assert_called_once()

    def test_login_uses_the_selected_proxy_for_browser_profile(self):
        from core.account_plan_import import _login_and_save_account

        profile = MagicMock()
        profile.driver = object()

        def selected_route(*_args, **_kwargs):
            from contextlib import contextmanager

            @contextmanager
            def route_context():
                yield "socks5://proxy.example:1080", "proxy_pool"

            return route_context()

        with (
            patch("core.account_plan_import.selected_account_proxy", side_effect=selected_route),
            patch("core.browser_profile.open_browser_profile", return_value=profile) as open_profile,
            patch("core.account_security._login_and_get_access_token", return_value="saved-token"),
            patch("core.account_plan_import.db.update_account_access_token", return_value=True),
        ):
            result = _login_and_save_account(
                account_id=7,
                email="known@example.com",
                password="password",
                totp_secret="JBSWY3DPEHPK3PXP",
                network_mode="proxy_pool",
            )

        self.assertTrue(result["ok"])
        open_profile.assert_called_once_with(proxy="socks5://proxy.example:1080")

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

    @patch("core.account_plan_import._schedule_login_tasks")
    @patch("webui.app.db.mark_account_plan_login_pending", return_value=True)
    @patch("webui.app.db.insert_account", return_value=9)
    @patch("webui.app.db.get_account_by_email", return_value=None)
    @patch("core.account_plan_import.preflight_login_network", return_value="proxy_pool")
    def test_import_route_saves_missing_credential_account_for_login(
        self, _preflight, _get_account, insert_account, _mark_pending, _schedule_login
    ):
        response = self.client.post(
            "/api/accounts/check-plan-import",
            headers=self.headers,
            json={
                "emails": "missing@example.com | password | JBSWY3DPEHPK3PXP",
                "login_network_mode": "proxy_pool",
            },
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["started"][0]["status"], "login_queued")
        self.assertEqual(payload["skipped_count"], 0)
        insert_account.assert_called_once()
        self.assertEqual(insert_account.call_args.kwargs["access_token"], "")
        self.assertEqual(insert_account.call_args.kwargs["registration_password"], "password")
        _schedule_login.assert_called_once()
        self.assertEqual(_schedule_login.call_args.args[0][0]["network_mode"], "proxy_pool")

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
        self.assertIn('id="importPlanLoginNetwork"', template)
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
