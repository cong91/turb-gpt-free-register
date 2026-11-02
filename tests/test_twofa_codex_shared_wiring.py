"""Offline regression for shared Codex OAuth after a 2FA change."""

import os
import socket
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TwofaCodexSharedWiringTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp_dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        env_file = self.temp_dir / ".env"
        env_file.touch()
        self.stack.enter_context(
            patch.dict(os.environ, {"TURB_ENV_FILE": str(env_file)}, clear=True)
        )
        thread_start = self.stack.enter_context(
            patch("threading.Thread.start", side_effect=AssertionError("unexpected background thread"))
        )
        self.addCleanup(thread_start.assert_not_called)

        from core import app_state_db

        self.stack.enter_context(patch.object(app_state_db, "APP_STATE_DB_PATH", self.temp_dir / "state.sqlite3"))
        self.stack.enter_context(patch.object(app_state_db, "get_named_document", return_value={}))
        app_db_connect = self.stack.enter_context(
            patch.object(app_state_db, "connect", side_effect=AssertionError("unexpected app database access"))
        )
        sqlite_connect = self.stack.enter_context(
            patch.object(sqlite3, "connect", side_effect=AssertionError("unexpected SQLite access"))
        )
        network_connect = self.stack.enter_context(
            patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network access"))
        )
        network_connect_ex = self.stack.enter_context(
            patch.object(socket.socket, "connect_ex", side_effect=AssertionError("unexpected network access"))
        )
        dns_lookup = self.stack.enter_context(
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS lookup"))
        )
        for guard in (app_db_connect, sqlite_connect, network_connect, network_connect_ex, dns_lookup):
            self.addCleanup(guard.assert_not_called)

        import config
        from core import browser_twofa_change, codex_oauth, codex_retry_service, db
        from core.account_security import TwofaChangeInput

        self.change = browser_twofa_change
        self.retry = codex_retry_service
        self.codex_oauth = codex_oauth
        self.db = db
        self.config = config
        self.events = []
        self.email = "wiring-test@example.invalid"
        self.password = "fake-password-for-test"
        self.old_totp = "AAAAAAAAAAAAAAAA"
        self.new_totp = "BBBBBBBBBBBBBBBB"
        self.proxy = "http://proxy.invalid:8080"
        self.item = TwofaChangeInput(self.email, self.password, self.old_totp)
        self.account = {
            "id": 7,
            "email": self.email,
            "registration_password": self.password,
            "totp_secret": self.old_totp,
            "access_token": "fake-access-token",
        }
        self.driver = Mock()
        self.driver.get_cookie.return_value = None
        self.opened = object()
        self.profile = SimpleNamespace(
            provider="roxy",
            driver=self.driver,
            existing_opened=self.opened,
            close=Mock(side_effect=lambda: self.events.append("close")),
            cleanup=Mock(side_effect=lambda: self.events.append("cleanup")),
        )

    @contextmanager
    def _proxy_context(self, *_args, **_kwargs):
        try:
            yield self.proxy, "rotating_proxy"
        finally:
            self.events.append("proxy_release")

    def _persist_twofa(self, account_id, **kwargs):
        self.events.append("persist_twofa")
        self.account.update(
            totp_secret=kwargs["totp_secret"],
            registration_password=kwargs["registration_password"],
            twofa_status=kwargs["status"],
        )
        return account_id == self.account["id"]

    def _record_codex(self, email, status, reason):
        self.events.append("record_codex")
        self.account.update(codex_status=status, codex_error=reason)
        return email == self.email

    def _run_change(self, oauth_result, *, persist=True):
        def oauth(*args, **kwargs):
            self.events.append(("oauth", self.account["totp_secret"]))
            return oauth_result

        self.stack.enter_context(
            patch.object(self.db, "get_account_by_email", side_effect=lambda _email: dict(self.account))
        )
        self.stack.enter_context(patch.object(self.db, "update_account_access_token", return_value=True))
        self.persist_twofa = self.stack.enter_context(
            patch.object(self.db, "update_account_2fa", side_effect=self._persist_twofa if persist else None,
                         return_value=False)
        )
        self.record_codex = self.stack.enter_context(
            patch.object(self.db, "update_account_codex_status", side_effect=self._record_codex)
        )
        self.stack.enter_context(patch.object(self.change, "configured_browser_profile_provider", return_value="roxy"))
        self.open_profile = self.stack.enter_context(
            patch.object(self.change, "open_browser_profile", return_value=self.profile)
        )
        self.stack.enter_context(patch.object(self.change, "preferred_account_proxy", side_effect=self._proxy_context))
        self.disable = self.stack.enter_context(
            patch("core.account_security.deactivate_2fa_in_page", side_effect=lambda *_a, **_k: self.events.append("disable"))
        )
        self.enroll = self.stack.enter_context(
            patch("core.account_security.setup_2fa_in_page", side_effect=lambda *_a, **_k: self.events.append("enroll") or self.new_totp)
        )
        self.stack.enter_context(
            patch("core.plan_check_service.run_sync_plan_check", return_value={"accepted": True, "ok": True})
        )
        self.verify = self.stack.enter_context(
            patch("core.personal_info_codex.verify_live_identity", side_effect=lambda *_a: self.events.append("verify"))
        )
        self.read_credential = self.stack.enter_context(
            patch.object(self.db, "read_codex_credential", return_value=('{"access_token": "fake-codex-token"}', {}))
        )
        self.oauth = self.stack.enter_context(patch.object(self.codex_oauth, "run_codex_oauth", side_effect=oauth))
        return self.change.run_twofa_change(self.item, login_codex_after_change=True)

    def _assert_change_dispatch(self, result):
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(self.account["totp_secret"], self.new_totp)
        self.persist_twofa.assert_called_once_with(
            7, status="active", totp_secret=self.new_totp, error=None,
            registration_password=self.password,
        )
        self.disable.assert_called_once_with(self.driver, access_token="fake-access-token")
        self.enroll.assert_called_once_with(self.driver, self.email, access_token="fake-access-token")
        self.open_profile.assert_called_once_with(proxy=self.proxy)
        self.verify.assert_called_with(self.driver, self.email)
        self.assertLess(self.events.index("verify"), self.events.index(("oauth", self.new_totp)))
        self.oauth.assert_called_once()
        self.assertEqual(self.oauth.call_args.args, ())
        kwargs = self.oauth.call_args.kwargs
        self.assertEqual(kwargs["email"], self.email)
        self.assertTrue(kwargs["force"])
        self.assertEqual(kwargs["oauth_driver"], "roxy")
        self.assertEqual(kwargs["proxy"], self.proxy)
        self.assertIs(kwargs["existing_driver"], self.driver)
        self.assertIs(kwargs["existing_opened"], self.opened)
        self.assertIs(kwargs["clear_existing_state"], False)
        self.assertIs(kwargs.get("fresh_browser_profile", False), False)
        credentials = kwargs["credentials"]
        self.assertEqual((credentials.email, credentials.password, credentials.totp_secret),
                         (self.email, self.password, self.new_totp))
        self.assertLess(self.events.index("persist_twofa"), self.events.index(("oauth", self.new_totp)))
        self.assertLess(self.events.index(("oauth", self.new_totp)), self.events.index("close"))
        self.assertLess(self.events.index("close"), self.events.index("cleanup"))
        self.assertLess(self.events.index("cleanup"), self.events.index("proxy_release"))
        self.profile.close.assert_called_once_with()
        self.profile.cleanup.assert_called_once_with()
        self.driver.get.assert_not_called()

    def test_change_and_standalone_retry_share_oauth_with_distinct_profile_lifetimes(self):
        filename = "fake-codex-wiring-test.json"
        result = self._run_change({
            "status": "success", "ok": True,
            "file_path": f"sqlite://codex_accounts/{filename}",
        })
        self._assert_change_dispatch(result)
        self.assertEqual(result["codex"]["status"], "success")
        self.assertTrue(result["codex"]["ok"])
        self.assertTrue(result["codex"]["credential_saved"])
        self.read_credential.assert_called_once_with(filename)
        self.record_codex.assert_called_once_with(self.email, "success", None)
        change_credentials = self.oauth.call_args.kwargs["credentials"]

        with (
            patch.object(self.config, "reload_all"),
            patch.object(self.retry, "preferred_account_proxy", return_value=nullcontext((self.proxy, "rotating_proxy"))),
        ):
            retry_result = self.retry.run_worker(
                self.email, oauth_driver="roxy", target_log_path=self.temp_dir / "retry.log"
            )

        self.assertTrue(retry_result["ok"])
        self.assertEqual(self.oauth.call_count, 2)
        retry_call = self.oauth.call_args_list[1]
        self.assertEqual(retry_call.args, (self.email,))
        self.assertEqual(retry_call.kwargs["credentials"], change_credentials)
        self.assertEqual(retry_call.kwargs["proxy"], self.proxy)
        self.assertEqual(retry_call.kwargs["oauth_driver"], "roxy")
        self.assertTrue(retry_call.kwargs["force"])
        self.assertTrue(retry_call.kwargs["fresh_browser_profile"])
        self.assertNotIn("existing_driver", retry_call.kwargs)
        self.assertNotIn("existing_opened", retry_call.kwargs)

    def test_oauth_failure_does_not_retry_or_reverse_successful_2fa_change(self):
        result = self._run_change({
            "status": "failed", "ok": False, "message": "net::ERR_CONNECTION_RESET",
        })
        self._assert_change_dispatch(result)
        self.assertFalse(result["codex"]["ok"])
        self.assertEqual(result["codex"]["status"], "failed")
        self.assertEqual(self.account["twofa_status"], "active")
        self.assertEqual(self.account["codex_status"], "failed")
        self.record_codex.assert_called_once_with(self.email, "failed", result["codex"]["reason"])
        self.read_credential.assert_not_called()

    def test_unpersisted_2fa_never_enters_shared_oauth(self):
        result = self._run_change({"status": "success", "ok": True}, persist=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["persisted"])
        self.assertFalse(result["retryable"])
        self.assertEqual(self.account["totp_secret"], self.old_totp)
        self.assertEqual(result["codex"]["status"], "skipped")
        self.assertFalse(result["codex"]["ok"])
        self.persist_twofa.assert_called_once_with(
            7, status="active", totp_secret=self.new_totp, error=None,
            registration_password=self.password,
        )
        self.disable.assert_called_once()
        self.enroll.assert_called_once()
        self.verify.assert_not_called()
        self.oauth.assert_not_called()
        self.record_codex.assert_not_called()
        self.read_credential.assert_not_called()
        self.assertEqual(self.events, ["disable", "enroll", "close", "cleanup", "proxy_release"])


if __name__ == "__main__":
    unittest.main()
