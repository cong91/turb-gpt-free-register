import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


class RegistrationAutoCodexTests(unittest.TestCase):
    def test_protocol_registration_forces_codex_after_serialized_plan_flow(self):
        import main

        session = Mock(
            proxy="http://proxy.example",
            exit_geo={"ip": "203.0.113.10"},
            device_id="device",
            auth_session_logging_id="session",
            sentinel_sid="sid",
            browser_profile=None,
        )
        auto_codex_kwargs = {}

        def run_auto_codex(**kwargs):
            auto_codex_kwargs.update(kwargs)
            return {"codex": kwargs["run_codex"]()}

        with ExitStack() as stack:
            stack.enter_context(patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"))
            stack.enter_context(patch.object(main._twofa_cfg, "ENABLE_2FA", True))
            stack.enter_context(patch.object(main._protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", False))
            stack.enter_context(patch.object(main._protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", False))
            stack.enter_context(patch.object(main, "BrowserSession", return_value=session))
            stack.enter_context(patch.object(main, "network_preflight"))
            stack.enter_context(patch.object(main, "get_providers", return_value=[]))
            stack.enter_context(patch.object(main, "get_csrf_token", return_value="csrf"))
            stack.enter_context(patch.object(main, "signin_openai", return_value="https://auth.example"))
            stack.enter_context(patch.object(main, "follow_authorize"))
            stack.enter_context(
                patch.object(
                    main,
                    "validate_email_otp",
                    return_value={
                        "page": {"type": "external_url"},
                        "external_url": "https://chatgpt.com/api/auth/callback?code=code",
                    },
                )
            )
            stack.enter_context(patch.object(main, "follow_oauth_callback"))
            stack.enter_context(
                patch.object(
                    main,
                    "fetch_session",
                    return_value={"accessToken": "access-token", "user": {}, "account": {}},
                )
            )
            stack.enter_context(patch.object(main, "checkpoint_account_data", return_value=7))
            stack.enter_context(patch.object(main, "setup_2fa_for_registration", return_value="TOTPSECRET"))
            stack.enter_context(
                patch(
                    "core.registration_auto_codex.run_registration_auto_codex",
                    side_effect=run_auto_codex,
                )
            )
            run_codex_oauth = stack.enter_context(
                patch(
                    "core.codex_oauth.run_codex_oauth",
                    return_value={"status": "success", "ok": True},
                )
            )
            stack.enter_context(patch.object(main, "save_account_data", return_value=8))
            stack.enter_context(patch("core.email_provider.resolve_email_source", return_value="outlook"))
            stack.enter_context(patch("core.flow_trigger.trigger_flow", return_value={"status": "skipped", "ok": False}))
            stack.enter_context(patch.object(main, "human_delay"))

            result = main.run_registration(
                email="user@example.com",
                name="Test User",
                birthday="1990-01-01",
                otp_code="123456",
            )

        self.assertTrue(result["success"])
        self.assertEqual(auto_codex_kwargs["twofa_status"], "active")
        run_codex_oauth.assert_called_once_with(
            "user@example.com",
            proxy="http://proxy.example",
            force=True,
        )

    @patch("core.plan_check_service.enqueue_account_plan_check")
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=7)
    def test_synchronous_registration_does_not_enqueue_a_second_plan_task(
        self, insert_account, _archive, _mark_consumed, enqueue_plan
    ):
        from config import register as register_config
        from core.account_export import save_account_data

        with (
            patch.object(register_config, "AUTO_PLAN_CHECK_AFTER_REGISTER", True),
            patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True),
        ):
            save_account_data(
                email="free@example.com",
                access_token="token",
                auto_plan_check=False,
            )

        insert_account.assert_called_once()
        enqueue_plan.assert_not_called()

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_plan_is_recorded_before_codex_runs_on_same_registration_flow(
        self, check_plan, claim_plan, mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        events = []
        check_plan.side_effect = lambda *args, **kwargs: events.append("plan") or {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        def run_codex():
            events.append("codex")
            return {"status": "success", "ok": True}

        outcome = run_registration_auto_codex(
            account_id=7,
            email="free@example.com",
            access_token="token",
            proxy="http://proxy.example",
            run_codex=run_codex,
        )

        self.assertEqual(events, ["plan", "codex"])
        self.assertEqual(outcome["plan"]["current_plan_type"], "free")
        self.assertEqual(outcome["codex"]["status"], "success")
        claim_plan.assert_called_once_with(acc_id=7, trigger="registration_auto")
        mark_running.assert_called_once_with(7)
        update_plan.assert_called_once()
        check_plan.assert_called_once_with(
            "token", proxy="http://proxy.example", timezone_offset_min="-"
        )

    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=False)
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_plan_claim_conflict_fails_registration_codex_step(self, claim_plan):
        from core.registration_auto_codex import run_registration_auto_codex

        run_codex = Mock()
        outcome = run_registration_auto_codex(
            account_id=7,
            email="busy@example.com",
            access_token="token",
            run_codex=run_codex,
        )

        self.assertFalse(outcome["codex"]["ok"])
        self.assertEqual(outcome["codex"]["status"], "failed")
        self.assertIn("占用", outcome["codex"]["message"])
        run_codex.assert_not_called()
        claim_plan.assert_called_once_with(acc_id=7, trigger="registration_auto")

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", True)
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
    @patch("config.codex.ENABLE_CODEX_AUTO", False)
    def test_plan_only_mode_checks_plan_without_starting_codex(
        self, check_plan, _claim_plan, _mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        check_plan.return_value = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }
        run_codex = Mock()

        outcome = run_registration_auto_codex(
            account_id=7,
            email="plan-only@example.com",
            access_token="token",
            run_codex=run_codex,
        )

        self.assertTrue(outcome["plan"]["ok"])
        self.assertEqual(outcome["codex"]["status"], "skipped")
        run_codex.assert_not_called()
        update_plan.assert_called_once_with(acc_id=7, result=outcome["plan"])

    @patch("core.registration_auto_codex.db.claim_account_plan_check")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_inactive_twofa_blocks_plan_and_codex(self, claim_plan):
        from core.registration_auto_codex import run_registration_auto_codex

        run_codex = Mock()
        outcome = run_registration_auto_codex(
            account_id=7,
            email="pending-2fa@example.com",
            access_token="token",
            run_codex=run_codex,
            twofa_status="pending",
        )

        self.assertFalse(outcome["codex"]["ok"])
        self.assertEqual(outcome["codex"]["status"], "failed")
        self.assertIn("2FA", outcome["codex"]["message"])
        claim_plan.assert_not_called()
        run_codex.assert_not_called()

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
    @patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False)
    @patch("config.codex.ENABLE_CODEX_AUTO", True)
    def test_generic_codex_runs_only_after_plan_check(
        self, check_plan, _claim_plan, _mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        events = []
        check_plan.side_effect = lambda *args, **kwargs: events.append("plan") or {
            "ok": True,
            "current_plan_type": "plus",
            "plus_trial_eligible": None,
        }

        def run_codex():
            events.append("codex")
            return {"status": "success", "ok": True}

        outcome = run_registration_auto_codex(
            account_id=7,
            email="generic-codex@example.com",
            access_token="token",
            run_codex=run_codex,
        )

        self.assertEqual(events, ["plan", "codex"])
        self.assertEqual(outcome["codex"]["status"], "success")
        update_plan.assert_called_once_with(acc_id=7, result=outcome["plan"])

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_registration_plan_check_receives_existing_browser_transport(
        self, check_plan, _claim_plan, _mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        browser_transport = Mock()
        check_plan.return_value = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        outcome = run_registration_auto_codex(
            account_id=7,
            email="free@example.com",
            access_token="token",
            proxy="http://registration-proxy:1",
            browser_transport=browser_transport,
            run_codex=Mock(return_value={"status": "success", "ok": True}),
        )

        self.assertEqual(outcome["codex"]["status"], "success")
        check_plan.assert_called_once_with(
            "token",
            proxy="http://registration-proxy:1",
            browser_transport=browser_transport,
            timezone_offset_min="-",
        )
        update_plan.assert_called_once_with(acc_id=7, result=outcome["plan"])

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_plus_trial_plan_does_not_start_codex(
        self, check_plan, _claim_plan, _mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        check_plan.return_value = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }
        run_codex = Mock()

        outcome = run_registration_auto_codex(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            run_codex=run_codex,
        )

        run_codex.assert_not_called()
        self.assertEqual(outcome["codex"]["status"], "skipped")
        self.assertIn("Plus", outcome["codex"]["message"])
        update_plan.assert_called_once()

    @patch("time.sleep")
    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan")
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_transient_plan_failure_retries_before_codex_login(
        self, check_plan, _claim_plan, _mark_running, update_plan, sleep
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        browser_transport = Mock()
        check_plan.side_effect = [
            {"ok": False, "retryable": True, "error": "ProxyError: SOCKS5 connection failed"},
            {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
        ]
        run_codex = Mock(return_value={"status": "success", "ok": True})

        outcome = run_registration_auto_codex(
            account_id=7,
            email="free@example.com",
            access_token="token",
            proxy="http://broken-proxy.example",
            browser_transport=browser_transport,
            run_codex=run_codex,
        )

        self.assertEqual(outcome["codex"]["status"], "success")
        run_codex.assert_called_once_with()
        self.assertEqual(
            check_plan.call_args_list,
            [
                call(
                    "token",
                    proxy="http://broken-proxy.example",
                    browser_transport=browser_transport,
                    timezone_offset_min="-",
                ),
                call(
                    "token",
                    proxy="http://broken-proxy.example",
                    browser_transport=browser_transport,
                    timezone_offset_min="-",
                ),
            ],
        )
        sleep.assert_called_once()
        update_plan.assert_called_once_with(acc_id=7, result=outcome["plan"])

    @patch("core.registration_auto_codex.db.update_account_plan_check")
    @patch("core.registration_auto_codex.db.mark_account_plan_check_running", return_value=True)
    @patch("core.registration_auto_codex.db.claim_account_plan_check", return_value=True)
    @patch("core.registration_auto_codex.check_account_plan", return_value={
        "ok": False,
        "retryable": True,
        "error": "ProxyError: SOCKS5 connection failed",
    })
    @patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True)
    def test_exhausted_plan_retry_is_failed_and_never_starts_codex(
        self, check_plan, _claim_plan, _mark_running, update_plan
    ):
        from core.registration_auto_codex import run_registration_auto_codex

        with patch("time.sleep"):
            outcome = run_registration_auto_codex(
                account_id=7,
                email="free@example.com",
                access_token="token",
                proxy="http://broken-proxy.example",
                run_codex=Mock(),
            )

        self.assertEqual(outcome["codex"]["status"], "failed")
        self.assertFalse(outcome["codex"]["ok"])
        self.assertEqual(check_plan.call_count, 2)
        update_plan.assert_called_once_with(acc_id=7, result=outcome["plan"])

    @patch("core.codex_oauth._save_cpa_local_record", return_value=None)
    @patch("core.codex_oauth._submit_cpa_callback", return_value={})
    @patch(
        "core.codex_oauth._request_cpa_authorize_url",
        return_value={"auth_url": "https://auth.example/authorize", "state": "state"},
    )
    @patch("core.codex_oauth._extract_code", return_value="code")
    @patch("core.browser_use_codex_oauth._finish_consent_workspace", return_value="http://localhost:1455/callback?code=code&state=state")
    @patch("core.browser_use_codex_oauth._do_phone_verification_if_present")
    @patch("core.browser_use_codex_oauth._fill_email_and_otp")
    @patch("core.browser_use_codex_oauth._install_account_dead_response_tracker", return_value={})
    @patch("core.browser_use_codex_oauth.BrowserUseClient")
    def test_existing_browser_is_reused_without_opening_or_closing_a_session(
        self,
        browser_client,
        _tracker,
        fill_email,
        _phone,
        _finish,
        _extract,
        _request_cpa,
        _submit_cpa,
        _save_cpa,
    ):
        from core import browser_use_codex_oauth as oauth

        browser = Mock()
        context = Mock()
        page = Mock()
        session_info = SimpleNamespace(proxy_country_code="vn", profile_id="registration-profile")

        with patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"):
            result = oauth._run_browser_use_codex_oauth_once(
                email="free@example.com",
                otp_provider=Mock(),
                force=True,
                existing_browser=browser,
                existing_context=context,
                existing_page=page,
                existing_session_info=session_info,
            )

        self.assertTrue(result["ok"])
        browser_client.assert_not_called()
        browser.close.assert_not_called()
        fill_email.assert_called_once()


if __name__ == "__main__":
    unittest.main()
