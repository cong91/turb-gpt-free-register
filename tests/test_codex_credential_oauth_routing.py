import inspect
import unittest
from unittest.mock import Mock, patch

from config import codex as codex_config
from config import roxybrowser as roxy_config
from core.codex_login_credentials import CodexLoginCredentials
from core.codex_oauth import run_codex_oauth
from core.roxy_codex_oauth import _run_roxy_codex_oauth_once, run_roxy_codex_oauth


class CodexCredentialOAuthRoutingTests(unittest.TestCase):
    def setUp(self):
        self.credentials = CodexLoginCredentials(
            email="user@example.com",
            password="openai-password",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    def test_oauth_entrypoint_accepts_credential_context(self):
        self.assertIn("credentials", inspect.signature(run_codex_oauth).parameters)

    @patch("core.cloakbrowser_driver.build_cloak_driver")
    @patch("core.roxy_codex_oauth.run_roxy_codex_oauth")
    def test_explicit_same_as_registration_driver_overrides_codex_setting(
        self, run_roxy, build_cloak
    ):
        driver = Mock()
        build_cloak.return_value = (driver, Mock())
        run_roxy.return_value = {"ok": True, "status": "success"}

        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "roxy"), \
             patch.object(roxy_config, "REGISTRATION_DRIVER", "cloak"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                oauth_driver="same_as_registration",
            )

        self.assertTrue(result["ok"])
        build_cloak.assert_called_once_with(proxy=None)
        self.assertIs(run_roxy.call_args.kwargs["existing_driver"], driver)

    def test_roxy_entrypoints_accept_credential_context(self):
        self.assertIn("credentials", inspect.signature(run_roxy_codex_oauth).parameters)
        self.assertIn("credentials", inspect.signature(_run_roxy_codex_oauth_once).parameters)

    @patch("core.roxy_codex_oauth.run_roxy_codex_oauth")
    def test_roxy_receives_credential_context(self, run_roxy):
        run_roxy.return_value = {"ok": True, "status": "success"}

        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "roxy"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                credentials=self.credentials,
            )

        self.assertTrue(result["ok"])
        self.assertIn("credentials", run_roxy.call_args.kwargs)
        self.assertIs(run_roxy.call_args.kwargs["credentials"], self.credentials)

    @patch("core.cloakbrowser_driver.build_cloak_driver")
    @patch("core.roxy_codex_oauth.run_roxy_codex_oauth")
    def test_cloak_receives_credential_context(self, run_roxy, build_cloak):
        driver = Mock()
        opened = Mock()
        build_cloak.return_value = (driver, opened)
        run_roxy.return_value = {"ok": True, "status": "success"}

        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "cloak"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                credentials=self.credentials,
            )

        self.assertTrue(result["ok"])
        self.assertIn("credentials", run_roxy.call_args.kwargs)
        self.assertIs(run_roxy.call_args.kwargs["credentials"], self.credentials)

    @patch("core.browser_use_codex_oauth.run_browser_use_codex_oauth")
    def test_browser_use_driver_receives_reusable_credentials(self, run_browser):
        run_browser.return_value = {"ok": True, "status": "success"}

        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "browser_use"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                credentials=self.credentials,
            )

        self.assertTrue(result["ok"])
        run_browser.assert_called_once()
        self.assertIs(run_browser.call_args.kwargs["credentials"], self.credentials)

    @patch("core.skyvern_codex_oauth.run_skyvern_codex_oauth")
    def test_skyvern_driver_receives_reusable_credentials(self, run_skyvern):
        run_skyvern.return_value = {"ok": True, "status": "success"}

        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "skyvern"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                credentials=self.credentials,
            )

        self.assertTrue(result["ok"])
        run_skyvern.assert_called_once()
        self.assertIs(run_skyvern.call_args.kwargs["credentials"], self.credentials)

    @patch("core.codex_oauth.BrowserSession")
    def test_protocol_rejects_reusable_credentials_before_network(self, browser_session):
        with patch.object(codex_config, "CODEX_OAUTH_DRIVER", "protocol"):
            result = run_codex_oauth(
                "user@example.com",
                force=True,
                credentials=self.credentials,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Roxy/Cloak", result["message"])
        browser_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
