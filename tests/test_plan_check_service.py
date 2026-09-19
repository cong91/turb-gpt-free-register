import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from core import plan_check_service


class PlanCheckWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._prepare_proxy = patch.object(plan_check_service, "prepare_rotating_proxy_lanes")
        self._prepare_proxy.start()
        self.addCleanup(self._prepare_proxy.stop)

    def test_plan_worker_requires_rotating_or_pool_proxy_when_no_proxy_is_supplied(self):
        payload = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy-pool.example:8080", "proxy_pool"

        with (
            patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
            patch.object(plan_check_service, "_wait_for_rate_slot"),
            patch.object(plan_check_service, "_registration_recheck_delay", return_value=0),
            patch.object(plan_check_service, "check_account_plan", return_value=payload) as check_plan,
            patch.object(plan_check_service.db, "update_account_plan_check"),
            patch.object(plan_check_service._QUEUE_SLOTS, "release"),
            patch.object(
                plan_check_service,
                "_run_auto_codex_oauth_for_free_account",
                return_value={"accepted": False, "reason": "disabled"},
            ),
            patch.object(
                plan_check_service,
                "required_account_proxy",
                side_effect=proxy_context,
            ) as required_proxy,
        ):
            result = plan_check_service._run_plan_check(
                account_id=1,
                email="user@example.com",
                access_token="token",
                trigger="registration_auto",
                proxy=None,
                timezone_offset_min="-",
            )

        self.assertEqual(result, payload)
        required_proxy.assert_called_once()
        check_plan.assert_called_once_with(
            "token",
            proxy="http://proxy-pool.example:8080",
            timezone_offset_min="-",
        )

    def test_manual_promotion_runs_pay153_and_rechecks_free_trial(self):
        first_plan = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }
        second_plan = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://vn-proxy.example:8080", "rotating_proxy"

        with (
            patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
            patch.object(
                plan_check_service,
                "required_account_proxy",
                side_effect=proxy_context,
            ) as required_proxy,
            patch.object(plan_check_service, "_wait_for_rate_slot"),
            patch.object(plan_check_service, "_registration_recheck_delay", return_value=0),
            patch.object(plan_check_service, "check_account_plan", side_effect=[first_plan, second_plan]) as check_plan,
            patch.object(plan_check_service.db, "update_account_plan_check") as update_plan,
            patch.object(plan_check_service.db, "update_account_pay153_promotion") as update_promotion,
            patch.object(plan_check_service._QUEUE_SLOTS, "release"),
            patch(
                "core.account_pay153_promotion.run_account_pay153_promotion_probe",
                return_value={"status": "success", "ok": True, "plus_trial_eligible_after": True},
            ) as run_promotion,
            patch.object(
                plan_check_service,
                "_run_auto_codex_oauth_for_free_account",
                return_value={"accepted": False, "reason": "trigger"},
            ),
        ):
            result = plan_check_service._run_plan_check(
                account_id=7,
                email="free@example.com",
                access_token="token",
                trigger="manual_pay153_promotion",
                proxy=None,
                timezone_offset_min="-",
            )

        self.assertEqual(result, second_plan)
        self.assertEqual(check_plan.call_count, 2)
        run_promotion.assert_called_once_with(
            account_id=7,
            email="free@example.com",
            access_token="token",
            plan_result=first_plan,
        )
        self.assertGreaterEqual(update_plan.call_count, 2)
        update_promotion.assert_called_once()

    def test_plan_worker_runs_pay153_only_after_trial_plan_is_persisted(self):
        payload = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy-pool.example:8080", "proxy_pool"

        with (
            patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
            patch.object(
                plan_check_service,
                "required_account_proxy",
                side_effect=proxy_context,
            ) as required_proxy,
            patch.object(plan_check_service, "_wait_for_rate_slot"),
            patch.object(plan_check_service, "_registration_recheck_delay", return_value=0),
            patch.object(plan_check_service, "check_account_plan", return_value=payload),
            patch.object(plan_check_service.db, "update_account_plan_check") as update_plan,
            patch.object(plan_check_service._QUEUE_SLOTS, "release"),
            patch("config.register.AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", True),
            patch(
                "core.registration_auto_pay153.run_registration_auto_pay153",
                return_value={"status": "success", "ok": True},
            ) as run_pay153,
            patch.object(
                plan_check_service,
                "_run_auto_codex_oauth_for_free_account",
                return_value={"accepted": False, "reason": "disabled"},
            ),
        ):
            result = plan_check_service._run_plan_check(
                account_id=1,
                email="trial@example.com",
                access_token="token",
                trigger="registration_auto",
                proxy="http://proxy.example",
                timezone_offset_min="-",
            )

        self.assertEqual(result, payload)
        update_plan.assert_called_once_with(acc_id=1, result=payload)
        required_proxy.assert_called_once_with(
            "http://proxy.example",
            rotating_scope=plan_check_service.PLAN_CHECK_PROXY_SCOPE,
            lane_id=None,
            lease_owner_id=None,
        )
        run_pay153.assert_called_once_with(
            account_id=1,
            email="trial@example.com",
            access_token="token",
            plan_result=payload,
        )

    def test_idle_worker_stops_and_next_batch_gets_a_fresh_executor(self):
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="plan-check-test",
        )
        previous_executor = plan_check_service._EXECUTOR
        plan_check_service._EXECUTOR = executor
        completed = threading.Event()

        def fake_check(*_args, **_kwargs):
            completed.set()
            return {
                "ok": True,
                "current_plan_type": "free",
                "plus_trial_eligible": True,
            }

        def no_plan_check_workers():
            return not any(
                thread.name.startswith("plan-check")
                for thread in threading.enumerate()
            )

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy-pool.example:8080", "proxy_pool"

        try:
            with (
                patch.object(plan_check_service.db, "claim_account_plan_check", return_value=True),
                patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
                patch.object(plan_check_service, "check_account_plan", side_effect=fake_check),
                patch.object(plan_check_service, "required_account_proxy", side_effect=proxy_context),
                patch.object(plan_check_service, "_registration_recheck_delay", return_value=0),
                patch.object(plan_check_service, "_wait_for_rate_slot"),
                patch.object(plan_check_service.db, "update_account_plan_check"),
                patch.object(
                    plan_check_service,
                    "_run_auto_codex_oauth_for_free_account",
                    return_value={"accepted": False, "reason": "disabled"},
                ),
            ):
                first = plan_check_service.enqueue_account_plan_check(
                    account_id=1,
                    email="first@example.com",
                    access_token="token-1",
                    trigger="manual_import",
                    proxy="",
                )
                self.assertTrue(first["accepted"])
                self.assertTrue(completed.wait(2))
                self.assertTrue(
                    self._wait_until(no_plan_check_workers),
                    "plan-check worker survived after the queue became idle",
                )
                self.assertIsNone(plan_check_service._EXECUTOR)

                completed.clear()
                second = plan_check_service.enqueue_account_plan_check(
                    account_id=2,
                    email="second@example.com",
                    access_token="token-2",
                    trigger="manual_import",
                    proxy="",
                )
                self.assertTrue(second["accepted"])
                self.assertTrue(completed.wait(2))
                self.assertTrue(self._wait_until(no_plan_check_workers))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            plan_check_service._EXECUTOR = previous_executor

    @staticmethod
    def _wait_until(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))
        return True


if __name__ == "__main__":
    unittest.main()
