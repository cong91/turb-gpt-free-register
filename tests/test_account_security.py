import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from core import db
from core.account_security import (
    TwofaChangeInput,
    _extract_mfa_factor_id,
    change_twofa_in_browser,
    deactivate_2fa_in_page,
    parse_twofa_change_inputs,
)
from core.browser_profile import open_browser_profile
from core.browser_twofa_change import run_twofa_change
from webui.app import create_app


class TwofaChangeInputTests(unittest.TestCase):
    def test_parses_credential_lines_with_pipe_in_password(self):
        result = parse_twofa_change_inputs(
            "old@example.com|pa|ss|JBSWY3DPEHPK3PXP\n"
        )

        self.assertEqual(
            result,
            [
                TwofaChangeInput(
                    email="old@example.com",
                    password="pa|ss",
                    current_totp_secret="JBSWY3DPEHPK3PXP",
                )
            ],
        )

    def test_rejects_duplicate_emails_and_empty_input(self):
        with self.assertRaisesRegex(ValueError, "required"):
            parse_twofa_change_inputs("")
        with self.assertRaisesRegex(ValueError, "unique"):
            parse_twofa_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP\n"
                "OLD@example.com|secret|JBSWY3DPEHPK3PXP"
            )


class BrowserProfileSelectionTests(unittest.TestCase):
    def test_personal_info_uses_the_configured_browser_provider(self):
        with (
            patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak"),
            patch("core.browser_profile._open_cloak") as open_cloak,
        ):
            expected = object()
            open_cloak.return_value = expected

            result = open_browser_profile()

        self.assertIs(result, expected)
        open_cloak.assert_called_once_with()

    def test_personal_info_routes_cloud_browser_aliases_to_generic_opener(self):
        with patch("core.browser_profile._open_cloud") as open_cloud:
            for configured, expected in (("browser_use", "browser_use"), ("skyvern", "skyvern")):
                with self.subTest(configured=configured), patch(
                    "config.roxybrowser.REGISTRATION_DRIVER", configured
                ):
                    open_browser_profile()

                open_cloud.assert_called_with(expected)

    def test_personal_info_does_not_silently_fallback_for_unknown_provider(self):
        with (
            patch("config.roxybrowser.REGISTRATION_DRIVER", "unknown-browser"),
            self.assertRaisesRegex(RuntimeError, "unknown-browser"),
        ):
            open_browser_profile()


class TwofaBrowserWorkflowTests(unittest.TestCase):
    def test_factor_lookup_prefers_totp_over_other_factor_types(self):
        self.assertEqual(
            _extract_mfa_factor_id(
                {
                    "native_default_factor_id": "passkey-id",
                    "factors": {
                        "totp": [{"id": "totp-id", "factor_type": "totp"}],
                        "passkeys": [{"id": "passkey-id"}],
                    },
                }
            ),
            "totp-id",
        )

    def test_deactivate_looks_up_factor_and_posts_to_disable_in_house(self):
        driver = Mock(current_url="https://chatgpt.com/")
        with (
            patch("core.account_security.fetch_session", return_value={"accessToken": "access-token"}),
            patch("core.account_security.BrowserPageTransport") as transport_cls,
        ):
            transport = transport_cls.return_value
            transport.device_id = "device-id"
            transport.get_chatgpt_headers.side_effect = lambda **_: {}
            transport.navigator_language.return_value = "en-US"
            info_response = Mock(status_code=200, text='{"mfa_enabled":true}')
            info_response.json.return_value = {
                "mfa_enabled": True,
                "mfa_enabled_v2": True,
                "native_default_factor_id": "factor-id",
                "other_providers_without_mfa": [],
                "show_push_auth": False,
                "show_sms": True,
                "show_passkey": True,
                "factors": {
                    "totp": [{
                        "id": "factor-id",
                        "factor_type": "totp",
                        "is_recovery": False,
                        "metadata": None,
                    }],
                    "push_auth": None,
                    "passkeys": [],
                    "sms": [],
                },
            }
            transport.get.return_value = info_response
            response = Mock(status_code=200, text="{}")
            transport.post.return_value = response

            result = deactivate_2fa_in_page(driver)

        self.assertTrue(result)
        transport.post.assert_called_once()
        args, kwargs = transport.post.call_args
        url = args[0]
        self.assertEqual(url, "https://chatgpt.com/backend-api/accounts/mfa/user/disable_in_house")
        self.assertEqual(json.loads(kwargs["data"]), {"factor_id": "factor-id"})
        self.assertIn("Bearer access-token", kwargs["headers"]["authorization"])
        self.assertEqual(
            kwargs["headers"]["x-openai-target-path"],
            "/backend-api/accounts/mfa/user/disable_in_house",
        )
        transport.get.assert_called_once()
        info_args, info_kwargs = transport.get.call_args
        self.assertEqual(info_args[0], "https://chatgpt.com/backend-api/accounts/mfa_info")
        self.assertEqual(
            info_kwargs["headers"]["x-openai-target-path"],
            "/backend-api/accounts/mfa_info",
        )

    def test_deactivate_does_not_post_when_factor_id_is_missing(self):
        driver = Mock(current_url="https://chatgpt.com/")
        with (
            patch("core.account_security.fetch_session", return_value={"accessToken": "access-token"}),
            patch("core.account_security.BrowserPageTransport") as transport_cls,
        ):
            transport = transport_cls.return_value
            transport.get_chatgpt_headers.return_value = {}
            transport.navigator_language.return_value = "en-US"
            info_response = Mock(status_code=200, text='{"factors":[]}')
            info_response.json.return_value = {"factors": []}
            transport.get.return_value = info_response

            with self.assertRaisesRegex(RuntimeError, "no factor_id"):
                deactivate_2fa_in_page(driver)

        transport.post.assert_not_called()

    def test_change_workflow_deactivates_before_reenrolling(self):
        driver = Mock(current_url="https://chatgpt.com/")
        item = TwofaChangeInput("user@example.com", "password", "OLDSECRET")
        calls = []

        def login(_driver, credentials):
            calls.append(("login", credentials.email))

        def deactivate(_driver, *, access_token):
            calls.append(("deactivate",))
            self.assertEqual(access_token, "fresh-token")
            return True

        def setup(_driver, email):
            calls.append(("setup", email))
            return "NEWSECRET"

        with (
            patch("core.email_change._login_chatgpt_with_credentials", side_effect=login),
            patch("core.account_security.fetch_session", return_value={"accessToken": "fresh-token"}),
            patch("core.account_security.deactivate_2fa_in_page", side_effect=deactivate),
            patch("core.account_security.setup_2fa_in_page", side_effect=setup),
        ):
            result = change_twofa_in_browser(driver, item)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("login", "user@example.com"), ("deactivate",), ("setup", "user@example.com")])
        self.assertEqual(result["new_totp_secret"], "NEWSECRET")

    def test_login_is_retried_when_session_has_no_access_token(self):
        driver = Mock(current_url="https://chatgpt.com/")
        item = TwofaChangeInput("user@example.com", "password", "OLDSECRET")
        with (
            patch("core.email_change._login_chatgpt_with_credentials") as login,
            patch(
                "core.account_security.fetch_session",
                side_effect=[{}, {"accessToken": "fresh-token"}],
            ),
            patch("core.account_security.deactivate_2fa_in_page") as deactivate,
            patch("core.account_security.setup_2fa_in_page", return_value="NEWSECRET"),
        ):
            result = change_twofa_in_browser(driver, item)

        self.assertTrue(result["ok"])
        self.assertEqual(login.call_count, 2)
        deactivate.assert_called_once_with(driver, access_token="fresh-token")
        self.assertEqual(result["access_token"], "fresh-token")

    def test_login_is_retried_when_session_request_fails(self):
        driver = Mock(current_url="https://chatgpt.com/")
        item = TwofaChangeInput("user@example.com", "password", "OLDSECRET")
        with (
            patch("core.email_change._login_chatgpt_with_credentials"),
            patch(
                "core.account_security.fetch_session",
                side_effect=[RuntimeError("session transport failed"), {"accessToken": "fresh-token"}],
            ),
            patch("core.account_security.deactivate_2fa_in_page"),
            patch("core.account_security.setup_2fa_in_page", return_value="NEWSECRET"),
        ):
            result = change_twofa_in_browser(driver, item)

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "fresh-token")


class TwofaRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.stack = ExitStack()
        for name, filename in (
            ("_ACCOUNTS_JSON", "accounts.json"),
            ("_LEGACY_ACCOUNTS_JSON", "legacy.json"),
            ("_ACCOUNTS_TXT", "accounts.txt"),
            ("_TOKENS_TXT", "tokens.txt"),
            ("_VIEWER_HTML", "viewer.html"),
        ):
            self.stack.enter_context(patch.object(db, name, root / filename))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_missing_email_is_saved_and_processed_without_duplicate(self):
        item = TwofaChangeInput("missing@example.com", "password", "OLDSECRET")
        driver = Mock()
        with (
            patch("core.browser_twofa_change.open_browser_profile", return_value=Mock(driver=driver, provider="roxy")),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={
                "ok": True,
                "email": item.email,
                "new_totp_secret": "NEWSECRET",
            }),
        ):
            result = run_twofa_change(item)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(db.count_accounts(), 1)
        stored = db.get_account_by_email(item.email)
        self.assertEqual(stored["registration_password"], item.password)
        self.assertEqual(stored["totp_secret"], "NEWSECRET")
        self.assertEqual(stored["twofa_status"], "active")

    def test_success_updates_existing_account_without_creating_duplicate(self):
        account_id = db.insert_account(
            email="old@example.com",
            access_token="old-token",
            registration_password="password",
            totp_secret="OLDSECRET",
            twofa_status="active",
        )
        item = TwofaChangeInput("old@example.com", "password", "OLDSECRET")
        driver = Mock()
        with (
            patch("core.browser_twofa_change.open_browser_profile", return_value=Mock(driver=driver, provider="roxy")),
            patch("core.browser_twofa_change.change_twofa_in_browser", return_value={
                "ok": True,
                "email": item.email,
                "new_totp_secret": "NEWSECRET",
            }),
        ):
            result = run_twofa_change(item)

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertEqual(result["account_id"], account_id)
        self.assertEqual(db.count_accounts(), 1)
        stored = db.get_account(account_id)
        self.assertEqual(stored["totp_secret"], "NEWSECRET")
        self.assertEqual(stored["twofa_status"], "active")

    def test_new_access_token_is_saved_even_when_replacement_fails(self):
        account_id = db.insert_account(
            email="old@example.com",
            access_token="old-token",
            registration_password="password",
            totp_secret="OLDSECRET",
            twofa_status="active",
        )
        item = TwofaChangeInput("old@example.com", "password", "OLDSECRET")
        with (
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=Mock(driver=Mock(), provider="roxy"),
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": False,
                    "email": item.email,
                    "remote_disabled": False,
                    "access_token": "fresh-token",
                    "error": "replacement failed",
                },
            ),
        ):
            result = run_twofa_change(item)

        self.assertFalse(result["ok"])
        self.assertTrue(result["access_token_saved"])
        self.assertNotIn("access_token", result)
        self.assertEqual(db.get_account(account_id)["access_token"], "fresh-token")

    def test_failure_after_deactivate_clears_old_secret_and_marks_account_failed(self):
        account_id = db.insert_account(
            email="old@example.com",
            access_token="old-token",
            registration_password="password",
            totp_secret="OLDSECRET",
            twofa_status="active",
        )
        item = TwofaChangeInput("old@example.com", "password", "OLDSECRET")
        with (
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=Mock(driver=Mock(), provider="roxy"),
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": False,
                    "email": item.email,
                    "remote_disabled": True,
                    "error": "enroll failed",
                },
            ),
        ):
            result = run_twofa_change(item)

        self.assertFalse(result["ok"])
        stored = db.get_account(account_id)
        self.assertEqual(stored["totp_secret"], None)
        self.assertEqual(stored["twofa_status"], "failed")
        self.assertIn("enroll failed", stored["twofa_error"])

    def test_empty_new_secret_after_deactivate_clears_old_secret(self):
        account_id = db.insert_account(
            email="old@example.com",
            access_token="old-token",
            registration_password="password",
            totp_secret="OLDSECRET",
            twofa_status="active",
        )
        item = TwofaChangeInput("old@example.com", "password", "OLDSECRET")
        with (
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=Mock(driver=Mock(), provider="roxy"),
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": True,
                    "email": item.email,
                    "remote_disabled": True,
                    "new_totp_secret": "",
                },
            ),
        ):
            result = run_twofa_change(item)

        self.assertFalse(result["ok"])
        self.assertTrue(result["remote_disabled"])
        stored = db.get_account(account_id)
        self.assertIsNone(stored["totp_secret"])
        self.assertEqual(stored["twofa_status"], "failed")

    def test_remote_failure_stays_visible_when_local_failure_state_cannot_be_saved(self):
        account_id = db.insert_account(
            email="old@example.com",
            access_token="old-token",
            registration_password="password",
            totp_secret="OLDSECRET",
            twofa_status="active",
        )
        item = TwofaChangeInput("old@example.com", "password", "OLDSECRET")
        with (
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=Mock(driver=Mock(), provider="roxy"),
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": False,
                    "email": item.email,
                    "remote_disabled": True,
                    "error": "enroll failed",
                },
            ),
            patch.object(db, "update_account_2fa", side_effect=RuntimeError("disk failure")),
        ):
            result = run_twofa_change(item)

        self.assertFalse(result["ok"])
        self.assertTrue(result["remote_disabled"])
        self.assertIn("disk failure", result["warning"])
        self.assertEqual(result["account_id"], account_id)


class TwofaApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()

    @patch("webui.email_change_api.db.save_personal_info_change_batch")
    @patch("webui.email_change_api.run_twofa_change_batch")
    def test_change_twofa_route_returns_statuses_and_persists_export_batch(self, run_batch, save_batch):
        run_batch.return_value = [{
            "ok": True,
            "persisted": True,
            "email": "user@example.com",
            "account_id": 7,
            "access_token": "token-must-stay-server-side",
            "new_totp_secret": "SECRET-MUST-STAY-SERVER-SIDE",
        }]
        save_batch.return_value = {"batch_id": "b" * 32, "exportable_count": 1}

        response = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": "user@example.com|password|OLDSECRET", "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["succeeded"], 1)
        self.assertRegex(payload["change_batch_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["exportable_count"], 1)
        self.assertNotIn("new_totp_secret", json.dumps(payload))
        self.assertNotIn("token-must-stay-server-side", json.dumps(payload))
        run_batch.assert_called_once()
        save_batch.assert_called_once()
        self.assertEqual(save_batch.call_args.args[1], "twofa")

    @patch("webui.email_change_api.db.save_personal_info_change_batch", return_value={"batch_id": "d" * 32, "exportable_count": 0})
    @patch("webui.email_change_api.run_twofa_change_batch")
    def test_change_twofa_route_fails_when_access_token_was_not_saved(self, run_batch, _save_batch):
        run_batch.return_value = [{
            "ok": True,
            "persisted": True,
            "access_token_saved": False,
            "email": "user@example.com",
            "account_id": 7,
        }]

        response = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": "user@example.com|password|OLDSECRET"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["succeeded"], 0)
        self.assertEqual(payload["failed"], 1)

    @patch("webui.email_change_api.db.get_personal_info_change_export_rows")
    @patch("webui.email_change_api.db.get_personal_info_change_batch")
    def test_personal_info_export_alias_exports_db_backed_twofa_batch(self, get_batch, get_rows):
        get_batch.return_value = {"batch_id": "batch-2", "mode": "twofa", "exportable_count": 1}
        get_rows.return_value = [{
            "email": "user@example.com",
            "registration_password": "password",
            "totp_secret": "NEWSECRET",
        }]

        response = self.client.post(
            "/api/accounts/personal-info/export",
            json={"batch_id": "batch-2"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "user@example.com | password | NEWSECRET\n")
        self.assertIn("personal-info-updated-accounts.txt", response.headers["Content-Disposition"])
        get_batch.assert_called_once_with("batch-2")
        get_rows.assert_called_once_with("batch-2")


if __name__ == "__main__":
    unittest.main()
