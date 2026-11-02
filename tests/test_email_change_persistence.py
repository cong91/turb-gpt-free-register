import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import app_state_db, db


class AccountEmailPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"))
        self.stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"))
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"))
        self.stack.enter_context(patch.object(db, "_TOKENS_TXT", root / "tokens.txt"))
        self.stack.enter_context(patch.object(db, "_VIEWER_HTML", root / "viewer.html"))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_update_account_email_rewrites_account_identity_and_copy_line(self):
        db.insert_account(
            email="old@example.com",
            access_token="access-token",
            registration_password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

        self.assertTrue(db.update_account_email("old@example.com", "new@example.com"))
        self.assertIsNone(db.get_account_by_email("old@example.com"))
        account = db.get_account_by_email("new@example.com")
        self.assertEqual(account["email"], "new@example.com")
        self.assertIn("new@example.com", account["copy_line"])
        self.assertEqual(account["registration_password"], "secret")

    def test_update_account_email_rewrites_registered_email_export(self):
        db.import_codex_credential_accounts([
            {
                "email": "old@example.com",
                "registration_password": "secret",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            }
        ])

        self.assertTrue(db.update_account_email("old@example.com", "new@example.com"))
        export = (Path(db._ACCOUNTS_TXT)).read_text(encoding="utf-8")
        self.assertIn("new@example.com", export)
        self.assertNotIn("old@example.com", export)

    def test_update_account_email_rejects_existing_target(self):
        db.insert_account(email="old@example.com", access_token="old-token")
        db.insert_account(email="new@example.com", access_token="new-token")

        with self.assertRaisesRegex(ValueError, "already exists"):
            db.update_account_email("old@example.com", "new@example.com")

    def test_update_account_email_unarchives_archived_account(self):
        db.insert_account(email="old@example.com", access_token="access-token")
        account_id = db.get_account_by_email("old@example.com")["id"]
        self.assertTrue(db.archive_account(account_id, True))
        self.assertTrue(db.get_account(account_id)["archived"])

        self.assertTrue(db.update_account_email("old@example.com", "new@example.com"))

        self.assertIsNone(db.get_account_by_email("old@example.com"))
        account = db.get_account_by_email("new@example.com")
        self.assertFalse(account["archived"])
        self.assertIsNone(account["archived_at"])
        self.assertEqual(account["email"], "new@example.com")

    def test_personal_info_change_batch_accepts_password_mode(self):
        app_state_path = Path(self.temp_dir.name) / "app-state.sqlite3"
        with patch.object(app_state_db, "APP_STATE_DB_PATH", app_state_path):
            account_id = db.insert_account(
                email="changed@example.com",
                access_token="access-token",
                registration_password="password",
            )
            batch = db.save_personal_info_change_batch(
                "pw-batch-1",
                "password",
                [
                    {
                        "ok": True,
                        "account_id": account_id,
                        "email": "changed@example.com",
                        "change_status": "success",
                    },
                ],
            )
            rows = db.get_personal_info_change_export_rows("pw-batch-1")
            latest = db.get_personal_info_change_batch()

        self.assertEqual(batch["mode"], "password")
        self.assertEqual(batch["exportable_count"], 1)
        self.assertEqual([row["email"] for row in rows], ["changed@example.com"])
        self.assertEqual(latest["batch_id"], "pw-batch-1")

    def test_personal_info_change_batch_resolves_export_rows_from_persistence(self):
        app_state_path = Path(self.temp_dir.name) / "app-state.sqlite3"
        with patch.object(app_state_db, "APP_STATE_DB_PATH", app_state_path):
            account_id = db.insert_account(
                email="changed@example.com",
                access_token="access-token",
                registration_password="password",
                totp_secret="NEWSECRET",
            )
            batch = db.save_personal_info_change_batch(
                "batch-1",
                "twofa",
                [
                    {
                        "ok": True,
                        "account_id": account_id,
                        "email": "changed@example.com",
                        "change_status": "success",
                        "new_totp_secret": "MUST-NOT-BE-STORED",
                        "codex": {
                            "status": "failed",
                            "ok": False,
                            "reason": "Codex login failed safely",
                            "password": "MUST-NOT-BE-STORED",
                            "callback_url": "https://auth.example/callback?code=secret",
                        },
                    },
                    {
                        "ok": False,
                        "email": "failed@example.com",
                        "change_status": "failed",
                        "error": "browser failed",
                    },
                ],
            )

            rows = db.get_personal_info_change_export_rows("batch-1")

        self.assertEqual(batch["exportable_count"], 1)
        self.assertEqual([row["email"] for row in rows], ["changed@example.com"])
        self.assertNotIn("MUST-NOT-BE-STORED", str(batch))
        self.assertEqual(batch["results"][0]["codex"]["status"], "failed")
        self.assertNotIn("password", batch["results"][0]["codex"])
        self.assertNotIn("callback_url", batch["results"][0]["codex"])
