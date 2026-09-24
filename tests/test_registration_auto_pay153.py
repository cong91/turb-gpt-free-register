import unittest
from unittest.mock import Mock, patch


class RegistrationAutoPay153Tests(unittest.TestCase):
    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.get_account", return_value=None)
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="claimed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_registration_flow_reuses_proxy_and_browser_for_pay153(
        self, run_checkout, _claim_pay153, _get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        browser_transport = Mock()
        run_checkout.return_value = {
            "ok": True,
            "status": "success",
            "result": {"checkout_session_id": "cs_live_registration"},
        }

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            proxy="http://registration-proxy:8080",
            browser_transport=browser_transport,
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertEqual(result["checkout_session_kind"], "cs_live")
        self.assertEqual(run_checkout.call_args.kwargs["proxy"], "http://registration-proxy:8080")
        self.assertIs(run_checkout.call_args.kwargs["browser_transport"], browser_transport)
        self.assertFalse(run_checkout.call_args.kwargs["verify_proxy_country"])
        update_pay153.assert_called_once_with(7, result)

    def test_queues_plan_and_pay_after_twofa_failure_when_enabled(self):
        from core.registration_auto_pay153 import enqueue_registration_auto_pay153

        with (
            patch("config.register.AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", True),
            patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": True, "busy": False},
            ) as enqueue_plan,
        ):
            result = enqueue_registration_auto_pay153(
                account_id=7,
                email="trial@example.com",
                access_token="token",
                proxy="http://proxy.example",
            )

        self.assertTrue(result["accepted"])
        enqueue_plan.assert_called_once_with(
            account_id=7,
            email="trial@example.com",
            access_token="token",
            trigger="registration_auto",
            proxy="http://proxy.example",
        )

    def test_does_not_queue_twofa_recovery_when_pay153_is_disabled(self):
        from core.registration_auto_pay153 import enqueue_registration_auto_pay153

        with (
            patch("config.register.AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", False),
            patch("core.plan_check_service.enqueue_account_plan_check") as enqueue_plan,
        ):
            result = enqueue_registration_auto_pay153(
                account_id=7,
                email="trial@example.com",
                access_token="token",
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "disabled")
        enqueue_plan.assert_not_called()

    def test_interrupted_checkout_retry_requires_confirmation(self):
        from core.registration_auto_pay153 import enqueue_registration_pay153_retry

        with patch(
            "core.registration_auto_pay153.db.get_account",
            return_value={
                "id": 7,
                "email": "trial@example.com",
                "access_token": "token",
                "pay153_recovery_required": True,
            },
        ), patch("core.plan_check_service.enqueue_account_plan_check") as enqueue_plan:
            result = enqueue_registration_pay153_retry(account_id=7)

        self.assertTrue(result["recovery_required"])
        enqueue_plan.assert_not_called()

    def test_confirmed_interrupted_checkout_retry_queues_manual_trigger(self):
        from core.registration_auto_pay153 import enqueue_registration_pay153_retry

        with (
            patch(
                "core.registration_auto_pay153.db.get_account",
                return_value={
                    "id": 7,
                    "email": "trial@example.com",
                    "access_token": "token",
                    "pay153_recovery_required": True,
                },
            ),
            patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": True, "busy": False},
            ) as enqueue_plan,
        ):
            result = enqueue_registration_pay153_retry(
                account_id=7,
                proxy="http://proxy.example",
                confirm_ambiguous_checkout=True,
            )

        self.assertTrue(result["accepted"])
        enqueue_plan.assert_called_once_with(
            account_id=7,
            email="trial@example.com",
            access_token="token",
            trigger="manual_pay153_retry",
            proxy="http://proxy.example",
        )

    def test_classifies_checkout_session_prefix(self):
        from core.registration_auto_pay153 import classify_checkout_session_id

        self.assertEqual(classify_checkout_session_id("oaics_live_123"), "oaics")
        self.assertEqual(classify_checkout_session_id("cs_live_123"), "cs_live")
        self.assertEqual(classify_checkout_session_id("cs_test_123"), "cs_test")
        self.assertEqual(classify_checkout_session_id(""), "unknown")

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.get_account", return_value=None)
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="claimed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_skips_checkout_for_non_free_trial(
        self, run_checkout, _claim_pay153, _get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        result = run_registration_auto_pay153(
            account_id=7,
            email="free@example.com",
            access_token="token",
            proxy="http://proxy.example",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkout_session_kind"], "unknown")
        run_checkout.assert_not_called()
        update_pay153.assert_called_once_with(7, result)

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.get_account", return_value=None)
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="claimed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_runs_checkout_and_persists_oaics_type(
        self, run_checkout, _claim_pay153, _get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        run_checkout.return_value = {
            "ok": True,
            "status": "success",
            "link_type": "ph_short",
            "result": {
                "checkout_session_id": "oaics_live_123",
                "long_url": "https://chatgpt.com/checkout/oaics_live_123",
            },
        }

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            proxy="http://proxy.example",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_session_id"], "oaics_live_123")
        self.assertEqual(result["checkout_session_kind"], "oaics")
        run_checkout.assert_called_once()
        self.assertEqual(run_checkout.call_args.kwargs["token"], "token")
        self.assertEqual(run_checkout.call_args.kwargs["link_type"], "ph_short")
        update_pay153.assert_called_once_with(7, result)

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.get_account", return_value=None)
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="claimed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_unknown_checkout_id_is_failed_closed(
        self, run_checkout, _claim_pay153, _get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        run_checkout.return_value = {
            "ok": True,
            "status": "success",
            "result": {"checkout_session_id": "unexpected_123"},
        }

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checkout_session_kind"], "unknown")
        update_pay153.assert_called_once_with(7, result)

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.get_account", return_value=None)
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="claimed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_provider_ok_false_cannot_be_persisted_as_success(
        self, run_checkout, _claim_pay153, _get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        run_checkout.return_value = {
            "ok": False,
            "status": "success",
            "result": {"checkout_session_id": "cs_live_123"},
        }

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checkout_session_kind"], "cs_live")
        update_pay153.assert_called_once_with(7, result)

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch(
        "core.registration_auto_pay153.db.get_account",
        return_value={
            "pay153_status": "success",
            "pay153_checkout_session_kind": "cs_live",
            "pay153_checked_at": "2026-09-16T00:00:00+00:00",
        },
    )
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="completed")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_does_not_repeat_a_successful_checkout_on_later_retry(
        self, run_checkout, _claim_pay153, get_account, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="new-token",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkout_session_kind"], "cs_live")
        self.assertIn("重复", result["message"])
        get_account.assert_called_once_with(7)
        run_checkout.assert_not_called()
        update_pay153.assert_not_called()

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="busy")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_skips_checkout_when_another_worker_owns_claim(
        self, run_checkout, _claim_pay153, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            proxy="http://proxy.example",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertEqual(result["status"], "skipped")
        self.assertIn("正在执行", result["message"])
        run_checkout.assert_not_called()
        update_pay153.assert_not_called()

    @patch("core.registration_auto_pay153.db.update_account_pay153")
    @patch("core.registration_auto_pay153.db.claim_account_pay153", return_value="recovery_required")
    @patch("core.registration_auto_pay153.extract_link_service._run_local_checkout")
    def test_does_not_repeat_an_interrupted_checkout_without_confirmation(
        self, run_checkout, _claim_pay153, update_pay153
    ):
        from core.registration_auto_pay153 import run_registration_auto_pay153

        result = run_registration_auto_pay153(
            account_id=7,
            email="free-plus@example.com",
            access_token="token",
            plan_result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("未自动重复执行", result["message"])
        run_checkout.assert_not_called()
        update_pay153.assert_not_called()


if __name__ == "__main__":
    unittest.main()
