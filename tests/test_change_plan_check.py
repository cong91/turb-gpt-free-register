"""Regression tests: post-change plan check reuses the live browser session."""
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from core.account_security import TwofaChangeInput
from core.browser_password_change import run_password_change
from core.browser_twofa_change import run_twofa_change
from core.password_change import PasswordChangeInput


def _twofa_item() -> TwofaChangeInput:
    return TwofaChangeInput(
        email="user@example.com",
        password="password",
        current_totp_secret="OLDTOTPSECRET",
    )


def _password_item() -> PasswordChangeInput:
    return PasswordChangeInput(
        email="user@example.com",
        current_password="cur-pw",
        new_password="NEWPW-123",
        mode="post_login_password_reset",
    )


class TwofaChangeLivePlanCheckTests(unittest.TestCase):
    def test_success_runs_plan_check_inside_open_browser_before_cleanup(self):
        events = []
        profile = Mock(provider="roxy")
        profile.driver = Mock()
        profile.close.side_effect = lambda: events.append("close")
        profile.cleanup.side_effect = lambda: events.append("cleanup")

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy.example:8080", "rotating_proxy"

        def fake_plan_check(**_kwargs):
            events.append("plan_check")
            return {
                "accepted": True,
                "ok": True,
                "current_plan_type": "free",
                "plus_trial_eligible": False,
            }

        with (
            patch(
                "core.browser_twofa_change.db.get_account_by_email",
                return_value={"id": 7, "access_token": "old-token"},
            ),
            patch(
                "core.browser_twofa_change.preferred_account_proxy",
                side_effect=proxy_context,
            ),
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=profile,
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": True,
                    "email": "user@example.com",
                    "new_totp_secret": "NEWSERTOTSECRET",
                    "remote_disabled": True,
                    "access_token": "new-token",
                },
            ) as change_twofa,
            patch("core.browser_twofa_change.db.update_account_access_token", return_value=True),
            patch("core.browser_twofa_change.db.update_account_2fa", return_value=True),
            patch(
                "core.plan_check_service.run_sync_plan_check",
                side_effect=fake_plan_check,
            ) as plan_check,
            patch(
                "core.plan_check_service.enqueue_account_plan_check"
            ) as enqueue_plan,
        ):
            result = run_twofa_change(_twofa_item())

        self.assertTrue(result["ok"])
        self.assertTrue(result["plan_check"]["ok"])
        self.assertEqual(result["plan_check"]["current_plan_type"], "free")
        change_twofa.assert_called_once()
        self.assertTrue(change_twofa.call_args.kwargs["keep_session"])
        plan_check.assert_called_once()
        plan_kwargs = plan_check.call_args.kwargs
        self.assertEqual(plan_kwargs["account_id"], 7)
        self.assertEqual(plan_kwargs["trigger"], "twofa_change")
        self.assertIsNotNone(plan_kwargs["browser_transport"])
        enqueue_plan.assert_not_called()
        # Plan check must run while the browser is still open.
        self.assertEqual(events, ["plan_check", "close", "cleanup"])

    def test_failure_skips_plan_check_and_closes_browser_immediately(self):
        profile = Mock(provider="roxy")
        profile.driver = Mock()

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield None, "direct"

        with (
            patch(
                "core.browser_twofa_change.db.get_account_by_email",
                return_value={"id": 7, "access_token": "old-token"},
            ),
            patch(
                "core.browser_twofa_change.preferred_account_proxy",
                side_effect=proxy_context,
            ),
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=profile,
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": False,
                    "email": "user@example.com",
                    "error": "login failed",
                },
            ),
            patch(
                "core.plan_check_service.run_sync_plan_check",
            ) as plan_check,
        ):
            result = run_twofa_change(_twofa_item())

        self.assertFalse(result["ok"])
        self.assertNotIn("plan_check", result)
        plan_check.assert_not_called()
        # Mỗi attempt thất bại phải tự dọn browser ngay, không giữ để check plan.
        self.assertEqual(profile.close.call_count, 3)
        self.assertEqual(profile.cleanup.call_count, 3)

    def test_plan_check_failure_keeps_twofa_success(self):
        profile = Mock(provider="roxy")
        profile.driver = Mock()

        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield None, "direct"

        with (
            patch(
                "core.browser_twofa_change.db.get_account_by_email",
                return_value={"id": 7, "access_token": "old-token"},
            ),
            patch(
                "core.browser_twofa_change.preferred_account_proxy",
                side_effect=proxy_context,
            ),
            patch(
                "core.browser_twofa_change.open_browser_profile",
                return_value=profile,
            ),
            patch(
                "core.browser_twofa_change.change_twofa_in_browser",
                return_value={
                    "ok": True,
                    "email": "user@example.com",
                    "new_totp_secret": "NEWSERTOTSECRET",
                    "remote_disabled": True,
                    "access_token": "new-token",
                },
            ),
            patch("core.browser_twofa_change.db.update_account_access_token", return_value=True),
            patch("core.browser_twofa_change.db.update_account_2fa", return_value=True),
            patch(
                "core.plan_check_service.run_sync_plan_check",
                return_value={"accepted": False, "ok": False, "error": "boom"},
            ),
        ):
            result = run_twofa_change(_twofa_item())

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertFalse(result["plan_check"]["ok"])
        self.assertEqual(result["plan_check"]["error"], "boom")


class PasswordChangeLivePlanCheckTests(unittest.TestCase):
    def _patches(self, profile, change_result):
        return (
            patch(
                "core.browser_password_change.db.get_account_by_email",
                return_value={"id": 7, "access_token": "old-token"},
            ),
            patch(
                "core.browser_password_change.preferred_account_proxy",
                side_effect=self._proxy_context(),
            ),
            patch(
                "core.browser_password_change.open_browser_profile",
                return_value=profile,
            ),
            patch(
                "core.browser_password_change.change_password_in_browser",
                return_value=change_result,
            ),
        )

    @staticmethod
    def _proxy_context():
        @contextmanager
        def proxy_context(*_args, **_kwargs):
            yield "http://proxy.example:8080", "rotating_proxy"

        return proxy_context

    def test_success_runs_plan_check_inside_open_browser_before_cleanup(self):
        events = []
        profile = Mock(provider="roxy")
        profile.driver = Mock()
        profile.close.side_effect = lambda: events.append("close")
        profile.cleanup.side_effect = lambda: events.append("cleanup")

        def fake_plan_check(**_kwargs):
            events.append("plan_check")
            return {
                "accepted": True,
                "ok": True,
                "current_plan_type": "plus",
                "plus_trial_eligible": None,
            }

        patches = self._patches(
            profile,
            {
                "ok": True,
                "email": "user@example.com",
                "mode": "post_login_password_reset",
                "access_token": "new-token",
            },
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("core.browser_password_change.db.update_account_access_token", return_value=True),
            patch("core.browser_password_change.db.update_account_password", return_value=True),
            patch(
                "core.plan_check_service.run_sync_plan_check",
                side_effect=fake_plan_check,
            ) as plan_check,
        ):
            result = run_password_change(_password_item())

        self.assertTrue(result["ok"])
        self.assertTrue(result["plan_check"]["ok"])
        plan_check.assert_called_once()
        plan_kwargs = plan_check.call_args.kwargs
        self.assertEqual(plan_kwargs["trigger"], "password_change")
        self.assertIsNotNone(plan_kwargs["browser_transport"])
        self.assertEqual(events, ["plan_check", "close", "cleanup"])

    def test_failure_skips_plan_check_and_closes_browser_immediately(self):
        profile = Mock(provider="roxy")
        profile.driver = Mock()

        patches = self._patches(
            profile,
            {
                "ok": False,
                "email": "user@example.com",
                "mode": "post_login_password_reset",
                "error": "challenge failed",
            },
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch(
                "core.plan_check_service.run_sync_plan_check",
            ) as plan_check,
        ):
            result = run_password_change(_password_item())

        self.assertFalse(result["ok"])
        self.assertNotIn("plan_check", result)
        plan_check.assert_not_called()
        # Mỗi attempt thất bại phải tự dọn browser ngay, không giữ để check plan.
        self.assertEqual(profile.close.call_count, 3)
        self.assertEqual(profile.cleanup.call_count, 3)


if __name__ == "__main__":
    unittest.main()
