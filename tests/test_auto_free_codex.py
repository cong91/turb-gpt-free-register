import unittest
from unittest.mock import patch

from config import register as register_config
from core import account_export, plan_check_service, registration_service


class AutoFreeCodexTests(unittest.TestCase):
    def setUp(self):
        self._rotating_proxy = patch("core.account_network.resolve_rotating_proxy", return_value=None)
        self._rotating_proxy.start()
        self.addCleanup(self._rotating_proxy.stop)

    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True})
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=7)
    def test_option_also_enables_registration_plan_check(
        self, insert_account, _archive, _mark_consumed, enqueue_plan
    ):
        with (
            patch.object(register_config, "AUTO_PLAN_CHECK_AFTER_REGISTER", False),
            patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True),
            patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol"),
        ):
            account_export.save_account_data(
                email="free@example.com",
                access_token="token",
                auto_plan_check=None,
            )

        insert_account.assert_called_once()
        enqueue_plan.assert_called_once()

    def test_free_without_plus_trial_runs_codex_oauth_after_registration_plan_check(self):
        result = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        with (
            patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True, create=True),
            patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol"),
            patch.object(plan_check_service.db, "get_account", return_value={"id": 7}),
            patch.object(plan_check_service.db, "update_account_codex_status") as update_status,
            patch(
                "core.codex_oauth.run_codex_oauth",
                return_value={"ok": True, "status": "success"},
            ) as run_oauth,
            patch("core.registration_service.submit_codex_retry_for_account", create=True) as submit,
        ):
            outcome = plan_check_service._run_auto_codex_oauth_for_free_account(
                account_id=7,
                email="free@example.com",
                access_token="token",
                trigger="registration_auto",
                result=result,
                proxy="socks5://127.0.0.1:25000",
            )

        self.assertTrue(outcome["accepted"])
        self.assertEqual(outcome["status"], "success")
        run_oauth.assert_called_once_with(
            "free@example.com",
            proxy="socks5://127.0.0.1:25000",
            force=True,
        )
        update_status.assert_called_once_with("free@example.com", "success", None)
        submit.assert_not_called()

    def test_free_plus_trial_does_not_enqueue_codex(self):
        with patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True, create=True), patch(
            "core.codex_oauth.run_codex_oauth", create=True
        ) as submit:
            outcome = plan_check_service._run_auto_codex_oauth_for_free_account(
                account_id=7,
                email="free-plus@example.com",
                access_token="token",
                trigger="registration_auto",
                result={
                    "ok": True,
                    "current_plan_type": "free",
                    "plus_trial_eligible": True,
                },
            )

        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "free_plus_or_unknown")
        submit.assert_not_called()

    def test_browser_registration_plan_worker_does_not_enqueue_standalone_codex(self):
        with (
            patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True, create=True),
            patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak"),
            patch("core.codex_oauth.run_codex_oauth", create=True) as submit,
        ):
            outcome = plan_check_service._run_auto_codex_oauth_for_free_account(
                account_id=7,
                email="free-browser@example.com",
                access_token="token",
                trigger="registration_auto",
                result={
                    "ok": True,
                    "current_plan_type": "free",
                    "plus_trial_eligible": False,
                },
            )

        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "live_browser_required")
        submit.assert_not_called()

    @patch.object(plan_check_service._QUEUE_SLOTS, "release")
    @patch.object(plan_check_service, "_registration_recheck_delay", return_value=0)
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service, "check_account_plan")
    @patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak")
    @patch("core.registration_service.submit_codex_retry_for_account")
    def test_browser_registration_plan_worker_never_calls_codex_retry_worker(
        self,
        submit_codex,
        check_plan,
        _mark_running,
        update_plan,
        _wait,
        _delay,
        _release,
    ):
        check_plan.return_value = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        outcome = plan_check_service._run_plan_check(
            account_id=7,
            email="free-browser@example.com",
            access_token="token",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-",
        )

        self.assertTrue(outcome["ok"])
        update_plan.assert_called_once_with(acc_id=7, result=outcome)
        submit_codex.assert_not_called()

    @patch.object(plan_check_service._QUEUE_SLOTS, "release")
    @patch.object(plan_check_service, "_registration_recheck_delay", return_value=0)
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service.db, "get_account", return_value={
        "id": 7,
        "extra_json": '{"registration_driver":"cloak"}',
    })
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service, "check_account_plan")
    @patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol")
    @patch("core.registration_service.submit_codex_retry_for_account")
    def test_browser_registration_driver_from_account_wins_over_live_config(
        self,
        submit_codex,
        check_plan,
        _mark_running,
        _get_account,
        update_plan,
        _wait,
        _delay,
        _release,
    ):
        check_plan.return_value = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        outcome = plan_check_service._run_plan_check(
            account_id=7,
            email="free-browser@example.com",
            access_token="token",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-",
        )

        self.assertTrue(outcome["ok"])
        update_plan.assert_called_once_with(acc_id=7, result=outcome)
        submit_codex.assert_not_called()

    def test_unknown_plus_trial_status_does_not_enqueue_codex(self):
        with patch.object(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True, create=True), patch(
            "core.codex_oauth.run_codex_oauth", create=True
        ) as submit:
            outcome = plan_check_service._run_auto_codex_oauth_for_free_account(
                account_id=7,
                email="free-unknown@example.com",
                access_token="token",
                trigger="registration_auto",
                result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": None},
            )

        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "free_plus_or_unknown")
        submit.assert_not_called()

    @patch("core.registration_service.db.get_account")
    def test_already_successful_codex_is_not_queued_again(self, get_account):
        get_account.return_value = {"id": 7, "codex_status": "success"}

        with patch.object(registration_service.codex_retry_service, "reserve") as reserve:
            outcome = registration_service.submit_codex_retry_for_account(
                account_id=7,
                email="done@example.com",
                access_token="token",
                trigger="registration_auto_free",
            )

        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "already_success")
        reserve.assert_not_called()

    @patch.object(plan_check_service, "_run_auto_codex_oauth_for_free_account")
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service, "check_account_plan")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    @patch.object(plan_check_service._QUEUE_SLOTS, "release")
    def test_registration_plan_worker_invokes_auto_codex_after_persisting_plan(
        self, _release, _wait, _mark_running, check_plan, update_plan, enqueue_codex
    ):
        result = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        check_plan.return_value = result

        outcome = plan_check_service._run_plan_check(
            account_id=7,
            email="free@example.com",
            access_token="token",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-",
        )

        self.assertEqual(outcome, result)
        update_plan.assert_called_once_with(acc_id=7, result=result)
        enqueue_codex.assert_called_once_with(
            account_id=7,
            email="free@example.com",
            access_token="token",
            trigger="registration_auto",
            result=result,
            proxy=None,
        )

    @patch.object(plan_check_service, "_run_auto_codex_oauth_for_free_account", return_value={"accepted": False, "reason": "disabled"})
    @patch.object(plan_check_service.db, "update_account_plan_check")
    @patch.object(plan_check_service, "check_account_plan")
    @patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True)
    @patch.object(plan_check_service, "_wait_for_rate_slot")
    @patch.object(plan_check_service, "_registration_recheck_delay", return_value=0.1)
    @patch.object(plan_check_service.time, "sleep")
    @patch.object(plan_check_service._QUEUE_SLOTS, "release")
    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False)
    def test_registration_plan_worker_rechecks_transient_failure(
        self,
        _wireguard_enabled,
        _release,
        sleep,
        _delay,
        _wait,
        _mark_running,
        check_plan,
        update_plan,
        _enqueue_codex,
    ):
        first = {"ok": False, "retryable": True, "error": "ProxyError: SOCKS5 connection failed"}
        second = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        check_plan.side_effect = [first, second]

        outcome = plan_check_service._run_plan_check(
            account_id=7,
            email="free@example.com",
            access_token="token",
            trigger="registration_auto",
            proxy=None,
            timezone_offset_min="-",
        )

        self.assertEqual(outcome, second)
        self.assertEqual(check_plan.call_count, 2)
        self.assertEqual(check_plan.call_args_list[1].kwargs["max_attempts"], 1)
        sleep.assert_called_once_with(0.1)
        update_plan.assert_called_once_with(acc_id=7, result=second)


if __name__ == "__main__":
    unittest.main()
