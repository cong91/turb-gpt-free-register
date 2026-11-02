import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, email_change
from core.email_change import EmailChangeInput
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.gmail_api_url_client import (
    GmailApiUrlAccount,
    GmailApiUrlError,
    poll_verification_code,
)


class EmailChangeOtpTests(unittest.TestCase):
    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.gmail_api_url_client._batch_store")
    def test_email_change_602_quarantines_raw_source_before_retry(
        self, mock_batch_store, mock_get
    ):
        """The direct email-change polling path must persist the terminal state."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_json = root / "gmail-pool.json"
            raw_txt = root / "gmail-pool.txt"
            sqlite_path = root / "turb.sqlite3"
            store = GmailApiUrlBatchStore(root / "gmail-state.sqlite3")
            code_url = "https://mail.example/terminal"
            item = EmailChangeInput(
                old_email="old@example.com",
                password="secret",
                totp_secret="JBSWY3DPEHPK3PXP",
                new_email="alias@example.com",
                code_url=code_url,
            )

            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", raw_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", raw_txt),
                patch.object(db, "_SQLITE_PATH", sqlite_path),
                patch.object(db, "_DEFAULT_SQLITE_PATH", sqlite_path),
                patch.object(db, "_SQLITE_READY", False),
                patch.object(db, "_SQLITE_READY_PATH", None),
            ):
                db.import_gmail_api_url_emails([
                    {"email": "source@example.com", "code_url": code_url},
                ])
                mock_batch_store.return_value = store
                response = Mock(status_code=200)
                response.json.return_value = {"code": 602, "message": "expired"}
                mock_get.return_value = response

                with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*expired"):
                    email_change.poll_new_email_otp(
                        item,
                        after_ts=1.0,
                        before_code=None,
                    )

                self.assertEqual(
                    db.get_gmail_api_url_email_by_email("source@example.com")["status"],
                    "failed",
                )
                mock_get.reset_mock()
                with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*quarantined"):
                    email_change.poll_new_email_otp(
                        item,
                        after_ts=1.0,
                        before_code=None,
                    )
                mock_get.assert_not_called()

    def test_classifies_recorded_settings_and_verification_states(self):
        classify = email_change.classify_email_change_state
        self.assertEqual(
            classify({"url": "https://chatgpt.com/settings", "body": "Account", "inputs": []}),
            "settings",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://chatgpt.com/",
                    "body": "Enter verification code",
                    "inputs": [{"id": "verification_code", "autocomplete": "one-time-code"}],
                }
            ),
            "email_otp",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://auth.openai.com/mfa/totp",
                    "body": "Enter the code from your authenticator app",
                    "inputs": [{"name": "code", "autocomplete": "one-time-code"}],
                }
            ),
            "totp",
        )
        self.assertEqual(
            classify(
                {
                    "url": "https://chatgpt.com/auth/login",
                    "body": "Log in",
                    "inputs": [{"name": "email", "type": "email"}],
                }
            ),
            "unknown",
        )

    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch(
        "core.gmail_api_url_client._fetch_code_once",
        side_effect=[(0, "111111"), (0, "222222")],
    )
    def test_gmail_api_polling_does_not_log_otp_values(self, _fetch, _sleep):
        account = GmailApiUrlAccount("new@example.com", "https://mail.example/otp/1")

        with self.assertLogs("core.gmail_api_url_client", level="INFO") as captured:
            code = poll_verification_code(
                account,
                max_wait=5,
                poll_interval=0,
                after_ts=1.0,
                before_code="111111",
            )

        self.assertEqual(code, "222222")
        output = "\n".join(captured.output)
        self.assertNotIn("111111", output)
        self.assertNotIn("222222", output)

    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch("core.gmail_api_url_client._fetch_code_once", return_value=(0, "222222"))
    def test_explicit_empty_baseline_accepts_first_new_otp(self, fetch, _sleep):
        account = GmailApiUrlAccount("new@example.com", "https://mail.example/otp/1")

        code = poll_verification_code(
            account,
            max_wait=0.01,
            poll_interval=0,
            after_ts=1.0,
            before_code=None,
        )

        self.assertEqual(code, "222222")
        fetch.assert_called_once()
