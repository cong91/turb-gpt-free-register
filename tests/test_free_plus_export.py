# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import db, free_plus_export
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

    def test_mark_exported_rejects_the_whole_batch_when_one_account_was_already_exported(self):
        with TemporaryDirectory() as temp_dir:
            accounts_path = Path(temp_dir) / "accounts.json"
            accounts_txt_path = Path(temp_dir) / "accounts.txt"
            tokens_txt_path = Path(temp_dir) / "tokens.txt"
            accounts = [
                self._eligible(7),
                {**self._eligible(8), "free_plus_exported_at": "2026-08-04T10:00:00"},
            ]
            with (
                patch.object(db, "_ACCOUNTS_JSON", accounts_path),
                patch.object(db, "_ACCOUNTS_TXT", accounts_txt_path),
                patch.object(db, "_TOKENS_TXT", tokens_txt_path),
                patch.object(db, "_render_static_viewer"),
            ):
                db._write_json(accounts_path, accounts)

                updated, skipped = db.mark_accounts_free_plus_exported([7, 8], format_name="modern")
                persisted = db._read_json(accounts_path, [])

        self.assertEqual(updated, [])
        self.assertEqual(skipped[0]["reason"], "已导出")
        self.assertIsNone(persisted[0].get("free_plus_exported_at"))

    @patch("webui.app.db.mark_accounts_free_plus_exported")
    @patch("core.free_plus_export.prepare_export")
    def test_export_marks_and_archives_before_download_url_is_used(self, prepare_export, mark_exported):
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
        mark_exported.assert_called_once_with([7], format_name="modern")
        downloaded = self.client.get(prepared.get_json()["download_url"], headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"account-line\n")
        self.assertIn("no-store", downloaded.headers.get("Cache-Control") or "")
        self.assertEqual(self.client.get(prepared.get_json()["download_url"], headers=self.headers).status_code, 404)

    @patch("webui.app.db.mark_accounts_free_plus_exported", return_value=([], [{"id": 7, "reason": "已导出"}]))
    @patch("core.free_plus_export.prepare_export")
    def test_export_does_not_issue_download_when_state_commit_conflicts(self, prepare_export, _mark_exported):
        prepare_export.return_value = {
            "content": b"account-line\n",
            "filename": "free-plus-modern.txt",
            "format": "modern",
            "accounts": [{"id": 7, "email": "user@example.com"}],
            "account_ids": [7],
            "count": 1,
            "skipped": [],
        }

        response = self.client.post(
            "/api/accounts/free-plus/export",
            json={"scope": "selected", "account_ids": [7], "format": "modern"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("download_url", response.get_json())

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

    @patch("core.free_plus_export.db.list_accounts")
    def test_prepare_recovery_export_rebuilds_latest_exported_batch(self, list_accounts):
        list_accounts.return_value = [
            {
                **self._eligible(7),
                "free_plus_exported_at": "2026-08-20T22:16:44",
                "free_plus_export_format": "modern",
                "archived": True,
            },
            {
                **self._eligible(8),
                "free_plus_exported_at": "2026-08-20T22:16:44",
                "free_plus_export_format": "modern",
                "archived": True,
            },
            {
                **self._eligible(9),
                "free_plus_exported_at": "2026-08-20T18:03:15",
                "free_plus_export_format": "legacy",
                "archived": True,
            },
        ]

        result = free_plus_export.prepare_recovery_export()

        self.assertEqual(result["exported_at"], "2026-08-20T22:16:44")
        self.assertEqual(result["account_ids"], [7, 8])
        self.assertEqual(result["format"], "modern")
        self.assertIn(b"user7@example.com | openai-pass | TOTPSECRET", result["content"])
        self.assertNotIn(b"user9@example.com", result["content"])

    @patch("webui.app.free_plus_export.prepare_recovery_export")
    def test_recovery_latest_returns_one_time_download(self, prepare_recovery_export):
        prepare_recovery_export.return_value = {
            "content": b"account-line\n",
            "filename": "free-plus-recovery-modern.txt",
            "format": "modern",
            "exported_at": "2026-08-20T22:16:44",
            "account_ids": [7],
            "count": 1,
        }

        response = self.client.post(
            "/api/accounts/free-plus/recover-latest",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        download_url = response.get_json()["download_url"]
        downloaded = self.client.get(download_url, headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"account-line\n")
        self.assertEqual(self.client.get(download_url, headers=self.headers).status_code, 404)

    def test_free_plus_export_requires_authentication(self):
        response = self.client.post(
            "/api/accounts/free-plus/export",
            json={"scope": "selected", "account_ids": [7]},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
