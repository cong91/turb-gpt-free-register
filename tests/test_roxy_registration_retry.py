import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import roxy_registration


class RoxyRegistrationRetryTests(unittest.TestCase):
    def test_free_without_plus_trial_runs_codex_oauth_in_current_profile(self):
        email = "user@example.com"
        driver = Mock()
        opened = SimpleNamespace(profile_id="profile-1", raw={"profile": "profile-1"})
        client = Mock()
        client.open_profile.return_value = opened
        codex_result = {"ok": True, "status": "success"}

        def run_auto(**kwargs):
            return {
                "plan": {
                    "ok": True,
                    "current_plan_type": "free",
                    "plus_trial_eligible": False,
                },
                "codex": kwargs["run_codex"](),
            }

        with ExitStack() as stack:
            for target in (
                "core.roxy_registration._center_browser_window",
                "core.roxy_registration._safe_get",
                "core.roxy_registration._page_warmup",
                "core.roxy_registration._maybe_accept",
                "core.roxy_registration._check_manual_stop",
                "core.roxy_registration._complete_email_otp",
                "core.roxy_registration.human_delay",
                "core.roxy_registration.db.update_account_2fa",
                "core.roxy_registration.post_register_dwell",
            ):
                stack.enter_context(patch(target))
            stack.enter_context(patch("core.roxy_registration.RoxyBrowserClient", return_value=client))
            stack.enter_context(patch("core.roxy_registration._build_driver", return_value=driver))
            stack.enter_context(patch("core.roxy_registration._submit_email_and_wait_next", return_value="password"))
            stack.enter_context(patch("core.roxy_registration._fill_password_page_if_present", return_value="openai-password"))
            stack.enter_context(patch("core.roxy_registration._complete_profile_page", return_value=True))
            stack.enter_context(
                patch(
                    "core.roxy_registration._fetch_chatgpt_session",
                    return_value={"accessToken": "access-token", "user": {}, "account": {}, "expires": None},
                )
            )
            stack.enter_context(patch("core.roxy_registration.resolve_email_source", return_value="gmail_api_url"))
            stack.enter_context(patch("core.roxy_registration.checkpoint_account_data", return_value=7))
            stack.enter_context(patch("core.account_export.setup_2fa_for_registration", return_value="TOTPSECRET"))
            stack.enter_context(
                patch("core.registration_auto_codex.run_registration_auto_codex", side_effect=run_auto)
            )
            run_codex = stack.enter_context(
                patch("core.codex_oauth.run_codex_oauth", return_value=codex_result)
            )
            stack.enter_context(patch("core.roxy_registration.save_account_data", return_value=7))
            stack.enter_context(patch("config.twofa.ENABLE_2FA", True))
            stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
            stack.enter_context(patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False))
            stack.enter_context(patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True))
            stack.enter_context(patch("config.roxybrowser.ROXY_KEEP_BROWSER_OPEN", True))
            result = roxy_registration.run_roxy_registration(
                email=email,
                name="Test User",
                birthday="1990-01-01",
                proxy="socks5://127.0.0.1:25000",
            )

        self.assertTrue(result["success"])
        run_codex.assert_called_once_with(
            email,
            oauth_driver="roxy",
            force=True,
            credentials=unittest.mock.ANY,
            existing_driver=driver,
            existing_opened=opened,
        )

    @patch("core.roxy_registration._assert_not_external_idp")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._wait_email_submit_next_state", side_effect=["unknown", "otp"])
    @patch("core.roxy_registration._submit_email_step")
    @patch("core.roxy_registration._email_input_value_state")
    def test_retry_reloads_login_page_after_spa_clears_email_inputs(
        self,
        email_state,
        _submit,
        _wait_state,
        _human_delay,
        maybe_accept,
        assert_not_external,
    ):
        driver = Mock()
        email = "user@example.com"
        email_state.side_effect = [
            {"url": "https://chatgpt.com/auth/login", "inputs": [{"value": email}]},
            {"url": "https://chatgpt.com/auth/login", "inputs": []},
            {"url": "https://chatgpt.com/auth/login", "inputs": [{"value": email}]},
        ]

        def type_email(_driver, _email, timeout):
            if type_email.calls and not driver.get.called:
                raise RuntimeError("stale SPA login DOM")
            type_email.calls += 1

        type_email.calls = 0
        with patch("core.roxy_registration._type_email_address", side_effect=type_email):
            result = roxy_registration._submit_email_and_wait_next(driver, email, attempts=2)

        self.assertEqual(result, "otp")
        driver.get.assert_called_once_with("https://chatgpt.com/auth/login")
        maybe_accept.assert_called_once_with(driver)
        assert_not_external.assert_called_once_with(driver, "retry login page")
        self.assertEqual(type_email.calls, 2)


if __name__ == "__main__":
    unittest.main()
