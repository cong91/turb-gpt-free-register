import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.account_security import TwofaChangeInput
from core.browser_twofa_change import run_twofa_change
from core.twofa_change_post_auth import run_twofa_post_auth


class TwofaPostAuthTests(unittest.TestCase):
    def setUp(self):
        from core.personal_info_codex import verify_live_identity

        self.verify_live_identity = verify_live_identity
        self.item = TwofaChangeInput("user@example.com", "password", "OLDSECRET")
        self.profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        for target in (
            "core.personal_info_codex.verify_live_identity",
            "core.personal_info_codex._codex_route_skip_reason",
        ):
            patcher = patch(target, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plan_and_codex_run_before_cleanup_with_new_secret(self):
        events = []
        result = {"ok": True, "persisted": True, "access_token_saved": True}
        outcome = {"status": "failed", "ok": False, "reason": "Codex failed safely"}
        progress = Mock(side_effect=lambda _index, update: events.append(update["codex"]["status"]) if update.get("codex") else None)

        def plan_check(**_kwargs):
            events.append("plan")
            return {"accepted": True, "ok": True, "current_plan_type": "free"}

        def codex(**kwargs):
            events.append("codex")
            self.assertEqual(kwargs["totp_secret"], "NEWSECRET")
            self.assertEqual(kwargs["password"], "password")
            self.assertEqual(kwargs["active_proxy"], "http://proxy.example:8080")
            return outcome

        with (
            patch("core.plan_check_service.run_sync_plan_check", side_effect=plan_check),
            patch("core.personal_info_codex.run_personal_info_codex_and_record", side_effect=codex),
        ):
            run_twofa_post_auth(
                account_id=7,
                item=self.item,
                access_token="saved-token",
                profile=self.profile,
                active_proxy="http://proxy.example:8080",
                new_totp_secret="NEWSECRET",
                persisted=True,
                login_codex_after_change=True,
                progress_callback=progress,
                progress_index=0,
                result=result,
            )

        self.assertEqual(events, ["plan", "running", "codex"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["codex"]["status"], "failed")

    def test_follow_up_progress_stays_running_until_codex_returns(self):
        updates = []
        with patch("core.personal_info_codex.run_personal_info_codex_and_record", return_value={"status": "success", "ok": True}):
            run_twofa_post_auth(
                account_id=7, item=self.item, access_token="", profile=self.profile,
                active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                login_codex_after_change=True,
                progress_callback=lambda _index, update: updates.append(update),
                progress_index=0, result={"ok": True, "persisted": True},
            )
        self.assertEqual(updates[0]["status"], "running")
        self.assertEqual(updates[0]["codex"]["status"], "running")

    def test_failed_change_is_skipped_and_never_runs_codex(self):
        result = {"ok": False, "persisted": False, "remote_disabled": True}
        with (
            patch("core.personal_info_codex.record_personal_info_codex") as record,
            patch("core.personal_info_codex.run_personal_info_codex_and_record") as run_codex,
        ):
            run_twofa_post_auth(
                account_id=7,
                item=self.item,
                access_token="",
                profile=self.profile,
                active_proxy=None,
                new_totp_secret="",
                persisted=False,
                login_codex_after_change=True,
                progress_callback=None,
                progress_index=None,
                result=result,
            )
        self.assertEqual(result["codex"]["status"], "skipped")
        self.assertTrue(result["remote_disabled"])
        self.assertFalse(result["ok"])
        run_codex.assert_not_called()
        record.assert_called_once()

    def test_codex_exception_does_not_change_persisted_mfa_success(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.personal_info_codex.run_personal_info_codex_and_record", side_effect=RuntimeError("secret details")),
            patch("core.personal_info_codex.record_personal_info_codex", return_value=None) as record,
        ):
            run_twofa_post_auth(
                account_id=7,
                item=self.item,
                access_token="",
                profile=self.profile,
                active_proxy=None,
                new_totp_secret="NEWSECRET",
                persisted=True,
                login_codex_after_change=True,
                progress_callback=None,
                progress_index=None,
                result=result,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["codex"]["status"], "failed")
        self.assertNotIn("secret details", result["codex"]["reason"])
        record.assert_called_once_with(self.item.email, result["codex"])

    def test_plan_failure_is_nested_and_codex_still_runs(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.plan_check_service.run_sync_plan_check", side_effect=RuntimeError("plan error")),
            patch("core.personal_info_codex.run_personal_info_codex_and_record", return_value={"status": "receipt", "ok": True}) as codex,
        ):
            run_twofa_post_auth(
                account_id=7,
                item=self.item,
                access_token="saved-token",
                profile=self.profile,
                active_proxy=None,
                new_totp_secret="NEWSECRET",
                persisted=True,
                login_codex_after_change=True,
                progress_callback=None,
                progress_index=None,
                result=result,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertFalse(result["plan_check"]["ok"])
        self.assertEqual(result["codex"]["status"], "receipt")
        codex.assert_called_once()

    def test_persistence_exception_does_not_expose_the_new_totp_secret(self):
        from webui.personal_info_results import _public_result

        with (
            patch("core.browser_twofa_change.db.get_account_by_email", return_value={"id": 7}),
            patch("core.browser_twofa_change.open_browser_profile", return_value=self.profile),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={
                "ok": True, "new_totp_secret": "NEWSECRET", "remote_disabled": True,
            }),
            patch("core.browser_twofa_change.db.update_account_2fa", side_effect=RuntimeError("cannot save NEWSECRET")),
            patch("core.browser_twofa_change.preferred_account_proxy") as proxy,
        ):
            proxy.return_value.__enter__.return_value = (None, "direct")
            result = run_twofa_change(self.item)

        self.assertFalse(result["persisted"])
        self.assertNotIn("NEWSECRET", json.dumps(_public_result(result)))

    def test_token_persistence_exception_redacts_tokens_and_new_totp(self):
        from webui.personal_info_results import _public_result

        with (
            patch("core.browser_twofa_change.db.get_account_by_email", return_value={"id": 7}),
            patch("core.browser_twofa_change.open_browser_profile", return_value=self.profile),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={
                "ok": True, "new_totp_secret": "NEWSECRET", "access_token": "TOKENVALUE", "remote_disabled": True,
            }),
            patch("core.browser_twofa_change.db.update_account_access_token", side_effect=RuntimeError("cannot save TOKENVALUE or NEWSECRET")),
            patch("core.browser_twofa_change.db.update_account_2fa", return_value=True),
            patch("core.browser_twofa_change.preferred_account_proxy") as proxy,
        ):
            proxy.return_value.__enter__.return_value = (None, "direct")
            result = run_twofa_change(self.item)

        public = json.dumps(_public_result(result))
        self.assertNotIn("TOKENVALUE", public)
        self.assertNotIn("NEWSECRET", public)

    def test_checkbox_off_never_authenticates_browser_or_runs_codex(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.personal_info_codex.verify_live_identity") as verify,
            patch("core.browser_twofa_login._login_existing_account") as login,
            patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
        ):
            run_twofa_post_auth(
                account_id=7, item=self.item, access_token="", profile=self.profile,
                active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                login_codex_after_change=False, progress_callback=None, progress_index=None,
                result=result,
            )
        verify.assert_not_called()
        login.assert_not_called()
        codex.assert_not_called()
        self.assertNotIn("codex", result)

    def test_unverifiable_route_skips_before_browser_login(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.personal_info_codex._codex_route_skip_reason", return_value="unsupported route"),
            patch("core.browser_twofa_login._login_existing_account") as login,
            patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
            patch("core.personal_info_codex.record_personal_info_codex") as record,
        ):
            run_twofa_post_auth(
                account_id=7, item=self.item, access_token="", profile=self.profile,
                active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                login_codex_after_change=True, progress_callback=None, progress_index=None,
                result=result,
            )
        self.assertEqual(result["codex"]["status"], "skipped")
        login.assert_not_called()
        codex.assert_not_called()
        record.assert_called_once()

    def test_verified_browser_session_is_reused_without_another_login(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.personal_info_codex.verify_live_identity") as verify,
            patch("core.browser_twofa_login._login_existing_account") as login,
            patch("core.personal_info_codex.run_personal_info_codex_and_record", return_value={"status": "success", "ok": True}) as codex,
        ):
            run_twofa_post_auth(
                account_id=7, item=self.item, access_token="", profile=self.profile,
                active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                login_codex_after_change=True, progress_callback=None, progress_index=None,
                result=result,
            )
        verify.assert_called_once_with(self.profile.driver, self.item.email)
        login.assert_not_called()
        self.assertIs(codex.call_args.kwargs["profile"], self.profile)
        self.assertEqual(result["codex"]["status"], "success")

    def test_real_identity_verification_fails_closed_for_session_states(self):
        sessions = (
            ("different account", {"accessToken": "token", "user": {"email": "other@example.com"}}),
            ("missing token", {"user": {"email": self.item.email}}),
            ("warning banner", {"error": "WARNING_BANNER"}),
            ("empty session", {}),
            ("request failed", None),
            ("timeout", TimeoutError("workspace transition")),
        )
        reasons = []
        for label, session in sessions:
            with self.subTest(session=label):
                driver = Mock()
                driver.context = None
                driver.current_window_handle = "original"
                if isinstance(session, Exception):
                    driver.execute_async_script.side_effect = session
                else:
                    driver.execute_async_script.return_value = session
                profile = SimpleNamespace(provider="roxy", driver=driver)
                result = {"ok": True, "persisted": True}
                with (
                    patch("core.personal_info_codex.verify_live_identity", side_effect=self.verify_live_identity),
                    patch("core.browser_twofa_login._login_existing_account") as login,
                    patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
                    patch("core.personal_info_codex.record_personal_info_codex") as record,
                ):
                    run_twofa_post_auth(
                        account_id=7, item=self.item, access_token="", profile=profile,
                        active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                        login_codex_after_change=True, progress_callback=None, progress_index=None,
                        result=result,
                    )
                self.assertTrue(result["ok"])
                self.assertTrue(result["persisted"])
                self.assertEqual(result["codex"]["status"], "failed")
                self.assertIn("xác minh", result["codex"]["reason"].lower())
                reasons.append(result["codex"]["reason"])
                login.assert_not_called()
                codex.assert_not_called()
                record.assert_called_once_with(self.item.email, result["codex"])
                driver.get.assert_called_once_with("https://chatgpt.com/")
                driver.delete_all_cookies.assert_not_called()
                driver.execute_script.assert_not_called()
                driver.close.assert_called_once_with()
                driver.switch_to.window.assert_called_once_with("original")
        self.assertEqual(len(set(reasons)), 1)

    def test_real_identity_verification_reuses_matching_session(self):
        driver = Mock()
        driver.context = None
        driver.current_window_handle = "original"
        driver.execute_async_script.return_value = {
            "accessToken": "token", "user": {"email": "USER@example.com"},
        }
        profile = SimpleNamespace(provider="roxy", driver=driver)
        result = {"ok": True, "persisted": True}
        with (
            patch("core.personal_info_codex.verify_live_identity", side_effect=self.verify_live_identity),
            patch("core.browser_twofa_login._login_existing_account") as login,
            patch("core.personal_info_codex.run_personal_info_codex_and_record", return_value={"status": "success", "ok": True}) as codex,
        ):
            run_twofa_post_auth(
                account_id=7, item=self.item, access_token="", profile=profile,
                active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                login_codex_after_change=True, progress_callback=None, progress_index=None,
                result=result,
            )
        self.assertEqual(result["codex"]["status"], "success")
        self.assertIs(codex.call_args.kwargs["profile"], profile)
        self.assertEqual(codex.call_args.kwargs["totp_secret"], "NEWSECRET")
        login.assert_not_called()
        driver.delete_all_cookies.assert_not_called()
        driver.execute_script.assert_not_called()
        driver.close.assert_called_once_with()
        driver.switch_to.window.assert_called_once_with("original")

    def test_cloud_identity_verification_fails_closed_on_mismatch_or_error(self):
        for label in ("mismatch", "http error", "timeout"):
            with self.subTest(session=label):
                driver = Mock()
                response = driver.context.request.get.return_value
                response.status = 503 if label == "http error" else 200
                response.json.return_value = {
                    "accessToken": "token", "user": {"email": "other@example.com"},
                }
                if label == "timeout":
                    driver.context.request.get.side_effect = TimeoutError("workspace transition")
                profile = SimpleNamespace(provider="browser_use", driver=driver)
                result = {"ok": True, "persisted": True}
                with (
                    patch("core.personal_info_codex.verify_live_identity", side_effect=self.verify_live_identity),
                    patch("core.browser_twofa_login._login_existing_account") as login,
                    patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
                    patch("core.personal_info_codex.record_personal_info_codex") as record,
                ):
                    run_twofa_post_auth(
                        account_id=7, item=self.item, access_token="", profile=profile,
                        active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                        login_codex_after_change=True, progress_callback=None, progress_index=None,
                        result=result,
                    )
                self.assertTrue(result["ok"])
                self.assertTrue(result["persisted"])
                self.assertEqual(result["codex"]["status"], "failed")
                login.assert_not_called()
                codex.assert_not_called()
                record.assert_called_once_with(self.item.email, result["codex"])
                driver.context.request.get.assert_called_once_with(
                    "https://chatgpt.com/api/auth/session", timeout=10000,
                )
                driver.context.clear_cookies.assert_not_called()
                driver.get.assert_not_called()
                driver.execute_script.assert_not_called()

    def test_stored_token_change_stops_without_verified_session_and_cleans_up(self):
        events = []
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        profile.close = Mock(side_effect=lambda: events.append("close"))
        profile.cleanup = Mock(side_effect=lambda: events.append("cleanup"))

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy.example:8080", "rotating_proxy"
            events.append("proxy_release")

        def verify(driver, email):
            self.assertIs(driver, profile.driver)
            self.assertEqual(email, self.item.email)
            events.append("verify")
            raise RuntimeError("no verified cookie session")

        with (
            patch("core.browser_twofa_change.db.get_account_by_email", return_value={"id": 7, "access_token": "stored-token"}),
            patch("core.browser_twofa_change.preferred_account_proxy", side_effect=proxy_context),
            patch("core.browser_twofa_change.open_browser_profile", return_value=profile),
            patch("core.account_security.deactivate_2fa_in_page", side_effect=lambda *_a, **_k: events.append("disable")),
            patch("core.account_security.setup_2fa_in_page", side_effect=lambda *_a, **_k: events.append("enroll") or "NEWSECRET"),
            patch("core.browser_twofa_change.db.update_account_access_token", return_value=True),
            patch("core.browser_twofa_change.db.update_account_2fa", side_effect=lambda *_a, **_k: events.append("persist") or True),
            patch("core.plan_check_service.run_sync_plan_check", side_effect=lambda **_k: events.append("plan") or {"accepted": True, "ok": True}),
            patch("core.personal_info_codex.verify_live_identity", side_effect=verify),
            patch("core.browser_twofa_login._login_existing_account") as browser_login,
            patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
            patch("core.personal_info_codex.record_personal_info_codex", side_effect=lambda *_a: events.append("codex_failed")) as record,
        ):
            result = run_twofa_change(
                self.item, login_codex_after_change=True, progress_index=0,
                progress_callback=lambda _i, update: events.append("codex_running") if update.get("codex") else None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["codex"]["status"], "failed")
        self.assertTrue(result["access_token_saved"])
        browser_login.assert_not_called()
        codex.assert_not_called()
        record.assert_called_once_with(self.item.email, result["codex"])
        profile.driver.get.assert_not_called()
        profile.driver.delete_all_cookies.assert_not_called()
        profile.driver.execute_script.assert_not_called()
        self.assertEqual(events, ["disable", "enroll", "persist", "plan", "codex_running", "verify", "codex_failed", "close", "cleanup", "proxy_release"])

    def test_identity_verification_errors_block_login_and_codex(self):
        for error in (
            RuntimeError("identity mismatch secret"),
            TimeoutError("workspace transition timed out NEWSECRET"),
            ValueError("malformed session password"),
        ):
            with self.subTest(error=type(error).__name__):
                result = {"ok": True, "persisted": True}
                with (
                    patch("core.personal_info_codex.verify_live_identity", side_effect=error) as verify,
                    patch("core.browser_twofa_login._login_existing_account") as login,
                    patch("core.personal_info_codex.run_personal_info_codex_and_record") as codex,
                    patch("core.personal_info_codex.record_personal_info_codex") as record,
                ):
                    run_twofa_post_auth(
                        account_id=7, item=self.item, access_token="", profile=self.profile,
                        active_proxy=None, new_totp_secret="NEWSECRET", persisted=True,
                        login_codex_after_change=True, progress_callback=None, progress_index=None,
                        result=result,
                    )
                self.assertTrue(result["ok"])
                self.assertTrue(result["persisted"])
                self.assertEqual(result["codex"]["status"], "failed")
                self.assertIn("xác minh", result["codex"]["reason"].lower())
                self.assertNotIn("secret", result["codex"]["reason"].lower())
                self.assertNotIn("password", result["codex"]["reason"].lower())
                verify.assert_called_once_with(self.profile.driver, self.item.email)
                login.assert_not_called()
                codex.assert_not_called()
                record.assert_called_once_with(self.item.email, result["codex"])
                self.profile.driver.get.assert_not_called()
                self.profile.driver.delete_all_cookies.assert_not_called()
                self.profile.driver.execute_script.assert_not_called()

    def test_worker_contains_unexpected_post_auth_failure_before_cleanup(self):
        events = []
        profile = SimpleNamespace(provider="roxy", driver=Mock())
        profile.close = Mock(side_effect=lambda: events.append("close"))
        profile.cleanup = Mock(side_effect=lambda: events.append("cleanup"))
        with (
            patch("core.browser_twofa_change.db.get_account_by_email", return_value={"id": 7}),
            patch("core.browser_twofa_change.open_browser_profile", return_value=profile),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={"ok": True, "new_totp_secret": "NEWSECRET"}) as change,
            patch("core.browser_twofa_change.db.update_account_2fa", return_value=True),
            patch("core.browser_twofa_change.preferred_account_proxy") as proxy,
            patch("core.twofa_change_post_auth.run_twofa_post_auth", side_effect=RuntimeError("NEWSECRET")),
            patch("core.personal_info_codex.record_personal_info_codex", side_effect=RuntimeError("password")),
        ):
            proxy.return_value.__enter__.return_value = (None, "direct")
            result = run_twofa_change(self.item, login_codex_after_change=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["codex"]["status"], "failed")
        self.assertFalse(result["codex"]["status_persisted"])
        self.assertNotIn("NEWSECRET", result["codex"]["reason"])
        change.assert_called_once()
        self.assertEqual(events, ["close", "cleanup"])

    def test_browser_worker_persists_change_then_codex_then_cleans_up(self):
        events = []
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        profile.close = Mock(side_effect=lambda: events.append("close"))
        profile.cleanup = Mock(side_effect=lambda: events.append("cleanup"))

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy.example:8080", "rotating_proxy"
            events.append("proxy_release")

        def update_twofa(*_args, **_kwargs):
            events.append("persist")
            return True

        def run_codex(**kwargs):
            events.append("codex")
            self.assertEqual(kwargs["totp_secret"], "NEWSECRET")
            return {"status": "success", "ok": True, "credential_saved": True}

        with (
            patch("core.browser_twofa_change.db.get_account_by_email", return_value={"id": 7, "access_token": "old"}),
            patch("core.browser_twofa_change.preferred_account_proxy", side_effect=proxy_context),
            patch("core.browser_twofa_change.open_browser_profile", return_value=profile),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={
                "ok": True, "email": self.item.email, "new_totp_secret": "NEWSECRET", "access_token": "newtoken",
            }),
            patch("core.browser_twofa_change.db.update_account_access_token", return_value=True),
            patch("core.browser_twofa_change.db.update_account_2fa", side_effect=update_twofa),
            patch("core.plan_check_service.run_sync_plan_check", side_effect=lambda **_kwargs: {"accepted": False, "ok": False}),
            patch("core.personal_info_codex.run_personal_info_codex_and_record", side_effect=run_codex),
        ):
            result = run_twofa_change(self.item, login_codex_after_change=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(events, ["persist", "codex", "close", "cleanup", "proxy_release"])



if __name__ == "__main__":
    unittest.main()
