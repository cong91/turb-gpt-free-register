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
            ),
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
            ),
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


class RunSyncPlanCheckTests(unittest.TestCase):
    def test_sync_plan_check_uses_browser_transport_and_persists_result(self):
        from unittest.mock import patch as mock_patch

        with (
            mock_patch.object(plan_check_service.db, "claim_account_plan_check", return_value=True) as claim,
            mock_patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
            mock_patch.object(plan_check_service.db, "update_account_plan_check") as update,
            mock_patch.object(plan_check_service, "_wait_for_rate_slot"),
            mock_patch.object(
                plan_check_service,
                "check_account_plan",
                return_value={"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
            ) as check_plan,
        ):
            result = plan_check_service.run_sync_plan_check(
                account_id=5,
                email="user@example.com",
                access_token="token",
                trigger="twofa_change",
                browser_transport=object(),
            )

        self.assertTrue(result["accepted"])
        self.assertTrue(result["ok"])
        claim.assert_called_once_with(acc_id=5, trigger="twofa_change")
        check_plan.assert_called_once()
        kwargs = check_plan.call_args.kwargs
        self.assertEqual(kwargs["proxy"], "")
        self.assertIsNotNone(kwargs["browser_transport"])
        update.assert_called_once()

    def test_sync_plan_check_busy_claim_skips_check(self):
        from unittest.mock import patch as mock_patch

        with (
            mock_patch.object(plan_check_service.db, "claim_account_plan_check", return_value=False),
            mock_patch.object(
                plan_check_service,
                "check_account_plan",
                side_effect=AssertionError("must not run when claim is busy"),
            ),
        ):
            result = plan_check_service.run_sync_plan_check(
                account_id=5,
                email="user@example.com",
                access_token="token",
                trigger="twofa_change",
                browser_transport=object(),
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["busy"])

    def test_sync_plan_check_requires_transport_and_token(self):
        result = plan_check_service.run_sync_plan_check(
            account_id=5,
            email="user@example.com",
            access_token="",
            trigger="twofa_change",
            browser_transport=object(),
        )
        self.assertFalse(result["accepted"])
        self.assertIn("access token", result["error"])

        result = plan_check_service.run_sync_plan_check(
            account_id=5,
            email="user@example.com",
            access_token="token",
            trigger="twofa_change",
            browser_transport=None,
        )
        self.assertFalse(result["accepted"])
        self.assertIn("browser transport", result["error"])


class RequiredAccountProxyRouteTests(unittest.TestCase):
    def test_falls_back_to_active_nordvpn_system_route(self):
        from core import account_network

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False),
            patch("core.account_network.resolve_rotating_proxy", return_value=None),
            patch("config.proxy.PLAN_CHECK_PROXY", ""),
            patch("config.proxy.pick_proxy", return_value=""),
            patch("core.nordvpn_cli.is_connected", return_value=True),
            account_network.required_account_proxy(
                None,
                rotating_scope="plan_check",
            ) as (route, mode),
        ):
            self.assertIsNone(route)
            self.assertEqual(mode, "nordvpn_system")

    def test_uses_configured_plan_proxy_before_pool(self):
        from core import account_network

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False),
            patch("core.account_network.resolve_rotating_proxy", return_value=None),
            patch("config.proxy.PLAN_CHECK_PROXY", "http://plan-proxy.example:8080"),
            patch("config.proxy.pick_proxy", side_effect=AssertionError("pool must not run")),
            account_network.required_account_proxy(
                None,
                rotating_scope="plan_check",
            ) as (route, mode),
        ):
            self.assertEqual(route, "http://plan-proxy.example:8080")
            self.assertEqual(mode, "plan_proxy")

    def test_raises_when_no_route_and_nordvpn_disconnected(self):
        from core import account_network

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False),
            patch("core.account_network.resolve_rotating_proxy", return_value=None),
            patch("config.proxy.PLAN_CHECK_PROXY", ""),
            patch("config.proxy.pick_proxy", return_value=""),
            patch("core.nordvpn_cli.is_connected", return_value=False),
            self.assertRaisesRegex(RuntimeError, "NordVPN"),
            account_network.required_account_proxy(
                None,
                rotating_scope="plan_check",
            ),
        ):
            pass

    def test_prefers_wireguard_lease_when_per_profile_proxy_enabled(self):
        from core import account_network

        @contextmanager
        def wireguard_context(*_args, **_kwargs):
            yield "socks5://127.0.0.1:25000"

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True),
            patch(
                "core.nordvpn_wireguard.proxy_for_registration",
                side_effect=wireguard_context,
            ),
            patch(
                "core.account_network.resolve_rotating_proxy",
                side_effect=AssertionError("rotating proxy must not run"),
            ),
            account_network.required_account_proxy(
                None,
                rotating_scope="plan_check",
            ) as (route, mode),
        ):
            self.assertEqual(route, "socks5://127.0.0.1:25000")
            self.assertEqual(mode, "nordvpn_wireguard")


if __name__ == "__main__":
    unittest.main()
