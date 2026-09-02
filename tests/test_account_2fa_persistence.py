import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountTwofaPersistenceTests(unittest.TestCase):
    def test_gmail_api_url_last_otp_is_persisted_by_code_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            patches = (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", root / "gmail.json"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", root / "gmail.txt"),
            )
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            self.assertEqual(
                db.import_gmail_api_url_emails([
                    {"email": "user@gmail.com", "code_url": "https://api.example/code"},
                ]),
                (1, 0),
            )
            self.assertTrue(db.record_gmail_api_url_otp("https://api.example/code", "123456"))
            self.assertEqual(
                db.get_gmail_api_url_last_otp("https://api.example/code"),
                "123456",
            )

    def test_update_account_2fa_preserves_auth_and_codex_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts_path = root / "accounts.json"
            accounts_path.write_text("[]\n", encoding="utf-8")
            patches = (
                patch.object(db, "_ACCOUNTS_JSON", accounts_path),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            )
            for item in patches:
                item.start()
                self.addCleanup(item.stop)

            account_id = db.insert_account(
                email="user@example.com",
                access_token="access-token",
                registration_password="openai-password",
                source_cdk="MAIL-CARD",
                codex_status="failed",
                twofa_status="pending",
            )
            self.assertTrue(
                db.update_account_2fa(
                    account_id,
                    status="failed",
                    error="TimeoutException: script timeout",
                )
            )
            row = db.get_account(account_id)

        self.assertEqual(row["access_token"], "access-token")
        self.assertEqual(row["registration_password"], "openai-password")
        self.assertEqual(row["source_cdk"], "MAIL-CARD")
        self.assertEqual(row["codex_status"], "failed")
        self.assertEqual(row["twofa_status"], "failed")
        self.assertEqual(row["twofa_error"], "TimeoutException: script timeout")
        self.assertIsNone(row["totp_secret"])


if __name__ == "__main__":
    unittest.main()
