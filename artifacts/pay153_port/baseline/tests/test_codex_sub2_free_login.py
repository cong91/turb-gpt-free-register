import unittest
from unittest.mock import patch

from core import codex_sub2_free_login
from webui.app import create_app


class CodexSub2FreeLoginTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    @staticmethod
    def _eligible(account_id: int = 7) -> dict:
        return {
            "id": account_id,
            "email": f"user{account_id}@example.com",
            "current_plan_type": "free",
            "plan_check_status": "success",
            "plan_check_ok": True,
            "plus_trial_eligible": False,
        }

    def test_predicate_requires_confirmed_free_without_trial(self):
        self.assertTrue(codex_sub2_free_login.is_confirmed_free_without_trial(self._eligible()))
        self.assertFalse(codex_sub2_free_login.is_confirmed_free_without_trial({
            **self._eligible(),
            "plus_trial_eligible": None,
        }))
        self.assertFalse(codex_sub2_free_login.is_confirmed_free_without_trial({
            **self._eligible(),
            "plan_check_ok": False,
        }))
        self.assertFalse(codex_sub2_free_login.is_confirmed_free_without_trial({
            **self._eligible(),
            "current_plan_type": "plus",
        }))

    def test_selector_excludes_accounts_already_authenticated_in_codex(self):
        result = codex_sub2_free_login.select_target_ids(
            [
                {**self._eligible(7), "codex_status": "success"},
                {**self._eligible(8), "codex_status": "failed"},
            ],
            authenticated_emails={"user8@example.com"},
        )

        self.assertEqual(result["account_ids"], [])
        self.assertEqual(result["skipped_authenticated"], 2)

    def test_selector_skips_only_retrying_accounts_with_a_live_worker(self):
        rows = [
            {**self._eligible(7), "codex_status": "retrying"},
            {**self._eligible(8), "codex_status": "retrying"},
        ]

        result = codex_sub2_free_login.select_target_ids(
            rows,
            authenticated_emails=set(),
            retrying_emails={"user7@example.com"},
        )

        self.assertEqual(result["account_ids"], [8])
        self.assertEqual(result["skipped_retrying"], 1)

    @patch("webui.codex_sub2_api.db.list_account_plan_check_statuses")
    @patch("webui.codex_sub2_api.db.list_codex_accounts")
    @patch("webui.codex_sub2_api.codex_retry_service.active_retrying_emails", return_value={"user10@example.com"})
    @patch("webui.codex_sub2_api._codex_cfg.CODEX_AUTH_URL_SOURCE", "sub2")
    def test_targets_api_returns_only_accounts_without_codex_auth(self, active_retrying, list_codex_accounts, list_statuses):
        list_statuses.return_value = {
            "items": [
                self._eligible(7),
                {**self._eligible(8), "plus_trial_eligible": True},
                {**self._eligible(9), "codex_status": "deactivated"},
                {**self._eligible(10), "codex_status": "retrying"},
            ],
            "total": 4,
        }
        list_codex_accounts.return_value = [{"email": "user7@example.com"}]

        response = self.client.get(
            "/api/codex/sub2-free-no-trial-targets",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account_ids"], [])
        self.assertEqual(response.get_json()["count"], 0)
        self.assertEqual(response.get_json()["skipped_authenticated"], 1)
        self.assertEqual(response.get_json()["skipped_deactivated"], 1)
        self.assertEqual(response.get_json()["skipped_retrying"], 1)
        list_statuses.assert_called_once_with(limit=5001, archived=False)
        list_codex_accounts.assert_called_once_with(archived="all")
        active_retrying.assert_called_once_with()

    @patch("webui.codex_sub2_api.db.list_account_plan_check_statuses")
    @patch("webui.codex_sub2_api.db.list_codex_accounts", return_value=[])
    @patch("webui.codex_sub2_api.codex_retry_service.active_retrying_emails", return_value=set())
    @patch("webui.codex_sub2_api._codex_cfg.CODEX_AUTH_URL_SOURCE", "sub2")
    def test_targets_api_releases_stale_retrying_status(self, active_retrying, _list_codex_accounts, list_statuses):
        list_statuses.return_value = {
            "items": [{**self._eligible(7), "codex_status": "retrying"}],
            "total": 1,
        }

        response = self.client.get(
            "/api/codex/sub2-free-no-trial-targets",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account_ids"], [7])
        self.assertEqual(response.get_json()["skipped_retrying"], 0)
        active_retrying.assert_called_once_with()

    @patch("webui.codex_sub2_api._codex_cfg.CODEX_AUTH_URL_SOURCE", "cpa")
    def test_targets_api_rejects_non_sub2_oauth_source(self):
        response = self.client.get(
            "/api/codex/sub2-free-no-trial-targets",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("CODEX_AUTH_URL_SOURCE=sub2", response.get_json()["error"])

    @patch("webui.codex_sub2_api.db.list_account_plan_check_statuses")
    @patch("webui.codex_sub2_api._codex_cfg.CODEX_AUTH_URL_SOURCE", "sub2")
    def test_targets_api_rejects_more_than_bulk_limit(self, list_statuses):
        list_statuses.return_value = {
            "items": [self._eligible(account_id) for account_id in range(1, 502)],
            "total": 501,
        }

        response = self.client.get(
            "/api/codex/sub2-free-no-trial-targets",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("500", response.get_json()["error"])

    def test_targets_api_requires_authentication(self):
        response = self.client.get("/api/codex/sub2-free-no-trial-targets")

        self.assertEqual(response.status_code, 401)

    def test_accounts_page_wires_free_no_trial_sub2_button(self):
        response = self.client.get("/", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="btnLoginFreeNoTrialSub2V2"', html)
        self.assertIn("Đăng nhập Free không trial", html)
        self.assertIn("loginAllFreeNoTrialToSub2", html)
        self.assertIn("/api/codex/sub2-free-no-trial-targets", html)
        self.assertIn("const scopeIds = await getAccountBulkIds();", html)
        self.assertIn("当前批量范围内", html)

    def test_vietnamese_localization_includes_new_button(self):
        response = self.client.get("/static/vi.js")
        try:
            self.assertEqual(response.status_code, 200)
            script = response.get_data(as_text=True)
            self.assertIn("Đăng nhập Free không trial", script)
            self.assertIn("sub2api", script)
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
