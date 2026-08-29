import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from core import browser_twofa_retry


class BrowserTwofaRetryTests(unittest.TestCase):
    def test_retry_uses_generic_browser_session_and_persists_provider(self):
        driver = Mock()
        profile = Mock(driver=driver, provider="cloak", timeout=90)
        account = {
            "id": 7,
            "email": "user@example.com",
            "registration_password": "password",
            "access_token": "old-token",
        }

        with (
            patch("core.browser_twofa_retry.open_browser_profile", return_value=profile),
            patch(
                "core.browser_twofa_retry._login_existing_account",
                return_value={"accessToken": "new-token", "user": {}, "account": {}},
            ),
            patch("core.browser_twofa_retry.setup_2fa_in_page", return_value="SECRET") as setup_2fa,
            patch("core.browser_twofa_retry.save_account_data", return_value=8) as save_account,
            patch("core.browser_twofa_retry.resolve_email_source", return_value="paymesh"),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False),
            patch("config.codex.ENABLE_CODEX_AUTO", False),
        ):
            result = browser_twofa_retry.run_twofa_retry(account, proxy="http://proxy")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], 8)
        self.assertEqual(result["browser_provider"], "cloak")
        self.assertEqual(save_account.call_args.kwargs["proxy_used"], "http://proxy")
        self.assertEqual(save_account.call_args.kwargs["extra"]["registration_driver"], "cloak")
        setup_2fa.assert_called_once_with(
            driver,
            "user@example.com",
            reauth=True,
        )
        profile.close.assert_called_once_with()
        profile.cleanup.assert_called_once_with()

    def test_retry_uses_wireguard_lease_when_proxy_is_not_explicit(self):
        driver = Mock()
        profile = Mock(driver=driver, provider="cloak", timeout=90)
        account = {
            "id": 7,
            "email": "user@example.com",
            "registration_password": "password",
        }

        @contextmanager
        def wireguard_context():
            yield "socks5://127.0.0.1:25000"

        with (
            patch("core.browser_twofa_retry.open_browser_profile", return_value=profile) as open_profile,
            patch(
                "core.browser_twofa_retry._login_existing_account",
                return_value={"accessToken": "new-token"},
            ),
            patch("core.browser_twofa_retry.setup_2fa_in_page", return_value="SECRET"),
            patch("core.browser_twofa_retry.save_account_data", return_value=8),
            patch("core.browser_twofa_retry.resolve_email_source", return_value="gmail_api_url"),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False),
            patch("config.codex.ENABLE_CODEX_AUTO", False),
            patch(
                "core.nordvpn_wireguard.proxy_for_registration",
                side_effect=wireguard_context,
            ) as wireguard,
        ):
            result = browser_twofa_retry.run_twofa_retry(account)

        self.assertTrue(result["ok"])
        wireguard.assert_called_once_with()
        open_profile.assert_called_once_with(proxy="socks5://127.0.0.1:25000")

    def test_retry_runs_serialized_plan_and_codex_before_saving(self):
        driver = Mock()
        profile = Mock(driver=driver, provider="cloak", timeout=90)
        account = {
            "id": 7,
            "email": "user@example.com",
            "registration_password": "password",
            "access_token": "old-token",
        }
        auto_result = {
            "plan": {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
            "codex": {"ok": True, "status": "success"},
        }

        with (
            patch("core.browser_twofa_retry.open_browser_profile", return_value=profile),
            patch(
                "core.browser_twofa_retry._login_existing_account",
                return_value={"accessToken": "new-token", "user": {}, "account": {}},
            ),
            patch("core.browser_twofa_retry.setup_2fa_in_page", return_value="SECRET"),
            patch("core.browser_twofa_retry.save_account_data", return_value=8) as save_account,
            patch("core.browser_twofa_retry.resolve_email_source", return_value="gmail_api_url"),
            patch("core.registration_auto_codex.run_registration_auto_codex", return_value=auto_result) as run_auto,
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", True),
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True),
            patch("config.codex.ENABLE_CODEX_AUTO", False),
        ):
            result = browser_twofa_retry.run_twofa_retry(account, proxy="socks5://127.0.0.1:25000")

        self.assertTrue(result["ok"])
        run_auto.assert_called_once()
        auto_kwargs = run_auto.call_args.kwargs
        self.assertEqual(auto_kwargs["account_id"], 7)
        self.assertEqual(auto_kwargs["email"], "user@example.com")
        self.assertEqual(auto_kwargs["access_token"], "new-token")
        self.assertEqual(auto_kwargs["proxy"], "socks5://127.0.0.1:25000")
        self.assertEqual(auto_kwargs["twofa_status"], "active")
        self.assertIsNotNone(auto_kwargs["browser_transport"])
        self.assertEqual(save_account.call_args.kwargs["auto_plan_check"], False)
        self.assertEqual(save_account.call_args.kwargs["extra"]["codex"], auto_result["codex"])

    def test_retry_reauthenticates_after_a_failed_attempt(self):
        driver = Mock()
        profile = Mock(driver=driver, provider="roxy", timeout=120)
        account = {"id": 7, "email": "user@example.com", "registration_password": "password"}

        with (
            patch("core.browser_twofa_retry.open_browser_profile", return_value=profile),
            patch(
                "core.browser_twofa_retry._login_existing_account",
                side_effect=[RuntimeError("first login failed"), {"accessToken": "token"}],
            ) as login,
            patch("core.browser_twofa_retry.setup_2fa_in_page", return_value="SECRET"),
            patch("core.browser_twofa_retry.save_account_data", return_value=7),
            patch("core.browser_twofa_retry.resolve_email_source", return_value="paymesh"),
            patch("core.browser_twofa_retry.human_delay"),
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False),
            patch("config.codex.ENABLE_CODEX_AUTO", False),
            patch(
                "core.nordvpn_wireguard.proxy_for_registration",
                side_effect=lambda: _null_proxy_context(),
            ),
        ):
            result = browser_twofa_retry.run_twofa_retry(account, max_attempts=2)

        self.assertTrue(result["ok"])
        self.assertEqual(login.call_count, 2)
        driver.get.assert_called_once_with("https://chatgpt.com/auth/login")


@contextmanager
def _null_proxy_context():
    yield None


if __name__ == "__main__":
    unittest.main()
