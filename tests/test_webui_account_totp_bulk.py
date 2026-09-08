import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiAccountTotpBulkTests(unittest.TestCase):
    @patch("webui.app.svc.retry_accounts_twofa")
    def test_bulk_setup_deduplicates_and_reports_each_result(self, retry_accounts_twofa):
        retry_accounts_twofa.return_value = {
            "ok": True,
            "started": [{"account_id": 1}],
            "started_count": 1,
            "reused": [],
            "reused_count": 0,
            "skipped": [{"account_id": 4, "reason": "账号缺少登录密码"}],
            "skipped_count": 1,
        }
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/totp-setup-bulk",
            json={"account_ids": [1, 1, 4], "workers": 3},
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["started_count"], 1)
        self.assertEqual(body["skipped_count"], 1)
        retry_accounts_twofa.assert_called_once_with([1, 1, 4], workers=3)

    def test_bulk_setup_validates_empty_and_maximum(self):
        client = create_app(auth_code="test-auth").test_client()

        empty = client.post(
            "/api/accounts/totp-setup-bulk",
            json={"account_ids": []},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(empty.status_code, 400)

        too_many = client.post(
            "/api/accounts/totp-setup-bulk",
            json={"account_ids": list(range(501))},
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(too_many.status_code, 400)


if __name__ == "__main__":
    unittest.main()
