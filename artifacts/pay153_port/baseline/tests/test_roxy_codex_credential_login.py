import importlib.util
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, call, patch

from core import browser_credential_login as credential_login
from core import roxy_codex_oauth
from core.codex_login_credentials import CodexLoginCredentials


class BrowserCredentialLoginTests(unittest.TestCase):
    def setUp(self):
        self.credentials = CodexLoginCredentials(
            email="user@example.com",
            password="openai-password",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    def test_driver_adapter_module_exists(self):
        spec = importlib.util.find_spec("core.browser_credential_login")
        self.assertIsNotNone(spec)

    def test_classifies_password_totp_email_otp_and_authenticated_pages(self):
        classify = getattr(credential_login, "classify_login_state", None)
        self.assertTrue(callable(classify))

        cases = [
            (
                {"url": "https://auth.openai.com/log-in/password", "body": "", "inputs": [{"type": "password"}]},
                "password",
            ),
            (
                {
                    "url": "https://auth.openai.com/mfa/totp",
                    "body": "Enter the code from your authenticator app",
                    "inputs": [{"autocomplete": "one-time-code", "name": "code"}],
                },
                "totp",
            ),
            (
                {
                    "url": "https://auth.openai.com/email-verification",
                    "body": "We sent a code to your email",
                    "inputs": [{"autocomplete": "one-time-code"}],
                },
                "email_otp",
            ),
            (
                {"url": "https://auth.openai.com/log-in/password", "body": "Incorrect password", "inputs": []},
                "password_invalid",
            ),
            (
                {
                    "url": "https://auth.openai.com/log-in/password",
                    "body": (
                        "<div class=\"_titleBlock\"><h1>Authentication Error</h1>"
                        "<div>You do not have an account because it has been deleted or deactivated.</div>"
                        "<span>error_code: account_deactivated</span></div>"
                    ),
                    "inputs": [],
                },
                "deactivated:account_deactivated",
            ),
            (
                {"url": "https://auth.openai.com/add-phone", "body": "Add a phone number", "inputs": []},
                "accepted",
            ),
            (
                {
                    "url": "https://auth.openai.com/log-in",
                    "body": "Choose an account Continue as user@example.com",
                    "inputs": [],
                },
                "account_chooser",
            ),
            (
                {
                    "url": "https://auth.openai.com/log-in",
                    "body": "Pick an account Use another account",
                    "inputs": [],
                },
                "account_chooser",
            ),
        ]

        for state, expected in cases:
            with self.subTest(expected=expected):
                driver = type(
                    "Driver",
                    (),
                    {
                        "current_url": state["url"],
                        "execute_script": lambda self, _script, current=state: current,
                    },
                )()
                self.assertEqual(classify(driver), expected)

    def test_open_and_submit_email_does_not_capture_screenshot_in_normal_flow(self):
        driver = Mock()
        with patch.object(credential_login, "human_delay"), patch.object(
            credential_login, "_maybe_accept"
        ), patch.object(credential_login, "classify_login_state", return_value="unknown"), patch.object(
            credential_login, "_type_email_address"
        ), patch.object(credential_login, "_submit_email_step"):
            state = credential_login._open_and_submit_email(
                driver, self.credentials.email, "https://auth.openai.com/oauth/authorize"
            )

        self.assertEqual(state, "email_submitted")
        driver.save_screenshot.assert_not_called()
        driver.page.screenshot.assert_not_called()

    def test_wait_for_login_state_ignores_stale_state_after_submit(self):
        wait = getattr(credential_login, "_wait_for_login_state", None)
        self.assertTrue(callable(wait))
        driver = object()
        with patch.object(
            credential_login,
            "classify_login_state",
            side_effect=["password", "password", "totp"],
        ) as classify:
            state = wait(driver, timeout=1, ignored_states={"password"})

        self.assertEqual(state, "totp")
        self.assertEqual(classify.call_count, 3)

    def test_wait_for_login_state_ignores_stale_totp_until_accepted(self):
        wait = getattr(credential_login, "_wait_for_login_state", None)
        self.assertTrue(callable(wait))
        driver = object()
        with patch.object(
            credential_login,
            "classify_login_state",
            side_effect=["totp", "totp", "accepted"],
        ) as classify:
            state = wait(driver, timeout=1, ignored_states={"totp"})

        self.assertEqual(state, "accepted")
        self.assertEqual(classify.call_count, 3)

    def test_password_and_totp_reach_authenticated_oauth_state(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        driver = object()
        with patch.object(credential_login, "_open_and_submit_email", create=True) as open_email, patch.object(
            credential_login,
            "_wait_for_login_state",
            side_effect=["password", "totp", "accepted"],
            create=True,
        ) as wait_state, patch.object(credential_login, "_submit_password", create=True) as submit_password, patch.object(
            credential_login,
            "generate_totp_code",
            return_value="123456",
            create=True,
        ), patch.object(credential_login, "_submit_totp_code", create=True) as submit_totp:
            login(driver, self.credentials, "https://auth.openai.com/oauth/authorize")

        open_email.assert_called_once_with(driver, self.credentials.email, "https://auth.openai.com/oauth/authorize")
        submit_password.assert_called_once_with(driver, self.credentials.password)
        submit_totp.assert_called_once_with(driver, "123456")
        self.assertEqual(wait_state.call_args_list[1].kwargs, {"ignored_states": {"password"}})
        self.assertEqual(wait_state.call_args_list[2].kwargs, {"ignored_states": {"totp"}})

    def test_invalid_totp_retries_once_with_fresh_code(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        driver = object()
        with patch.object(credential_login, "_open_and_submit_email", create=True), patch.object(
            credential_login,
            "_wait_for_login_state",
            side_effect=["password", "totp", "totp_invalid", "accepted"],
            create=True,
        ), patch.object(credential_login, "_submit_password", create=True), patch.object(
            credential_login,
            "generate_totp_code",
            side_effect=["111111", "222222"],
            create=True,
        ) as generate, patch.object(credential_login, "_submit_totp_code", create=True) as submit_totp:
            login(driver, self.credentials, "https://auth.openai.com/oauth/authorize")

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            generate.call_args_list,
            [
                call(self.credentials.totp_secret, previous_code=None),
                call(self.credentials.totp_secret, previous_code="111111"),
            ],
        )
        self.assertEqual(submit_totp.call_args_list, [call(driver, "111111"), call(driver, "222222")])

    def test_email_otp_challenge_fails_without_mailbox_fallback(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        with (
            patch.object(credential_login, "_open_and_submit_email", create=True),
            patch.object(
                credential_login,
                "_wait_for_login_state",
                side_effect=["password", "email_otp"],
                create=True,
            ),
            patch.object(credential_login, "_submit_password", create=True),
            self.assertRaisesRegex(RuntimeError, "email OTP"),
        ):
            login(object(), self.credentials, "https://auth.openai.com/oauth/authorize")

    def test_already_authenticated_state_skips_password_and_totp(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        with patch.object(credential_login, "_open_and_submit_email", create=True), patch.object(
            credential_login,
            "_wait_for_login_state",
            return_value="accepted",
            create=True,
        ), patch.object(credential_login, "_submit_password", create=True) as submit_password, patch.object(
            credential_login,
            "_submit_totp_code",
            create=True,
        ) as submit_totp:
            login(object(), self.credentials, "https://auth.openai.com/oauth/authorize")

        submit_password.assert_not_called()
        submit_totp.assert_not_called()

    def test_account_chooser_selects_matching_account_before_waiting_for_phone(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        driver = object()
        with patch.object(
            credential_login,
            "_open_and_submit_email",
            return_value="account_chooser",
            create=True,
        ), patch.object(
            credential_login,
            "_select_matching_account",
            return_value=True,
            create=True,
        ) as select_account, patch.object(
            credential_login,
            "_wait_for_login_state",
            return_value="accepted",
            create=True,
        ), patch.object(credential_login, "_submit_password", create=True) as submit_password:
            login(driver, self.credentials, "https://auth.openai.com/oauth/authorize")

        select_account.assert_called_once_with(driver, self.credentials.email)
        submit_password.assert_not_called()

    def test_account_chooser_without_matching_account_fails_closed(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        with (
            patch.object(credential_login, "_open_and_submit_email", return_value="account_chooser", create=True),
            patch.object(credential_login, "_select_matching_account", return_value=False, create=True),
            self.assertRaisesRegex(RuntimeError, "account chooser"),
        ):
            login(object(), self.credentials, "https://auth.openai.com/oauth/authorize")

    def test_account_chooser_after_email_submit_continues_to_totp(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        driver = object()
        with patch.object(
            credential_login,
            "_open_and_submit_email",
            return_value="email_submitted",
            create=True,
        ), patch.object(
            credential_login,
            "_wait_for_login_state",
            side_effect=["account_chooser", "password", "totp", "accepted"],
            create=True,
        ) as wait_state, patch.object(
            credential_login,
            "_select_matching_account",
            return_value=True,
            create=True,
        ) as select_account, patch.object(credential_login, "_submit_password", create=True) as submit_password, patch.object(
            credential_login,
            "generate_totp_code",
            return_value="123456",
            create=True,
        ), patch.object(credential_login, "_submit_totp_code", create=True) as submit_totp:
            login(driver, self.credentials, "https://auth.openai.com/oauth/authorize")

        select_account.assert_called_once_with(driver, self.credentials.email)
        submit_password.assert_called_once_with(driver, self.credentials.password)
        submit_totp.assert_called_once_with(driver, "123456")
        self.assertEqual(wait_state.call_count, 4)

    def test_account_chooser_after_password_submit_continues_to_totp(self):
        login = getattr(credential_login, "login_with_credentials", None)
        self.assertTrue(callable(login))
        driver = object()
        with patch.object(
            credential_login,
            "_open_and_submit_email",
            return_value="email_submitted",
            create=True,
        ), patch.object(
            credential_login,
            "_wait_for_login_state",
            side_effect=["password", "account_chooser", "totp", "accepted"],
            create=True,
        ) as wait_state, patch.object(
            credential_login,
            "_select_matching_account",
            return_value=True,
            create=True,
        ) as select_account, patch.object(credential_login, "_submit_password", create=True), patch.object(
            credential_login,
            "generate_totp_code",
            return_value="123456",
            create=True,
        ), patch.object(credential_login, "_submit_totp_code", create=True) as submit_totp:
            login(driver, self.credentials, "https://auth.openai.com/oauth/authorize")

        select_account.assert_called_once_with(driver, self.credentials.email)
        submit_totp.assert_called_once_with(driver, "123456")
        self.assertEqual(wait_state.call_count, 4)

    @patch("core.browser_credential_login.login_with_credentials")
    @patch("core.roxy_codex_oauth._fill_email_and_otp")
    def test_roxy_oauth_uses_credential_adapter_before_existing_sms_callback_pipeline(
        self,
        fill_email_otp,
        login_credentials,
    ):
        driver = Mock()
        opened = Mock(profile_id="profile-1")
        callback_url = "http://localhost:1455/auth/callback?code=oauth-code&state=state-1"
        with patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"), patch(
            "core.codex_oauth._request_cpa_authorize_url",
            return_value={"state": "state-1", "auth_url": "https://auth.openai.com/oauth/authorize"},
        ), patch("core.roxy_codex_oauth._do_phone_verification_if_present") as phone, patch(
            "core.roxy_codex_oauth._finish_consent_workspace",
            return_value=callback_url,
        ), patch("core.codex_oauth._extract_code", return_value="oauth-code"), patch(
            "core.codex_oauth._submit_cpa_callback",
            return_value={"message": "ok"},
        ), patch("core.codex_oauth._save_cpa_local_record", return_value=None):
            result = roxy_codex_oauth._run_roxy_codex_oauth_once(
                email="user@example.com",
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=False,
                credentials=self.credentials,
            )

        self.assertTrue(result["ok"])
        login_credentials.assert_called_once_with(
            driver,
            self.credentials,
            "https://auth.openai.com/oauth/authorize",
        )
        fill_email_otp.assert_not_called()
        phone.assert_called_once_with(driver)

    def test_roxy_oauth_allows_email_otp_fallback_when_credentials_are_incomplete(self):
        """Missing password or TOTP may use the existing mailbox OTP flow."""
        driver = Mock()
        opened = Mock(profile_id="profile-1", raw={})
        otp_provider = Mock()
        callback_url = "http://localhost:1455/auth/callback?code=oauth-code&state=state-1"

        with ExitStack() as stack:
            fill_email_otp = stack.enter_context(patch("core.roxy_codex_oauth._fill_email_and_otp"))
            stack.enter_context(patch("core.codex_oauth._codex_auth_url_source", return_value="sub2"))
            stack.enter_context(patch(
                "core.codex_oauth._request_sub2_authorize_url",
                return_value={
                    "state": "state-1",
                    "auth_url": "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback",
                    "session_id": "session-1",
                },
            ))
            stack.enter_context(patch("core.roxy_codex_oauth._do_phone_verification_if_present"))
            stack.enter_context(patch("core.roxy_codex_oauth._finish_consent_workspace", return_value=callback_url))
            stack.enter_context(patch("core.codex_oauth._extract_code", return_value="oauth-code"))
            stack.enter_context(patch("core.codex_oauth._submit_sub2_callback", return_value={"message": "ok"}))
            stack.enter_context(patch("core.codex_oauth._save_sub2_local_record", return_value=None))
            stack.enter_context(patch("core.roxy_codex_oauth.human_delay"))

            result = roxy_codex_oauth._run_roxy_codex_oauth_once(
                email="user@example.com",
                otp_provider=otp_provider,
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=False,
                credentials=None,
            )

        self.assertTrue(result["ok"])
        fill_email_otp.assert_called_once_with(
            driver,
            "user@example.com",
            otp_provider,
            "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback",
        )

    def test_roxy_oauth_retries_failed_round_with_same_open_browser(self):
        driver = Mock()
        opened = Mock(profile_id="profile-1")
        failed = {
            "status": "failed",
            "ok": False,
            "message": "RuntimeError: Codex credential login stayed on the authenticator page after submit",
        }
        success = {"status": "success", "ok": True, "email": "user@example.com"}

        with patch.object(
            roxy_codex_oauth,
            "_run_roxy_codex_oauth_once",
            side_effect=[failed, success],
        ) as run_once, patch.object(roxy_codex_oauth.time, "sleep") as sleep:
            result = roxy_codex_oauth.run_roxy_codex_oauth(
                email="user@example.com",
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=True,
                credentials=self.credentials,
            )

        self.assertEqual(result, success)
        self.assertEqual(run_once.call_count, 2)
        for invocation in run_once.call_args_list:
            self.assertIs(invocation.kwargs["existing_driver"], driver)
            self.assertIs(invocation.kwargs["existing_opened"], opened)
            self.assertTrue(invocation.kwargs["reuse_existing_profile"])
            self.assertTrue(invocation.kwargs["clear_existing_state"])
        sleep.assert_called_once()

    def test_roxy_oauth_does_not_retry_deterministic_password_failure(self):
        driver = Mock()
        opened = Mock(profile_id="profile-1")
        failed = {
            "status": "failed",
            "ok": False,
            "message": "RuntimeError: Codex credential login password was rejected",
        }

        with patch.object(
            roxy_codex_oauth,
            "_run_roxy_codex_oauth_once",
            return_value=failed,
        ) as run_once, patch.object(roxy_codex_oauth.time, "sleep") as sleep:
            result = roxy_codex_oauth.run_roxy_codex_oauth(
                email="user@example.com",
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=True,
                credentials=self.credentials,
            )

        self.assertEqual(result, failed)
        run_once.assert_called_once()
        sleep.assert_not_called()

    def test_roxy_oauth_does_not_retry_locked_account(self):
        driver = Mock()
        opened = Mock(profile_id="profile-1")
        failed = {
            "status": "failed",
            "ok": False,
            "message": "RuntimeError: Your account has been locked",
        }

        with patch.object(
            roxy_codex_oauth,
            "_run_roxy_codex_oauth_once",
            return_value=failed,
        ) as run_once, patch.object(roxy_codex_oauth.time, "sleep") as sleep:
            result = roxy_codex_oauth.run_roxy_codex_oauth(
                email="user@example.com",
                force=True,
                existing_driver=driver,
                existing_opened=opened,
                reuse_existing_profile=True,
                clear_existing_state=True,
                credentials=self.credentials,
            )

        self.assertEqual(result, failed)
        run_once.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
