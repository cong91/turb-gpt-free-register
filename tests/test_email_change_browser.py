import threading
import time
import unittest
from unittest.mock import Mock, patch

from core import email_change
from core.email_change import EmailChangeInput


class EmailChangeBrowserWorkflowTests(unittest.TestCase):
    def test_credential_login_uses_browser_email_entry(self):
        from types import SimpleNamespace

        from core import browser_email_entry, browser_page_actions

        driver = Mock()
        credentials = SimpleNamespace(email="user@example.com")
        with (
            patch.object(browser_email_entry, "_type_email_address") as type_email,
            patch.object(browser_email_entry, "_submit_email_step") as submit_email,
            patch.object(browser_page_actions, "_maybe_accept") as accept,
            patch.object(email_change, "_wait_for_change_state", return_value="authenticated"),
        ):
            email_change._login_chatgpt_with_credentials(driver, credentials)

        driver.get.assert_called_once_with("https://chatgpt.com/auth/login")
        accept.assert_called_once_with(driver)
        type_email.assert_called_once_with(driver, credentials.email, timeout=20)
        submit_email.assert_called_once_with(driver, credentials.email)

    def test_change_workflow_logs_in_polls_new_mail_and_confirms_success(self):
        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        with (
            patch.object(email_change, "_login_chatgpt_with_credentials") as login,
            patch.object(email_change, "_open_settings_account"),
            patch.object(email_change, "_submit_email_change_request") as request_change,
            patch.object(email_change, "snapshot_verification_code", return_value="111111") as snapshot,
            patch.object(email_change, "poll_new_email_otp", return_value="654321") as poll,
            patch.object(email_change, "_submit_email_verification_code") as submit_code,
            patch.object(email_change, "_wait_for_email_change_success") as wait_success,
            patch.object(email_change, "acknowledge_verification_code") as acknowledge,
            patch.object(email_change.time, "time", return_value=100.0),
        ):
            result = email_change.change_email_in_browser(object(), item)

        self.assertEqual(result, {"ok": True, "old_email": item.old_email, "new_email": item.new_email})
        login.assert_called_once()
        request_change.assert_called_once()
        snapshot.assert_called_once_with(item)
        poll.assert_called_once_with(item, after_ts=100.0, before_code="111111")
        submit_code.assert_called_once_with(unittest.mock.ANY, "654321")
        wait_success.assert_called_once()
        acknowledge.assert_called_once_with(item.gmail_account(), "654321")

    def test_submit_error_after_attempt_is_uncertain_and_never_retried(self):
        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        with (
            patch.object(email_change, "_login_chatgpt_with_credentials"),
            patch.object(email_change, "_open_settings_account"),
            patch.object(email_change, "_submit_email_change_request"),
            patch.object(email_change, "snapshot_verification_code", return_value="111111"),
            patch.object(email_change, "poll_new_email_otp", return_value="654321"),
            patch.object(
                email_change,
                "_submit_email_verification_code",
                side_effect=RuntimeError("transport ended after submit"),
            ) as submit_code,
            patch.object(email_change, "_wait_for_email_change_success") as wait_success,
            patch.object(email_change.time, "time", return_value=100.0),
        ):
            with self.assertRaisesRegex(
                email_change.EmailChangeConfirmationError,
                "may have been accepted remotely",
            ):
                email_change.change_email_in_browser(object(), item)

        submit_code.assert_called_once()
        wait_success.assert_not_called()


class BrowserEmailChangeRunnerTests(unittest.TestCase):
    def test_unsupported_route_releases_acquired_proxy_without_opening_browser(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com", password="secret", totp_secret="TOTPSECRET",
            new_email="new@example.com", code_url="https://mail.example/otp",
        )
        with (
            patch.object(browser_email_change, "resolve_rotating_proxy", return_value="http://proxy:1"),
            patch.object(browser_email_change, "configured_browser_profile_provider", return_value="skyvern"),
            patch.object(browser_email_change, "open_browser_profile") as open_profile,
            patch.object(browser_email_change, "release_rotating_proxy") as release,
        ):
            result = browser_email_change.run_email_change(item, proxy_lane_id=2)

        self.assertFalse(result["ok"])
        open_profile.assert_not_called()
        release.assert_called_once_with(
            scope=browser_email_change.EMAIL_CHANGE_PROXY_SCOPE,
            lane_id=2,
            proxy_url="http://proxy:1",
        )

    def test_runner_cleans_profile_and_updates_persistence_after_success(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        driver = Mock()
        profile = Mock(driver=driver, provider="roxy")
        with (
            patch("config.proxy.ROTATING_PROXY_ENABLED", False),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile),
            patch.object(
                browser_email_change,
                "change_email_in_browser",
                return_value={"ok": True, "old_email": item.old_email, "new_email": item.new_email},
            ),
            patch.object(browser_email_change.db, "update_account_email", return_value=True) as update_email,
            patch.object(
                browser_email_change.db,
                "get_account_by_email",
                return_value={"id": 77, "email": item.new_email},
            ),
            patch.object(browser_email_change.db, "get_gmail_api_url_email_by_email", return_value={"email": item.new_email}),
            patch.object(browser_email_change.db, "release_gmail_api_url_email") as mark_used,
        ):
            result = browser_email_change.run_email_change(item)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["account_id"], 77)
        update_email.assert_called_once_with(item.old_email, item.new_email)
        mark_used.assert_called_once_with(item.new_email, "used", "account email changed")
        profile.close.assert_called_once_with()
        profile.cleanup.assert_called_once_with()

    def test_follow_up_exception_never_retries_successful_email_change(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com", password="password-secret", totp_secret="totp-secret",
            new_email="new@example.com", code_url="https://mail.example/otp/secret",
        )
        profile = Mock(driver=Mock(), provider="roxy")
        with (
            patch.object(browser_email_change, "resolve_rotating_proxy", return_value=None),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile) as open_profile,
            patch.object(browser_email_change, "change_email_in_browser", return_value={"ok": True}) as change,
            patch.object(browser_email_change.db, "update_account_email", return_value=True),
            patch.object(browser_email_change.db, "get_account_by_email", return_value={"id": 7}),
            patch.object(browser_email_change.db, "get_gmail_api_url_email_by_email", return_value=None),
            patch("core.personal_info_codex.run_personal_info_codex_and_record", side_effect=RuntimeError("password-secret totp-secret")),
            patch("core.personal_info_codex.record_personal_info_codex") as record,
        ):
            result = browser_email_change.run_email_change(item, login_codex_after_change=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["codex"]["status"], "failed")
        self.assertNotIn("password-secret", str(result))
        self.assertNotIn("totp-secret", str(result))
        change.assert_called_once_with(profile.driver, item, keep_session=True)
        open_profile.assert_called_once()
        record.assert_called_once_with(item.new_email, result["codex"])
        profile.close.assert_called_once()

    def test_failed_email_persistence_and_skipped_status_error_do_not_retry_mutation(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com", password="password-secret", totp_secret="totp-secret",
            new_email="new@example.com", code_url="https://mail.example/otp/secret",
        )
        profile = Mock(driver=Mock(), provider="roxy")
        with (
            patch.object(browser_email_change, "resolve_rotating_proxy", return_value=None),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile),
            patch.object(browser_email_change, "change_email_in_browser", return_value={"ok": True}) as change,
            patch.object(browser_email_change.db, "update_account_email", return_value=False),
            patch.object(browser_email_change.db, "get_gmail_api_url_email_by_email", return_value=None),
            patch("core.personal_info_codex.skipped_personal_info_codex", side_effect=RuntimeError("password-secret")),
        ):
            result = browser_email_change.run_email_change(item, login_codex_after_change=True)

        self.assertTrue(result["ok"])
        self.assertFalse(result["persisted"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["codex"]["status"], "skipped")
        self.assertFalse(result["codex"]["status_persisted"])
        self.assertNotIn("password-secret", str(result))
        change.assert_called_once()

    def test_runner_does_not_persist_when_browser_reports_failure(self):
        from core import browser_email_change

        item = EmailChangeInput(
            old_email="old@example.com",
            password="secret",
            totp_secret="JBSWY3DPEHPK3PXP",
            new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        driver = Mock()
        profile = Mock(driver=driver, provider="roxy")
        with (
            patch("config.proxy.ROTATING_PROXY_ENABLED", False),
            patch.object(browser_email_change, "open_browser_profile", return_value=profile),
            patch.object(browser_email_change, "change_email_in_browser", return_value={"ok": False, "error": "verification failed"}),
            patch.object(browser_email_change.db, "update_account_email") as update_email,
        ):
            result = browser_email_change.run_email_change(item)

        self.assertFalse(result["ok"])
        self.assertFalse(result["persisted"])
        update_email.assert_not_called()

    def test_batch_serializes_accounts_that_share_one_gmail_api_url(self):
        from core import browser_email_change

        items = [
            EmailChangeInput(
                old_email=f"old{i}@example.com",
                password="secret",
                totp_secret="JBSWY3DPEHPK3PXP",
                new_email=f"new{i}@gmail.com",
                code_url="https://mail.example/otp/shared" if i < 3 else "https://mail.example/otp/other",
            )
            for i in range(4)
        ]
        active: dict[str, int] = {}
        peak: dict[str, int] = {}
        lock = threading.Lock()

        def run_one(item, proxy_lane_id=None):
            with lock:
                active[item.code_url] = active.get(item.code_url, 0) + 1
                peak[item.code_url] = max(peak.get(item.code_url, 0), active[item.code_url])
            time.sleep(0.02)
            with lock:
                active[item.code_url] -= 1
            return {"ok": True, "old_email": item.old_email, "new_email": item.new_email}

        with patch.object(browser_email_change, "run_email_change", side_effect=run_one):
            results = browser_email_change.run_email_change_batch(items, workers=4)

        self.assertEqual(peak["https://mail.example/otp/shared"], 1)
        self.assertEqual([result["old_email"] for result in results], [item.old_email for item in items])



class EmailChangeRecoveryTests(unittest.TestCase):
    def test_browser_runner_retries_with_fresh_profile_and_preserves_proxy_lane(self):
        from core import browser_email_change
        item = EmailChangeInput(
            old_email="old@example.com", password="secret",
            totp_secret="JBSWY3DPEHPK3PXP", new_email="new@example.com",
            code_url="https://mail.example/otp/1",
        )
        first = Mock(driver=Mock(), provider="roxy")
        second = Mock(driver=Mock(), provider="roxy")
        with (
            patch("config.proxy.ROTATING_PROXY_ENABLED", True),
            patch.object(browser_email_change, "resolve_rotating_proxy", return_value="http://proxy:1") as resolve,
            patch.object(browser_email_change, "open_browser_profile", side_effect=[first, second]),
            patch.object(browser_email_change, "change_email_in_browser", side_effect=[
                {"ok": False, "retryable": True, "error": "reauth_required"},
                {"ok": True},
            ]),
            patch.object(browser_email_change.db, "update_account_email", return_value=True),
            patch.object(browser_email_change.db, "get_account_by_email", return_value={"id": 7}),
            patch.object(browser_email_change.db, "get_gmail_api_url_email_by_email", return_value=None),
            patch.object(browser_email_change, "release_rotating_proxy") as release,
        ):
            result = browser_email_change.run_email_change(item, proxy_lane_id=2)
        self.assertTrue(result["ok"])
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(release.call_count, 2)
        self.assertEqual(first.close.call_count, 1)
        self.assertEqual(second.cleanup.call_count, 1)

    def test_email_change_api_does_not_create_a_separate_retry_progress_store(self):
        from webui import email_change_api

        self.assertFalse(hasattr(email_change_api, "_email_progress"))
        self.assertFalse(hasattr(email_change_api, "api_accounts_change_email_retry"))
