import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class AccountPay153PromotionApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    @patch("webui.app.plan_check_service.enqueue_account_plan_check")
    @patch("webui.app.db.get_account")
    def test_promotion_route_queues_worker_without_returning_token_or_proxy(
        self, get_account, enqueue
    ):
        get_account.return_value = {
            "id": 7,
            "email": "free@example.com",
            "access_token": "server-side-access-token",
        }
        enqueue.return_value = {
            "accepted": True,
            "busy": False,
            "account_id": 7,
            "status": "queued",
            "trigger": "manual_pay153_promotion",
        }

        response = self.client.post(
            "/api/accounts/7/pay153/promotion",
            headers=self.headers,
            json={"proxy": "http://client-supplied-route:8080"},
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trigger"], "manual_pay153_promotion")
        self.assertNotIn("server-side-access-token", response.get_data(as_text=True))
        self.assertNotIn("client-supplied-route", response.get_data(as_text=True))
        enqueue.assert_called_once_with(
            account_id=7,
            email="free@example.com",
            access_token="server-side-access-token",
            trigger="manual_pay153_promotion",
            proxy=None,
            timezone_offset_min="-",
        )

    @patch("webui.app.db.get_account", return_value=None)
    def test_promotion_route_returns_not_found_for_unknown_account(self, _get_account):
        response = self.client.post(
            "/api/accounts/999/pay153/promotion",
            headers=self.headers,
            json={},
        )

        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertFalse(response.get_json()["ok"])


class AccountPay153PromotionTemplateTests(unittest.TestCase):
    def test_accounts_workspace_contains_free_only_promotion_action(self):
        template_path = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        template = template_path.read_text(encoding="utf-8")

        self.assertIn("data-account-pay153-promotion", template)
        self.assertIn("/api/accounts/${encodeURIComponent(id)}/pay153/promotion", template)
        self.assertIn("plan !== 'free'", template)
        self.assertIn("manual_pay153_promotion", template)


if __name__ == "__main__":
    unittest.main()
