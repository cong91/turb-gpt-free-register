# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import _compact_account_for_list, create_app


class WebuiTwofaReactivateTests(unittest.TestCase):
    def test_account_list_marks_every_non_active_twofa_account_for_reactivation(self):
        for status in ("disabled", "pending", "failed"):
            item = _compact_account_for_list({"id": 1, "email": "user@example.com", "twofa_status": status})
            self.assertTrue(item["twofa_reactivate_available"], status)

        active = _compact_account_for_list({
            "id": 2,
            "email": "active@example.com",
            "twofa_status": "active",
            "totp_secret": "SECRET",
        })
        self.assertFalse(active["twofa_reactivate_available"])

    @patch("webui.app.svc.retry_account_twofa")
    @patch("webui.app.db.get_account")
    def test_account_reactivate_route_dispatches_existing_twofa_retry_action(
        self,
        get_account,
        retry_account_twofa,
    ):
        get_account.return_value = {
            "id": 7,
            "email": "user@example.com",
            "twofa_status": "failed",
        }
        retry_account_twofa.return_value = {
            "ok": True,
            "created": True,
            "retry_action": "2fa",
            "message": "started",
        }
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/7/twofa/reactivate",
            headers={"X-Auth-Code": "test-auth"},
            json={"workers": 2},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        retry_account_twofa.assert_called_once_with(7, workers=2)

    @patch("webui.app.svc.retry_accounts_twofa", create=True)
    def test_bulk_reactivate_route_dispatches_account_ids_and_workers(self, retry_accounts_twofa):
        retry_accounts_twofa.return_value = {
            "ok": True,
            "started": [{"account_id": 7}],
            "started_count": 1,
            "reused": [],
            "reused_count": 0,
            "skipped": [],
            "skipped_count": 0,
        }
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/twofa/reactivate-bulk",
            headers={"X-Auth-Code": "test-auth"},
            json={"account_ids": [7, 8], "workers": 3},
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        retry_accounts_twofa.assert_called_once_with([7, 8], workers=3)

    def test_bulk_reactivate_route_rejects_more_than_500_accounts(self):
        client = create_app(auth_code="test-auth").test_client()
        response = client.post(
            "/api/accounts/twofa/reactivate-bulk",
            headers={"X-Auth-Code": "test-auth"},
            json={"account_ids": list(range(501))},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "Khôi phục 2FA chỉ xử lý tối đa 500 tài khoản mỗi lần",
        )

    def test_account_template_contains_reactivate_twofa_action(self):
        client = create_app(auth_code="test-auth").test_client()
        response = client.get("/", headers={"X-Auth-Code": "test-auth"})

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data-account-twofa-reactivate", html)
        self.assertIn("twofa_reactivate_available", html)


if __name__ == "__main__":
    unittest.main()
