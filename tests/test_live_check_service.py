import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core.live_check_browser  # noqa: F401 - patch target needs the module imported.
from core import db, live_check_service


def _db_context(root: Path) -> ExitStack:
    stack = ExitStack()
    for target, value in (
        ("_ACCOUNTS_JSON", root / "accounts.json"),
        ("_ACCOUNTS_TXT", root / "accounts.txt"),
        ("_TOKENS_TXT", root / "tokens.txt"),
        ("_OUTLOOK_JSON", root / "outlook.json"),
        ("_OUTLOOK_TXT", root / "outlook.txt"),
        ("_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
        ("_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
    ):
        stack.enter_context(patch.object(db, target, value))
    stack.enter_context(patch.object(db, "_schedule_static_viewer_refresh"))
    return stack


_ROUTE = {
    "proxy": None,
    "proxy_mode": "auto",
    "network_route": "direct",
    "proxy_used": None,
    "proxy_fallback_reason": None,
}


class LiveCheckServiceTests(unittest.TestCase):
    def _run_live_check(self, account_id: int, email: str, *, liveness_result: dict) -> dict:
        # Mirror the enqueue contract: claim the live check and hold a queue
        # slot (_run_live_check releases it in its finally block).
        self.assertTrue(db.claim_account_live_check(acc_id=account_id, trigger="manual"))
        self.assertTrue(live_check_service._QUEUE_SLOTS.acquire(blocking=False))
        with (
            patch.object(live_check_service, "resolve_rotating_proxy", return_value=None),
            patch.object(live_check_service, "resolve_plan_check_route", return_value=dict(_ROUTE)),
            patch.object(live_check_service, "check_account_liveness", return_value=liveness_result),
            patch.object(live_check_service, "_append_log"),
        ):
            return live_check_service._run_live_check(
                account_id=account_id,
                email=email,
                proxy=None,
                trigger="manual",
            )

    def test_successful_live_check_reenqueues_failed_plan_check_with_fresh_token(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = db.insert_account(email="retry@example.com", access_token="stale-at")
            db.update_account_plan_check(
                acc_id=account_id,
                result={"ok": False, "error": "AT已过期/失效，请手动查活刷新"},
            )

            with patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": True, "status": "queued"},
            ) as enqueue:
                result = self._run_live_check(
                    account_id,
                    "retry@example.com",
                    liveness_result={
                        "ok": True,
                        "status": "live",
                        "checked_at": "2026-09-17T12:00:00",
                        "access_token": "fresh-at-token",
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual(db.get_account(account_id)["access_token"], "fresh-at-token")
            enqueue.assert_called_once()
            kwargs = enqueue.call_args.kwargs
            self.assertEqual(kwargs["account_id"], account_id)
            self.assertEqual(kwargs["email"], "retry@example.com")
            self.assertEqual(kwargs["access_token"], "fresh-at-token")
            self.assertEqual(kwargs["trigger"], "after_live_check")

    def test_successful_live_check_skips_plan_recheck_when_plan_already_success(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = db.insert_account(email="healthy@example.com", access_token="at")
            db.update_account_plan_check(
                acc_id=account_id,
                result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
            )

            with patch(
                "core.plan_check_service.enqueue_account_plan_check"
            ) as enqueue:
                result = self._run_live_check(
                    account_id,
                    "healthy@example.com",
                    liveness_result={
                        "ok": True,
                        "status": "live",
                        "checked_at": "2026-09-17T12:00:00",
                        "access_token": "fresh-at-token",
                    },
                )

            self.assertTrue(result["ok"])
            enqueue.assert_not_called()

    def test_failed_live_check_does_not_reenqueue_plan_check(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = db.insert_account(email="dead@example.com", access_token="at")
            db.update_account_plan_check(
                acc_id=account_id,
                result={"ok": False, "error": "AT已过期/失效，请手动查活刷新"},
            )

            with patch(
                "core.plan_check_service.enqueue_account_plan_check"
            ) as enqueue:
                result = self._run_live_check(
                    account_id,
                    "dead@example.com",
                    liveness_result={
                        "ok": False,
                        "status": "failed",
                        "checked_at": "2026-09-17T12:00:00",
                        "error": "login failed",
                    },
                )

            self.assertFalse(result["ok"])
            enqueue.assert_not_called()

    def test_rotating_lease_403_retires_proxy_and_retries_with_fresh_ip(self):
        failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403: "}
        success = {"ok": True, "status": "live", "access_token": "fresh-token"}
        slot = SimpleNamespace(released=False, release=lambda: setattr(slot, "released", True))
        with (
            patch.object(live_check_service, "_QUEUE_SLOTS", slot),
            patch.object(live_check_service, "resolve_rotating_proxy", side_effect=["http://blocked:1", "http://fresh:2"]) as resolve,
            patch.object(live_check_service, "release_rotating_proxy", return_value=True) as release,
            patch.object(live_check_service, "resolve_plan_check_route", return_value={
                "proxy": "http://blocked:1",
                "network_route": "proxy",
                "proxy_mode": "request",
            }),
            patch.object(live_check_service, "check_account_liveness", side_effect=[failed, success]) as check,
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "get_account", return_value={}),
            patch.object(live_check_service.db, "update_account_liveness"),
            patch.object(live_check_service, "_append_log"),
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        # 第一次用被 CF 拦截的出口，重试必须换成新租约的出口 IP。
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "http://blocked:1")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "http://fresh:2")
        # 失败租约必须被作废，且新租约按 force_refresh 解析；
        # 新租约随后在 finally 中正常释放（不带 retire）。
        self.assertEqual(
            release.call_args_list[0],
            unittest.mock.call(
                scope=live_check_service.LIVE_CHECK_PROXY_SCOPE,
                lane_id=None,
                proxy_url="http://blocked:1",
                retire=True,
            ),
        )
        self.assertEqual(
            release.call_args_list[1],
            unittest.mock.call(
                scope=live_check_service.LIVE_CHECK_PROXY_SCOPE,
                lane_id=None,
                proxy_url="http://fresh:2",
            ),
        )
        self.assertEqual(resolve.call_args_list[1].kwargs["force_refresh"], True)
        self.assertEqual(
            resolve.call_args_list[1].kwargs["exclude_proxy_url"],
            "http://blocked:1",
        )
        self.assertTrue(slot.released)

    def test_rotating_lease_403_keeps_original_failure_when_refresh_unavailable(self):
        failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403: "}
        direct_failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403: direct"}
        slot = SimpleNamespace(released=False, release=lambda: setattr(slot, "released", True))
        with (
            patch.object(live_check_service, "_QUEUE_SLOTS", slot),
            patch.object(live_check_service, "resolve_rotating_proxy", side_effect=["http://blocked:1", None]),
            patch.object(live_check_service, "release_rotating_proxy", return_value=True),
            patch.object(live_check_service, "resolve_plan_check_route", return_value={
                "proxy": "http://blocked:1",
                "network_route": "proxy",
                "proxy_mode": "request",
            }),
            patch.object(live_check_service, "check_account_liveness", side_effect=[failed, direct_failed]) as check,
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "get_account", return_value={}),
            patch.object(live_check_service.db, "update_account_liveness"),
            patch.object(live_check_service, "_append_log"),
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        # Lấy không được IP mới → giữ kết quả fail, nhưng vẫn phải thử直连 một lần.
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], direct_failed["error"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "http://blocked:1")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertTrue(slot.released)

    def test_browser_fallback_recovers_when_all_proxy_routes_are_blocked(self):
        failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403: "}
        browser_ok = {"ok": True, "access_token": "browser-token", "session": {"user": {"id": "u1"}}}
        slot = SimpleNamespace(released=False, release=lambda: setattr(slot, "released", True))
        with (
            patch.object(live_check_service, "_QUEUE_SLOTS", slot),
            patch.object(live_check_service, "resolve_rotating_proxy", side_effect=["http://blocked:1", "http://blocked:1"]),
            patch.object(live_check_service, "release_rotating_proxy", return_value=True),
            patch.object(live_check_service, "resolve_plan_check_route", return_value={
                "proxy": "http://blocked:1",
                "network_route": "proxy",
                "proxy_mode": "request",
            }),
            patch.object(live_check_service, "check_account_liveness", return_value=failed) as check,
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "get_account", return_value={}),
            patch.object(live_check_service.db, "update_account_liveness"),
            patch.object(live_check_service, "_append_log"),
            patch("core.live_check_browser.browser_refresh_session", return_value=browser_ok) as browser_refresh,
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        # 代理与直连都 403 后（代理→换IP→直连共 3 次协议尝试），Roxy 浏览器
        # 兜底必须接住并返回新 AT。
        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "browser-token")
        self.assertEqual(check.call_count, 3)
        self.assertEqual(check.call_args_list[2].kwargs["proxy"], "")
        browser_refresh.assert_called_once_with("user@example.com", email_source=None)
        self.assertTrue(slot.released)

    def test_browser_fallback_failure_keeps_original_result(self):
        failed = {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403: "}
        slot = SimpleNamespace(released=False, release=lambda: setattr(slot, "released", True))
        with (
            patch.object(live_check_service, "_QUEUE_SLOTS", slot),
            patch.object(live_check_service, "resolve_rotating_proxy", return_value="http://blocked:1"),
            patch.object(live_check_service, "release_rotating_proxy", return_value=True),
            patch.object(live_check_service, "resolve_plan_check_route", return_value={
                "proxy": "http://blocked:1",
                "network_route": "proxy",
                "proxy_mode": "request",
            }),
            patch.object(live_check_service, "check_account_liveness", return_value=failed),
            patch.object(live_check_service.db, "mark_account_live_check_running", return_value=True),
            patch.object(live_check_service.db, "get_account", return_value={}),
            patch.object(live_check_service.db, "update_account_liveness"),
            patch.object(live_check_service, "_append_log"),
            patch("core.live_check_browser.browser_refresh_session", side_effect=RuntimeError("roxy down")),
        ):
            result = live_check_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])
        self.assertTrue(slot.released)


if __name__ == "__main__":
    unittest.main()
