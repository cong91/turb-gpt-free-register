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

    def test_plan_worker_uses_wireguard_when_no_proxy_is_supplied(self):
        payload = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }

        @contextmanager
        def wireguard_context():
            yield "socks5://127.0.0.1:25000"

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
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True),
            patch(
                "core.nordvpn_wireguard.proxy_for_registration",
                side_effect=wireguard_context,
            ) as wireguard,
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
        wireguard.assert_called_once_with()
        check_plan.assert_called_once_with(
            "token",
            proxy="socks5://127.0.0.1:25000",
            timezone_offset_min="-",
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

        try:
            with (
                patch.object(plan_check_service.db, "claim_account_plan_check", return_value=True),
                patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
                patch.object(plan_check_service, "check_account_plan", side_effect=fake_check),
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
