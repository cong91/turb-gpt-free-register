"""Regression tests for Roxy profile lifecycle."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.roxybrowser_client import RoxyBrowserClient


class RoxyProfileQuotaTests(unittest.TestCase):
    def test_profile_create_requests_are_serialized(self):
        start = threading.Barrier(2)
        state_lock = threading.Lock()
        state = {"active": 0, "max_active": 0, "created": 0}

        def request(_client, _method, _path, **_kwargs):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["created"] += 1
                profile_id = str(state["created"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
            return {"id": profile_id}

        def create():
            start.wait(timeout=2)
            RoxyBrowserClient().create_profile()

        with patch.object(RoxyBrowserClient, "request", new=request):
            threads = [threading.Thread(target=create) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(state["created"], 2)
        self.assertEqual(state["max_active"], 1)

    def test_create_waits_through_roxy_busy_and_quota_responses(self):
        client = RoxyBrowserClient()
        stop_check = Mock()
        responses = [
            RuntimeError("Roxy API 返回失败 POST /browser/create: Creating, please wait!"),
            RuntimeError("Roxy API 返回失败 POST /browser/create: Insufficient profile quota"),
            {"id": "ready-profile"},
        ]

        with (
            patch.object(client, "request", side_effect=responses) as request,
            patch("core.roxybrowser_client.time.sleep") as sleep,
        ):
            profile_id = client.create_profile(stop_check=stop_check)

        self.assertEqual(profile_id, "ready-profile")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertGreaterEqual(stop_check.call_count, 3)

    def test_create_does_not_retry_ambiguous_transport_failure(self):
        client = RoxyBrowserClient()

        with (
            patch.object(
                client,
                "request",
                side_effect=RuntimeError("Roxy API request timed out"),
            ) as request,
            patch("core.roxybrowser_client.time.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "timed out"),
        ):
            client.create_profile()

        request.assert_called_once()
        sleep.assert_not_called()

    def test_open_profile_forwards_stop_check_while_waiting_to_create(self):
        client = RoxyBrowserClient()
        stop_check = Mock()

        with (
            patch.object(client, "create_profile", return_value="profile-1") as create,
            patch.object(
                 client,
                 "request",
                 return_value={"debuggerAddress": "127.0.0.1:9222"},
            ),
        ):
            client.open_profile(stop_check=stop_check)

        create.assert_called_once_with(None, stop_check=stop_check)

    def test_workers_can_open_profiles_concurrently(self):
        """Roxy profile concurrency is controlled by the registration worker pool."""
        opened = threading.Barrier(2)
        calls = []
        calls_lock = threading.Lock()

        def request(_client, method, path, **kwargs):
            with calls_lock:
                calls.append(path)
            if path == "/browser/create":
                return {"id": str(100 + calls.count(path))}
            if path == "/browser/open":
                opened.wait(timeout=2)
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        results = []
        with patch.object(RoxyBrowserClient, "request", new=request):
            threads = [
                threading.Thread(target=lambda: results.append(RoxyBrowserClient().open_profile()))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(len(results), 2)
            self.assertEqual(calls.count("/browser/create"), 2)
            for result in results:
                RoxyBrowserClient().cleanup_profile(result)

        self.assertEqual(calls.count("/browser/delete"), 2)

    def test_open_failure_deletes_profile_created_for_this_run(self):
        """If opening fails after create, the newly created profile is cleaned up."""
        calls = []

        def request(_client, method, path, **kwargs):
            calls.append(path)
            if path == "/browser/create":
                return {"id": "303"}
            if path == "/browser/open":
                raise RuntimeError("open failed")
            return {"ok": True}

        with patch.object(RoxyBrowserClient, "request", new=request):  # noqa: SIM117
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                RoxyBrowserClient().open_profile()

        self.assertEqual(calls, ["/browser/create", "/browser/open", "/browser/close", "/browser/delete"])


if __name__ == "__main__":
    unittest.main()
