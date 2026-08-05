# -*- coding: utf-8 -*-
"""Regression tests for Roxy profile lifecycle."""
import threading
import unittest
from unittest.mock import patch

from core.roxybrowser_client import RoxyBrowserClient
import core.roxybrowser_client as roxy_client


class RoxyProfileQuotaTests(unittest.TestCase):
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

        with patch.object(RoxyBrowserClient, "request", new=request):
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                RoxyBrowserClient().open_profile()

        self.assertEqual(calls, ["/browser/create", "/browser/open", "/browser/close", "/browser/delete"])


if __name__ == "__main__":
    unittest.main()
