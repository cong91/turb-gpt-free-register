import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import codex_sub2_free_login, db
from core.personal_info_codex import (
    _browser_context,
    _codex_route_skip_reason,
    _matches_identity,
    record_personal_info_codex,
    run_personal_info_codex,
    run_personal_info_codex_and_record,
    skipped_personal_info_codex,
)
from webui.personal_info_results import _codex_public


class PersonalInfoCodexTests(unittest.TestCase):
    def test_public_progress_keeps_nonterminal_status_without_sensitive_fields(self):
        for status in ("waiting_change", "running"):
            with self.subTest(status=status):
                self.assertEqual(
                    _codex_public({"status": status, "ok": False, "password": "secret"}),
                    {"status": status, "ok": False},
                )

    def test_public_codex_result_drops_sensitive_fields(self):
        result = _codex_public({
            "status": "success",
            "ok": True,
            "credential_saved": True,
            "reason": "safe reason",
            "password": "password-secret",
            "totp_secret": "totp-secret",
            "access_token": "access-secret",
            "callback_url": "https://auth.example/callback?code=secret",
        })

        self.assertEqual(
            result,
            {
                "status": "success",
                "ok": True,
                "credential_saved": True,
                "reason": "safe reason",
            },
        )
        self.assertNotIn("password-secret", json.dumps(result))
        self.assertNotIn("callback", json.dumps(result))

    def test_identity_mismatch_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "không khớp"):
            _matches_identity(
                {"accessToken": "token", "user": {"email": "other@example.com"}},
                "expected@example.com",
            )

    def test_roxy_context_requires_owned_open_profile(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=None)

        with self.assertRaisesRegex(RuntimeError, "ngữ cảnh"):
            _browser_context(profile, "http://proxy.example")

    def test_cloud_codex_failure_is_independent_from_primary_change(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        with (
            patch("core.personal_info_codex._verify_current_identity"),
            patch("core.codex_oauth.run_codex_oauth", side_effect=RuntimeError("oauth failed")),
            patch.object(db, "update_account_codex_status", return_value=True),
        ):
            result = run_personal_info_codex(
                email="user@example.com",
                profile=profile,
                active_proxy="http://proxy.example",
                password="password",
                totp_secret="TOTPSECRET",
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertNotIn("password", result)
        self.assertNotIn("totp_secret", result)

    def test_follow_up_records_codex_failure_without_raising(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        with (
            patch("core.personal_info_codex._verify_current_identity"),
            patch("core.codex_oauth.run_codex_oauth", side_effect=RuntimeError("provider leak")),
            patch.object(db, "update_account_codex_status", side_effect=RuntimeError("db failure")),
        ):
            outcome = run_personal_info_codex_and_record(
                email="user@example.com",
                profile=profile,
                active_proxy="http://proxy.example:3128",
                password="password-secret",
                totp_secret="totp-secret",
            )

        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["status_persisted"])
        self.assertNotIn("password-secret", json.dumps(outcome))
        self.assertNotIn("totp-secret", json.dumps(outcome))
        self.assertNotIn("provider leak", json.dumps(outcome))

    def test_skips_cloud_route_that_cannot_be_proven_and_preserves_session_info(self):
        session_info = SimpleNamespace(raw={})
        profile = SimpleNamespace(
            provider="browser_use",
            driver=SimpleNamespace(browser=Mock(), context=Mock(), page=Mock()),
            session_info=session_info,
        )
        with (
            patch("config.browser_use.BROWSER_USE_USE_PROXY", False),
            patch("core.personal_info_codex._verify_current_identity"),
            patch("core.codex_oauth.run_codex_oauth") as oauth,
        ):
            outcome = run_personal_info_codex(
                email="user@example.com",
                profile=profile,
                active_proxy="http://proxy.example:3128",
                password="password",
                totp_secret="totp",
            )

        self.assertEqual(outcome["status"], "skipped")
        oauth.assert_not_called()

    def test_skyvern_with_proxy_is_skipped_without_opening_another_profile(self):
        profile = SimpleNamespace(provider="skyvern", driver=Mock(), session_info=Mock())
        with (
            patch("core.personal_info_codex._verify_current_identity") as verify,
            patch("core.codex_oauth.run_codex_oauth") as oauth,
        ):
            outcome = run_personal_info_codex(
                email="user@example.com",
                profile=profile,
                active_proxy="http://proxy.example:3128",
                password="password",
                totp_secret="totp",
            )

        self.assertEqual(outcome["status"], "skipped")
        verify.assert_not_called()
        oauth.assert_not_called()

    def test_skipped_follow_up_does_not_overwrite_account_status(self):
        with patch.object(db, "update_account_codex_status") as update:
            outcome = skipped_personal_info_codex("user@example.com", "change failed")
            record_personal_info_codex("other@example.com", {"status": " SKIPPED "})

        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "change failed")
        self.assertNotIn("status_persisted", outcome)
        update.assert_not_called()

    def test_route_skip_is_visible_without_updating_account_status(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        with (
            patch("core.personal_info_codex._codex_route_skip_reason", return_value="route unavailable"),
            patch("core.codex_oauth.run_codex_oauth") as oauth,
            patch.object(db, "update_account_codex_status") as update,
        ):
            outcome = run_personal_info_codex_and_record(
                email="user@example.com", profile=profile, active_proxy=None,
                password="password", totp_secret="totp",
            )

        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "route unavailable")
        update.assert_not_called()
        oauth.assert_not_called()

    def test_terminal_codex_outcomes_still_update_account_status(self):
        for status in ("success", "receipt", "failed"):
            with self.subTest(status=status):
                with patch.object(db, "update_account_codex_status", return_value=True) as update:
                    record_personal_info_codex(
                        "user@example.com", {"status": status, "reason": "reason"}
                    )
                update.assert_called_once_with(
                    "user@example.com", status, "reason" if status == "failed" else None
                )

    def test_explicit_otp_provider_reaches_same_browser_oauth(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        otp_provider = Mock()
        with (
            patch("core.personal_info_codex._codex_route_skip_reason", return_value=None),
            patch("core.personal_info_codex._verify_current_identity"),
            patch("core.codex_oauth._codex_auth_url_source", return_value="sub2"),
            patch("core.codex_oauth.run_codex_oauth", return_value={"status": "receipt", "ok": True}) as oauth,
            patch.object(db, "update_account_codex_status", return_value=True),
        ):
            outcome = run_personal_info_codex_and_record(
                email="user@example.com", profile=profile, active_proxy=None,
                password="password", totp_secret="totp", otp_provider=otp_provider,
            )

        self.assertEqual(outcome["status"], "receipt")
        self.assertIs(oauth.call_args.kwargs["otp_provider"], otp_provider)
        self.assertIs(oauth.call_args.kwargs["existing_driver"], profile.driver)
        self.assertFalse(oauth.call_args.kwargs["clear_existing_state"])

    def test_callback_receipt_is_recorded_without_claiming_a_local_credential(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        with (
            patch("core.personal_info_codex._codex_route_skip_reason", return_value=None),
            patch("core.personal_info_codex._verify_current_identity"),
            patch("core.codex_oauth._codex_auth_url_source", return_value="sub2"),
            patch("core.codex_oauth.run_codex_oauth", return_value={"status": "receipt", "ok": True}),
            patch.object(db, "update_account_codex_status", return_value=True) as update,
        ):
            outcome = run_personal_info_codex_and_record(
                email="user@example.com", profile=profile, active_proxy=None,
                password="password", totp_secret="totp",
            )

        self.assertEqual(outcome["status"], "receipt")
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["credential_saved"])
        update.assert_called_once_with("user@example.com", "receipt", None)
        selection = codex_sub2_free_login.select_target_ids([{
            "id": 7,
            "email": "user@example.com",
            "codex_status": update.call_args.args[1],
            "current_plan_type": "free",
            "plan_check_status": "success",
            "plan_check_ok": True,
            "plus_trial_eligible": False,
        }])
        self.assertEqual(selection["account_ids"], [])
        self.assertEqual(selection["skipped_receipt"], 1)

    def test_roxy_clear_existing_state_is_disabled_and_live_identity_is_verified(self):
        profile = SimpleNamespace(provider="roxy", driver=Mock(), existing_opened=Mock())
        oauth_result = {
            "status": "success",
            "ok": True,
            "file_path": "sqlite://codex_accounts/codex-user@example.com.json",
        }
        with (
            patch("core.personal_info_codex._verify_current_identity") as verify,
            patch("core.codex_oauth.run_codex_oauth", return_value=oauth_result) as oauth,
            patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"),
            patch("config.roxybrowser.ROXY_CREATE_USE_PROXY_POOL", False),
            patch.object(db, "read_codex_credential", return_value=(json.dumps({"access_token": "stored"}), {})),
        ):
            outcome = run_personal_info_codex(
                email="user@example.com",
                profile=profile,
                active_proxy=None,
                password="password",
                totp_secret="totp",
            )

        verify.assert_called_once_with(profile, "user@example.com")
        self.assertFalse(oauth.call_args.kwargs["clear_existing_state"])
        self.assertIs(oauth.call_args.kwargs["existing_driver"], profile.driver)
        self.assertIs(oauth.call_args.kwargs["existing_opened"], profile.existing_opened)
        self.assertEqual(outcome["status"], "success")


if __name__ == "__main__":
    unittest.main()
