import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.account_export import checkpoint_account_data, save_account_data


class GmailCdkAccountExportTests(unittest.TestCase):
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.db.insert_account", return_value=19)
    def test_checkpoint_consumes_batch_alias_before_twofa(self, insert_account, mark_consumed):
        row_id = checkpoint_account_data(
            email="alias@gmail.com",
            access_token="token",
            email_source="gmail_api_url",
        )

        self.assertEqual(row_id, 19)
        mark_consumed.assert_called_once_with("alias@gmail.com")

    @patch("core.bamboommo_client.get_account_context_metadata")
    @patch("core.db.insert_account", return_value=20)
    def test_checkpoint_persists_bamboommo_rental_metadata_before_twofa(
        self, insert_account, get_metadata
    ):
        get_metadata.return_value = {
            "source": "bamboommo",
            "rental_id": "R-CHECKPOINT",
            "server": 2,
            "can_request_next_otp": True,
        }

        row_id = checkpoint_account_data(
            email="alias@gmail.com",
            access_token="token",
            email_source="bamboommo",
            extra={"email_service": {"existing": "value"}},
        )

        self.assertEqual(row_id, 20)
        self.assertEqual(
            insert_account.call_args.kwargs["extra"]["email_service"]["rental_id"],
            "R-CHECKPOINT",
        )

    @patch("core.otpgmail_client.get_account_context_metadata")
    @patch("core.db.insert_account", return_value=21)
    def test_checkpoint_persists_otpmail_order_metadata_before_twofa(
        self, insert_account, get_metadata
    ):
        get_metadata.return_value = {
            "source": "otpmail",
            "email": "alias@gmail.com",
            "query_email": "alias@gmail.com",
            "order_id": "O-CHECKPOINT",
            "service_code": "dr",
        }

        row_id = checkpoint_account_data(
            email="alias@gmail.com",
            access_token="token",
            email_source="otpmail",
            extra={"email_service": {"existing": "value"}},
        )

        self.assertEqual(row_id, 21)
        self.assertEqual(
            insert_account.call_args.kwargs["extra"]["email_service"],
            {
                "existing": "value",
                "source": "otpmail",
                "email": "alias@gmail.com",
                "query_email": "alias@gmail.com",
                "order_id": "O-CHECKPOINT",
                "service_code": "dr",
            },
        )

    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True})
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.gmail_123452026_client.get_account_context")
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=17)
    def test_successful_persistence_saves_source_cdk_before_consuming_reservation(
        self, insert_account, _archive, get_context, mark_consumed, _enqueue
    ):
        get_context.return_value.cdk = "CDK-EXACT"
        row_id = save_account_data(
            email="abcdef@gmail.com",
            access_token="token",
            email_source="gmail_123452026",
        )

        self.assertEqual(row_id, 17)
        self.assertEqual(insert_account.call_args.kwargs["source_cdk"], "CDK-EXACT")
        mark_consumed.assert_called_once_with("abcdef@gmail.com")

    @patch("core.remail_client.get_account_context_metadata")
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=23)
    def test_successful_persistence_saves_remail_service_metadata(
        self, insert_account, _archive, mark_consumed, get_metadata
    ):
        get_metadata.return_value = {
            "source": "remail",
            "email": "fresh@outlook.test",
            "service_token": "st-test-token",
            "order_no": "R-EXACT",
            "project_id": 2,
            "email_suffix": "outlook.com",
        }

        row_id = save_account_data(
            email="fresh@outlook.test",
            access_token="access-token",
            email_source="remail",
            extra={"email_service": {"existing": "value"}},
        )

        self.assertEqual(row_id, 23)
        self.assertEqual(
            insert_account.call_args.kwargs["extra"]["email_service"],
            {
                "existing": "value",
                "source": "remail",
                "email": "fresh@outlook.test",
                "service_token": "st-test-token",
                "order_no": "R-EXACT",
                "project_id": 2,
                "email_suffix": "outlook.com",
            },
        )
        mark_consumed.assert_called_once_with("fresh@outlook.test")

    @patch("core.otpgmail_client.get_account_context_metadata")
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=24)
    def test_successful_persistence_saves_otpmail_order_metadata(
        self, insert_account, _archive, mark_consumed, get_metadata
    ):
        get_metadata.return_value = {
            "source": "otpmail",
            "email": "fresh@gmail.com",
            "query_email": "fresh@gmail.com",
            "order_id": "O-EXACT",
            "service_code": "openai",
        }

        row_id = save_account_data(
            email="fresh@gmail.com",
            access_token="access-token",
            email_source="otpmail",
            extra={"email_service": {"existing": "value"}},
        )

        self.assertEqual(row_id, 24)
        self.assertEqual(
            insert_account.call_args.kwargs["extra"]["email_service"],
            {
                "existing": "value",
                "source": "otpmail",
                "email": "fresh@gmail.com",
                "query_email": "fresh@gmail.com",
                "order_id": "O-EXACT",
                "service_code": "openai",
            },
        )
        mark_consumed.assert_called_once_with("fresh@gmail.com")

    @patch("core.bamboommo_client.get_account_context_metadata")
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=25)
    def test_successful_persistence_saves_bamboommo_rental_metadata(
        self, insert_account, _archive, mark_consumed, get_metadata
    ):
        get_metadata.return_value = {
            "source": "bamboommo",
            "email": "fresh@gmail.com",
            "query_email": "fresh@gmail.com",
            "rental_id": "R-EXACT",
            "server": 2,
            "code_type_mail": "GM",
            "code_service": "OP",
        }

        row_id = save_account_data(
            email="fresh@gmail.com",
            access_token="access-token",
            email_source="bamboommo",
            extra={"email_service": {"existing": "value"}},
        )

        self.assertEqual(row_id, 25)
        self.assertEqual(insert_account.call_args.kwargs["extra"]["email_service"]["rental_id"], "R-EXACT")
        mark_consumed.assert_called_once_with("fresh@gmail.com")

    def test_batch_archive_uses_canonical_account_line(self):
        from core import account_export

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "注册成功账号.json").write_text("[]\n", encoding="utf-8")
            with patch("core.db.get_account", return_value={
                "id": 7,
                "email": "user@example.com",
                "registration_password": "openai-pass",
                "totp_secret": "TOTPSECRET",
                "original_email_line": "user@example.com----mailbox----client----refresh",
            }):
                account_export._append_batch_archive(
                    row_id=7,
                    email="user@example.com",
                    access_token="access-token",
                    totp_secret="TOTPSECRET",
                    email_source="outlook",
                    proxy_used=None,
                    extra={"registration_password": "openai-pass"},
                    batch_dir=folder,
                )

            line = (folder / "注册成功整行.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(line, "user@example.com | openai-pass | TOTPSECRET")
            self.assertNotIn("access-token", line)
            self.assertNotIn("mailbox----", line)

    @patch("core.account_export.setup_2fa")
    def test_browser_2fa_adapter_uses_existing_context(self, setup_2fa):
        from core.account_export import setup_2fa_in_browser

        setup_2fa.return_value = "SECRET"
        context = object()
        page = object()
        self.assertEqual(
            setup_2fa_in_browser(context, page, "user@example.com"),
            "SECRET",
        )
        transport = setup_2fa.call_args.args[0]
        self.assertIs(transport.context, context)
        self.assertIs(transport.page, page)
        self.assertEqual(setup_2fa.call_args.args[1], "user@example.com")
        self.assertEqual(setup_2fa.call_args.kwargs["reauth"], False)


if __name__ == "__main__":
    unittest.main()
