# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class AccountCopyApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    @patch("webui.app.db.get_account")
    def test_single_secret_returns_canonical_copy_line(self, get_account):
        get_account.return_value = {
            "id": 7,
            "email": "user@example.com",
            "registration_password": "openai-pass",
            "totp_secret": "TOTPSECRET",
        }
        response = self.client.get(
            "/api/accounts/7/secret?field=copy_line",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["value"], "user@example.com | openai-pass | TOTPSECRET")

    @patch("webui.app.db.get_account")
    def test_single_secret_supports_legacy_format(self, get_account):
        get_account.return_value = {
            "id": 7,
            "email": "user@example.com",
            "original_email_line": "user@example.com----mailbox----client----refresh",
            "access_token": "access-token",
            "registration_password": "openai-pass",
            "totp_secret": "TOTPSECRET",
        }
        response = self.client.get(
            "/api/accounts/7/secret?field=copy_line&format=legacy",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["value"],
            "user@example.com----mailbox----client----refresh----access-token----TOTPSECRET",
        )

    @patch("webui.app.db.get_account")
    def test_invalid_export_format_is_rejected(self, get_account):
        get_account.return_value = {"id": 7, "email": "user@example.com"}
        response = self.client.get(
            "/api/accounts/7/secret?field=copy_line&format=unknown",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("format", response.get_json()["error"])

    @patch("webui.app.db.get_account")
    def test_bulk_secret_preserves_canonical_lines_and_skips_empty(self, get_account):
        rows = {
            7: {"id": 7, "email": "one@example.com", "registration_password": "pass1", "totp_secret": "TOTP1"},
            8: {"id": 8, "email": "two@example.com", "registration_password": "", "totp_secret": ""},
        }
        get_account.side_effect = rows.get
        response = self.client.post(
            "/api/accounts/secret-bulk",
            json={"account_ids": [7, 8], "field": "copy_line"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [item["value"] for item in payload["values"]],
            ["one@example.com | pass1 | TOTP1", "two@example.com |  | "],
        )
        self.assertEqual(payload["skipped"], [])


if __name__ == "__main__":
    unittest.main()
