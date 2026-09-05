"""Gmail API URL provider 集成测试（全程 mock）。"""
import unittest
from unittest.mock import patch

from core import email_provider


class GmailApiUrlProviderTests(unittest.TestCase):
    """Gmail API URL provider 集成测试套件。"""

    def test_parse_sources_includes_gmail_api_url(self):
        """parse_email_sources 保留 gmail_api_url。"""
        sources = email_provider.parse_email_sources("outlook,gmail_api_url,generic_api")

        self.assertIn("gmail_api_url", sources)
        self.assertEqual(sources, ["outlook", "gmail_api_url", "generic_api"])

    @patch("core.gmail_api_url_client.pick_account")
    def test_acquire_routes_to_gmail_api_url_client(self, mock_pick):
        """acquire_email 路由到 gmail_api_url_client.pick_account。"""
        mock_pick.return_value.email = "acquired@gmail.com"

        with patch("core.email_provider.parse_email_sources", return_value=["gmail_api_url"]):
            email = email_provider.acquire_email(job_id=17)

        self.assertEqual(email, "acquired@gmail.com")
        mock_pick.assert_called_once()

    @patch("core.db.get_gmail_api_url_email_by_email")
    def test_resolve_source_recognizes_gmail_api_url_context(self, mock_get):
        """resolve_email_source 识别 gmail_api_url 上下文。"""
        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}

        source = email_provider.resolve_email_source("test@gmail.com")

        self.assertEqual(source, "gmail_api_url")
        mock_get.assert_called_once_with("test@gmail.com")

    @patch("core.email_provider._current_otp_job_id", return_value=17)
    @patch(
        "core.db.get_job",
        return_value={
            "id": 17,
            "email": "alias@gmail.com",
            "email_source": "gmail_api_url",
        },
    )
    @patch("core.email_provider._registered_email_source", return_value=None)
    @patch("core.qan8_gmail_api_allocator.Qan8GmailApiAllocator.get_account_context")
    def test_active_job_source_wins_when_alias_exists_in_two_providers(
        self, mock_qan8_context, _mock_registered, _mock_job, _mock_job_id
    ):
        mock_qan8_context.return_value = object()

        source = email_provider.resolve_email_source("alias@gmail.com")

        self.assertEqual(source, "gmail_api_url")
        mock_qan8_context.assert_not_called()

    @patch("core.db.get_gmail_api_url_email_by_email")
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="654321")
    def test_wait_for_otp_routes_to_gmail_api_url_client(self, mock_poll, mock_get):
        """wait_for_otp 路由到 gmail_api_url_client.poll_verification_code。"""
        from config import email as email_config

        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}

        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            code = email_provider.wait_for_otp("test@gmail.com", after_ts=123.0, max_wait=60)

        self.assertEqual(code, "654321")
        mock_poll.assert_called_once()
        call_args = mock_poll.call_args
        self.assertEqual(call_args[0][0].email, "test@gmail.com")

    @patch("core.email_provider._get_code_url_account")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.is_gmail_api_url_account_blocked", return_value=True)
    @patch("core.db.is_gmail_api_url_code_url_failed", return_value=False)
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="654321")
    def test_wait_for_otp_rejects_blocked_raw_root_before_provider(
        self,
        mock_poll,
        mock_failed_url,
        mock_account_blocked,
        mock_batch_store,
        _mock_source,
        mock_account,
    ):
        from pathlib import Path

        from core.gmail_api_url_client import GmailApiUrlError

        mock_batch_store.return_value.path = Path("runtime") / "turb.sqlite3"
        mock_account.return_value = type(
            "Account",
            (),
            {
                "email": "disabled.root+alias@gmail.com",
                "code_url": "http://example.com/otp",
            },
        )()

        with self.assertRaisesRegex(GmailApiUrlError, "source is disabled"):
            email_provider.wait_for_otp("disabled.root+alias@gmail.com", after_ts=123.0)

        mock_account_blocked.assert_called_once_with(
            "disabled.root+alias@gmail.com",
            sqlite_path=Path("runtime") / "turb.sqlite3",
        )
        mock_failed_url.assert_called_once_with(
            "http://example.com/otp",
            sqlite_path=Path("runtime") / "turb.sqlite3",
        )
        mock_poll.assert_not_called()

    @patch("core.email_provider._quarantine_code_url_after_provider_error")
    @patch("core.email_provider._get_code_url_account")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.is_gmail_api_url_account_blocked", return_value=True)
    @patch("core.db.is_gmail_api_url_code_url_failed", return_value=True)
    @patch("core.gmail_api_url_client.poll_verification_code")
    def test_wait_for_otp_preserves_602_for_quarantined_url(
        self,
        mock_poll,
        mock_url_failed,
        _mock_account_blocked,
        mock_batch_store,
        _mock_source,
        mock_account,
        quarantine,
    ):
        from pathlib import Path

        from core.gmail_api_url_client import GmailApiUrlError

        mock_batch_store.return_value.path = Path("runtime") / "turb.sqlite3"
        mock_account.return_value = type(
            "Account",
            (),
            {"email": "failed.root+alias@gmail.com", "code_url": "http://example.com/otp"},
        )()

        with self.assertRaisesRegex(GmailApiUrlError, "code=602"):
            email_provider.wait_for_otp("failed.root+alias@gmail.com", after_ts=123.0)

        mock_url_failed.assert_called_once_with(
            "http://example.com/otp",
            sqlite_path=Path("runtime") / "turb.sqlite3",
        )
        mock_poll.assert_not_called()
        quarantine.assert_called_once()

    @patch("core.email_provider._get_code_url_account")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.is_gmail_api_url_account_blocked", return_value=True)
    @patch("core.db.is_gmail_api_url_code_url_failed", return_value=False)
    @patch("core.gmail_api_url_client.snapshot_verification_code", return_value="654321")
    def test_snapshot_rejects_blocked_raw_root_before_provider(
        self,
        mock_snapshot,
        mock_failed_url,
        mock_account_blocked,
        mock_batch_store,
        _mock_source,
        mock_account,
    ):
        from pathlib import Path

        from core.gmail_api_url_client import GmailApiUrlError

        mock_batch_store.return_value.path = Path("runtime") / "turb.sqlite3"
        mock_account.return_value = type(
            "Account",
            (),
            {
                "email": "exhaustedroot+alias@gmail.com",
                "code_url": "http://example.com/otp",
            },
        )()

        with self.assertRaisesRegex(GmailApiUrlError, "source is disabled"):
            email_provider.snapshot_verification_code("exhaustedroot+alias@gmail.com")

        mock_account_blocked.assert_called_once_with(
            "exhaustedroot+alias@gmail.com",
            sqlite_path=Path("runtime") / "turb.sqlite3",
        )
        mock_failed_url.assert_called_once_with(
            "http://example.com/otp",
            sqlite_path=Path("runtime") / "turb.sqlite3",
        )
        mock_snapshot.assert_not_called()

    @patch("core.db.get_gmail_api_url_email_by_email")
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="222222")
    def test_wait_for_otp_forwards_previous_code_as_stale_guard(self, mock_poll, mock_get):
        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}

        with patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"), \
             patch.object(email_provider, "parse_email_sources", return_value=["gmail_api_url"]):
            email_provider.wait_for_otp(
                "test@gmail.com",
                after_ts=456.0,
                before_code="111111",
            )

        self.assertEqual(mock_poll.call_args.kwargs["before_code"], "111111")

    @patch("core.db.get_gmail_api_url_email_by_email")
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="222222")
    def test_wait_for_otp_forwards_explicit_empty_baseline(self, mock_poll, mock_get):
        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}

        with patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"):
            email_provider.wait_for_otp(
                "test@gmail.com",
                after_ts=456.0,
                before_code=None,
                stage="registration_email_otp",
            )

        self.assertIsNone(mock_poll.call_args.kwargs["before_code"])
        self.assertEqual(mock_poll.call_args.kwargs["stage"], "registration_email_otp")

    @patch("core.db.get_gmail_api_url_email_by_email")
    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=None)
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="654321")
    def test_wait_for_otp_uses_configured_defaults(self, mock_poll, _mock_batch, mock_get):
        from config import email as email_config

        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), \
             patch.object(email_config, "OTP_MAX_WAIT", 17), \
             patch.object(email_config, "OTP_POLL_INTERVAL", 4):
            email_provider.wait_for_otp("test@gmail.com", after_ts=123.0)

        assert mock_poll.call_args.kwargs["max_wait"] == 17
        assert mock_poll.call_args.kwargs["poll_interval"] == 4

    @patch("core.gmail_api_url_client.get_batch_account_context")
    @patch("core.db.get_gmail_api_url_email_by_email", return_value=None)
    @patch("core.gmail_api_url_client.poll_verification_code", return_value="654321")
    def test_batch_alias_keeps_stale_guard_after_resend(
        self, mock_poll, _mock_get, mock_batch
    ):
        from config import email as email_config

        mock_batch.return_value = type(
            "Account", (), {"email": "alias@gmail.com", "code_url": "http://example.com/otp"}
        )()
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            email_provider.wait_for_otp("alias@gmail.com", after_ts=456.0)

        self.assertEqual(mock_poll.call_args.kwargs["after_ts"], 456.0)

    @patch("core.db.release_unconsumed_gmail_api_url_email", return_value=True)
    @patch("core.db.get_gmail_api_url_email_by_email")
    def test_release_unconsumed_routes_to_gmail_api_url_client(self, mock_get, mock_release):
        """release_email_if_unconsumed 路由到 db.release_unconsumed_gmail_api_url_email。"""
        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}
        
        result = email_provider.release_email_if_unconsumed("test@gmail.com", note="failed early")

        self.assertTrue(result)
        mock_release.assert_called_once_with("test@gmail.com", note="failed early")

    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.db.release_unconsumed_gmail_api_url_email", return_value=False)
    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=object())
    @patch("core.gmail_api_url_client.release_account", return_value=True)
    def test_release_unconsumed_releases_batch_alias_assignment(
        self, mock_release, _mock_context, _mock_db_release, _mock_source
    ):
        result = email_provider.release_email_if_unconsumed(
            "alias@gmail.com", note="registration failed"
        )

        self.assertTrue(result)
        mock_release.assert_called_once_with(
            "alias@gmail.com", status="available", note="registration failed"
        )

    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.db.release_unconsumed_gmail_api_url_email", return_value=False)
    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=object())
    @patch("core.gmail_api_url_client.release_account", return_value=True)
    def test_registration_failure_discards_batch_alias_assignment(
        self, mock_release, _mock_context, _mock_db_release, _mock_source
    ):
        result = email_provider.release_email_if_unconsumed(
            "alias@gmail.com",
            note="password step failed",
            discard_on_failure=True,
        )

        self.assertTrue(result)
        mock_release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="password step failed",
        )

    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch(
        "core.db.get_gmail_api_url_email_by_email",
        return_value={"email": "test@gmail.com", "code_url": "https://mail.example/broken"},
    )
    @patch("core.db.fail_gmail_api_url_sources_for_code_url")
    def test_provider_error_marks_gmail_api_url_email_failed(
        self, mock_release, _mock_get, _mock_source
    ):
        result = email_provider.release_email_if_unconsumed(
            "test@gmail.com",
            note="GmailApiUrlError: Provider error code=602: refund required",
        )

        self.assertTrue(result)
        mock_release.assert_called_once_with(
            "https://mail.example/broken",
            note="GmailApiUrlError: Provider error code=602: refund required",
        )

    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch(
        "core.db.get_gmail_api_url_email_by_email",
        return_value={"email": "test@gmail.com", "code_url": "https://mail.example/broken"},
    )
    @patch("core.db.fail_gmail_api_url_sources_for_code_url")
    def test_lowercase_provider_602_marks_gmail_api_url_email_failed(
        self, mock_release, _mock_get, _mock_source
    ):
        result = email_provider.release_email_if_unconsumed(
            "test@gmail.com",
            note="gmail api error: provider error code=602",
        )

        self.assertTrue(result)
        mock_release.assert_called_once_with(
            "https://mail.example/broken",
            note="gmail api error: provider error code=602",
        )

    @patch("core.email_provider._quarantine_code_url_after_provider_error")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.db.release_unconsumed_gmail_api_url_email", return_value=False)
    @patch("core.gmail_api_url_client.get_batch_account_context")
    @patch("core.gmail_api_url_client.release_account", return_value=True)
    def test_batch_602_cleanup_quarantines_code_url_before_discard(
        self,
        mock_release,
        mock_context,
        _mock_db_release,
        _mock_source,
        quarantine,
    ):
        account = type(
            "Account", (), {"email": "alias@gmail.com", "code_url": "https://mail.example/broken"}
        )()
        mock_context.return_value = account

        result = email_provider.release_email_if_unconsumed(
            "alias@gmail.com",
            note="Provider error code=602",
            discard_on_failure=True,
        )

        self.assertTrue(result)
        quarantine.assert_called_once()
        self.assertEqual(quarantine.call_args.args[0], account)
        self.assertEqual(quarantine.call_args.kwargs["source"], "gmail_api_url")
        mock_release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="Provider error code=602",
        )

    @patch("core.email_provider._quarantine_code_url_after_provider_error")
    @patch("core.email_provider._get_code_url_account")
    @patch("core.email_provider.resolve_email_source", return_value="gmail_api_url")
    @patch("core.gmail_api_url_client.release_account")
    def test_direct_release_keeps_gmail_api_url_602_terminal(
        self, mock_release, _mock_source, mock_account, quarantine
    ):
        account = type(
            "Account", (), {"email": "alias@gmail.com", "code_url": "https://mail.example/broken"}
        )()
        mock_account.return_value = account

        email_provider.release_email(
            "alias@gmail.com",
            status="available",
            note="Provider error code=602",
        )

        quarantine.assert_called_once()
        self.assertEqual(quarantine.call_args.args[0], account)
        self.assertEqual(quarantine.call_args.kwargs["source"], "gmail_api_url")
        mock_release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="Provider error code=602",
        )

    @patch("core.email_provider._quarantine_code_url_after_provider_error")
    @patch("core.email_provider._get_code_url_account")
    @patch("core.email_provider.resolve_email_source", return_value="qan8_gmail_api")
    @patch("core.gmail_api_url_client.release_account")
    def test_qan8_provenance_release_uses_canonical_gmail_api(
        self, mock_release, _mock_source, mock_account, quarantine
    ):
        account = type(
            "Account", (), {"email": "alias@gmail.com", "code_url": "https://mail.example/broken"}
        )()
        mock_account.return_value = account

        email_provider.release_email(
            "alias@gmail.com",
            status="available",
            note="Provider error code=602",
        )

        quarantine.assert_called_once()
        self.assertEqual(quarantine.call_args.args[0], account)
        self.assertEqual(quarantine.call_args.kwargs["source"], "gmail_api_url")
        mock_release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="Provider error code=602",
        )

    @patch("core.db.get_gmail_api_url_email_by_email")
    @patch("core.gmail_api_url_client.release_account")
    def test_mark_consumed_routes_to_gmail_api_url_client(self, mock_release, mock_get):
        """mark_email_consumed 路由到 gmail_api_url_client.release_account with status=used。"""
        mock_get.return_value = {"email": "test@gmail.com", "code_url": "http://example.com/otp"}
        
        email_provider.mark_email_consumed("test@gmail.com")

        mock_release.assert_called_once_with("test@gmail.com", status="used", note="")

    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=object())
    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.get_gmail_api_url_email_by_email", return_value=None)
    def test_batch_alias_consumption_completes_assignment(self, _mock_get, mock_store_factory, _mock_context):
        active = type("Assignment", (), {"assignment_id": "assignment-1"})()
        store = mock_store_factory.return_value
        store.find_active_assignment_for_alias.return_value = active

        from core.gmail_api_url_client import release_account

        release_account("alias@gmail.com", status="used")

        store.complete.assert_called_once_with("assignment-1")
        store.release.assert_not_called()

    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=object())
    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.get_gmail_api_url_email_by_email", return_value=None)
    def test_failed_batch_alias_is_discarded_instead_of_released(
        self, _mock_get, mock_store_factory, _mock_context
    ):
        active = type("Assignment", (), {"assignment_id": "assignment-2"})()
        store = mock_store_factory.return_value
        store.find_active_assignment_for_alias.return_value = active

        from core.gmail_api_url_client import release_account

        release_account("failed@gmail.com", status="available", note="registration failed")

        store.discard.assert_called_once_with(
            "assignment-2", reason="registration failed"
        )
        store.release.assert_not_called()

    @patch("core.gmail_api_url_client.pick_account", side_effect=RuntimeError("pool empty"))
    @patch("core.outlook_client.pick_account")
    def test_explicit_gmail_api_url_source_does_not_fallback(self, mock_outlook, _mock_gmail):
        """显式指定 gmail_api_url 时不回退到 outlook。"""
        with self.assertRaisesRegex(RuntimeError, "gmail_api_url"):
            email_provider.acquire_email(job_id=17, email_source="gmail_api_url")

        mock_outlook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
