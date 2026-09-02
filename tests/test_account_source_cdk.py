import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountSourceCdkTests(unittest.TestCase):
    def test_insert_preserves_source_cdk_and_copy_line_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts_path = root / "accounts.json"
            accounts_path.write_text("[]\n", encoding="utf-8")
            outlook_path = root / "outlook.json"
            outlook_path.write_text("[]\n", encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"
            ), patch.object(db, "_OUTLOOK_JSON", outlook_path), patch.object(
                db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"
            ), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(
                db, "_TOKENS_TXT", root / "tokens.txt"
            ), patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), patch.object(
                db, "_VIEWER_HTML", root / "viewer.html"
            ):
                account_id = db.insert_account(
                    email="abcdef@gmail.com",
                    access_token="token-one",
                    email_source="gmail_123452026",
                    source_cdk="CDK-EXACT",
                )
                db.insert_account(
                    email="abcdef@gmail.com",
                    access_token="token-two",
                    email_source="gmail_123452026",
                )
                account = db.get_account(account_id)

        self.assertEqual(account["source_cdk"], "CDK-EXACT")
        self.assertNotIn("CDK-EXACT", account["copy_line"])

    def test_registration_password_is_persisted_and_exported_without_mailbox_material(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts_path = root / "accounts.json"
            accounts_path.write_text("[]\n", encoding="utf-8")
            outlook_path = root / "outlook.json"
            outlook_path.write_text(json.dumps([{
                "id": 1,
                "email": "abcdef@gmail.com",
                "password": "mailbox-password",
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "status": "available",
            }]), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), patch.object(
                db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"
            ), patch.object(db, "_OUTLOOK_JSON", outlook_path), patch.object(
                db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"
            ), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(
                db, "_TOKENS_TXT", root / "tokens.txt"
            ), patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"), patch.object(
                db, "_VIEWER_HTML", root / "viewer.html"
            ):
                account_id = db.insert_account(
                    email="abcdef@gmail.com",
                    access_token="access-token",
                    totp_secret="TOTPSECRET",
                    extra={"registration_password": "openai-password"},
                )
                account = db.get_account(account_id)

        self.assertEqual(account["registration_password"], "openai-password")
        self.assertEqual(account["password"], "mailbox-password")
        self.assertEqual(account["copy_line"], "abcdef@gmail.com | openai-password | TOTPSECRET")
        self.assertNotIn("access-token", account["copy_line"])
        self.assertNotIn("mailbox-password", account["copy_line"])
        self.assertNotIn("client-id", account["copy_line"])

    def test_legacy_account_line_preserves_original_material_and_token(self):
        row = {
            "email": "user@example.com",
            "original_email_line": "user@example.com----mailbox----client----refresh",
            "access_token": "access-token",
            "registration_password": "openai-pass",
            "totp_secret": "TOTPSECRET",
        }
        self.assertEqual(
            db.account_line(row, "legacy"),
            "user@example.com----mailbox----client----refresh----access-token----TOTPSECRET",
        )

    def test_invalid_account_line_format_is_rejected(self):
        with self.assertRaises(ValueError):
            db.account_line({"email": "user@example.com"}, "unknown")

        self.assertEqual(
            db._account_line({"email": "user@example.com", "registration_password": "pass"}),
            "user@example.com | pass | ",
        )


if __name__ == "__main__":
    unittest.main()
