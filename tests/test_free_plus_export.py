# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import free_plus_export
from webui.app import create_app


class FreePlusExportTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    @staticmethod
    def _eligible(account_id: int = 7) -> dict:
        return {
            "id": account_id,
            "email": f"user{account_id}@example.com",
            "registration_password": "openai-pass",
            "totp_secret": "TOTPSECRET",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }

    def test_free_plus_predicate_excludes_paid_plus_and_ineligible_free(self):
        self.assertTrue(free_plus_export.is_free_plus_account(self._eligible()))
        self.assertFalse(free_plus_export.is_free_plus_account({"current_plan_type": "plus", "plus_trial_eligible": True}))
        self.assertFalse(free_plus_export.is_free_plus_account({"current_plan_type": "free", "plus_trial_eligible": False}))

    @patch("core.free_plus_export.db.get_account")
    def test_prepare_selected_skips_ineligible_and_previously_exported(self, get_account):
        rows = {
            7: self._eligible(7),
            8: {**self._eligible(8), "free_plus_exported_at": "2026-08-04T10:00:00"},
            9: {**self._eligible(9), "plus_trial_eligible": False},
        }
        get_account.side_effect = rows.get

        result = free_plus_export.prepare_export(
            scope="selected",
            account_ids=[7, 8, 9],
            format_name="modern",
        )

        self.assertEqual(result["account_ids"], [7])
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertIn(b"user7@example.com | openai-pass | TOTPSECRET", result["content"])

    @patch("webui.app.db.mark_accounts_free_plus_exported")
    @patch("core.free_plus_export.prepare_export")
    def test_download_marks_and_archives_only_when_download_url_is_used(self, prepare_export, mark_exported):
        prepare_export.return_value = {
            "content": b"account-line\n",
            "filename": "free-plus-modern.txt",
            "format": "modern",
            "accounts": [{"id": 7, "email": "user@example.com"}],
            "account_ids": [7],
            "count": 1,
            "skipped": [],
        }
        mark_exported.return_value = ([{"id": 7, "archived": True}], [])

        prepared = self.client.post(
            "/api/accounts/free-plus/export",
            json={"scope": "selected", "account_ids": [7], "format": "modern"},
            headers=self.headers,
        )

        self.assertEqual(prepared.status_code, 200)
        mark_exported.assert_not_called()
        downloaded = self.client.get(prepared.get_json()["download_url"], headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"account-line\n")
        self.assertIn("no-store", downloaded.headers.get("Cache-Control") or "")
        mark_exported.assert_called_once_with([7], format_name="modern")

    @patch("webui.app.db.set_accounts_free_plus_export_state")
    def test_manual_export_state_does_not_archive_through_api(self, set_state):
        set_state.return_value = ([{"id": 7, "archived": False}], [])

        response = self.client.post(
            "/api/accounts/free-plus/export-state",
            json={"account_ids": [7], "exported": True},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        set_state.assert_called_once_with([7], exported=True)
        self.assertFalse(response.get_json()["updated"][0]["archived"])

    def test_free_plus_export_requires_authentication(self):
        response = self.client.post(
            "/api/accounts/free-plus/export",
            json={"scope": "selected", "account_ids": [7]},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
