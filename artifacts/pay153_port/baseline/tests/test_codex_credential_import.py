import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class CodexCredentialImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.accounts_path = root / "accounts.json"
        self.accounts_path.write_text("[]\n", encoding="utf-8")
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", self.accounts_path))
        self.stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"))
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"))
        self.stack.enter_context(patch.object(db, "_TOKENS_TXT", root / "tokens.txt"))
        self.stack.enter_context(patch.object(db, "_VIEWER_HTML", root / "viewer.html"))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_pipe_credentials_import_creates_account_without_access_token(self):
        response = self.client.post(
            "/api/outlook/import",
            json={
                "source": "credentials",
                "text": "user@example.com | pa|ssword | JBSWY3DPEHPK3PXP",
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["inserted"], 1)
        account = db.get_account_by_email("user@example.com")
        self.assertEqual(account["access_token"], "")
        self.assertEqual(account["registration_password"], "pa|ssword")
        self.assertEqual(account["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(account["codex_login_mode"], "credentials")
        self.assertEqual(account["twofa_status"], "active")

    def test_pipe_credentials_import_enriches_existing_account_without_token(self):
        db.insert_account(
            email="user@example.com",
            access_token="",
            email_source="outlook",
            codex_status="failed",
            codex_error="old failure",
        )

        response = self.client.post(
            "/api/outlook/import",
            json={
                "source": "credentials",
                "text": "user@example.com | new-password | JBSWY3DPEHPK3PXP",
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["updated"], 1)
        account = db.get_account_by_email("user@example.com")
        self.assertEqual(account["registration_password"], "new-password")
        self.assertEqual(account["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(account["email_source"], "outlook")
        self.assertEqual(account["codex_status"], "failed")
        self.assertEqual(account["codex_error"], "old failure")

    def test_pipe_credentials_import_rejects_incomplete_rows(self):
        response = self.client.post(
            "/api/outlook/import",
            json={"source": "credentials", "text": "user@example.com | password |"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("USER | PASS | 2FA", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
