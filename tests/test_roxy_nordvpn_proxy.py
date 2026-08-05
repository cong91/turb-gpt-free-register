# -*- coding: utf-8 -*-
"""Regression tests for NordVPN SOCKS5 attachment to Roxy profiles."""
import unittest
from contextlib import contextmanager
from unittest import mock

import main
from core import roxy_registration
from core.roxybrowser_client import RoxyBrowserClient


class RoxyProxyAttachmentTests(unittest.TestCase):
    def test_open_profile_attaches_proxy_before_open(self):
        calls = []

        def request(_client, method, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/browser/create":
                return {"id": "401"}
            if path == "/browser/open":
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        with mock.patch.object(RoxyBrowserClient, "request", new=request):
            opened = RoxyBrowserClient().open_profile(proxy="socks5://127.0.0.1:25000")

        self.assertEqual([path for path, _ in calls[:2]], ["/browser/create", "/browser/open"])
        create_body = calls[0][1]["json_body"]
        proxy_info = create_body["proxyInfo"]
        self.assertEqual(proxy_info["proxyMethod"], "custom")
        self.assertEqual(proxy_info["proxyCategory"], "SOCKS5")
        self.assertEqual(proxy_info["protocol"], "SOCKS5")
        self.assertEqual(proxy_info["host"], "127.0.0.1")
        self.assertEqual(proxy_info["port"], "25000")
        self.assertEqual(opened.profile_id, "401")

    def test_explicit_proxy_wins_over_configured_proxy_pool(self):
        payloads = []

        def request(_client, method, path, **kwargs):
            if path == "/browser/create":
                payloads.append(kwargs["json_body"])
                return {"id": "402"}
            if path == "/browser/open":
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        with mock.patch("config.roxybrowser.ROXY_CREATE_USE_PROXY_POOL", True), \
             mock.patch("config.proxy.pick_proxy") as pick_proxy, \
             mock.patch.object(RoxyBrowserClient, "request", new=request):
            RoxyBrowserClient().open_profile(proxy="socks5://127.0.0.1:25001")

        pick_proxy.assert_not_called()
        self.assertEqual(payloads[0]["proxyInfo"]["port"], "25001")

    def test_existing_profile_rejects_dynamic_proxy(self):
        client = RoxyBrowserClient()
        with mock.patch("config.roxybrowser.ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
             self.assertRaisesRegex(RuntimeError, "ROXY_PROFILE_ID"):
            client.open_profile(
                profile_id="existing-profile",
                proxy="socks5://127.0.0.1:25000",
            )

    def test_roxy_registration_forwards_proxy_to_profile_open(self):
        client = mock.MagicMock()
        client.open_profile.side_effect = RuntimeError("stop after open")
        with mock.patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), \
             self.assertRaisesRegex(RuntimeError, "stop after open"):
            roxy_registration.run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="1990-01-01",
                proxy="socks5://127.0.0.1:25000",
            )

        client.open_profile.assert_called_once_with(proxy="socks5://127.0.0.1:25000")


class MainNordVPNProxyLifecycleTests(unittest.TestCase):
    def test_roxy_registration_uses_proxy_inside_context(self):
        events = []

        @contextmanager
        def proxy_context():
            events.append("proxy-start")
            try:
                yield "socks5://127.0.0.1:25000"
            finally:
                events.append("proxy-stop")

        def run_roxy(**kwargs):
            events.append(("registration", kwargs["proxy"]))
            return {"success": True}

        with mock.patch("config.roxybrowser.REGISTRATION_DRIVER", "roxy"), \
             mock.patch("core.nordvpn_wireguard.proxy_for_registration", side_effect=proxy_context), \
             mock.patch("core.roxy_registration.run_roxy_registration", side_effect=run_roxy):
            result = main.run_registration("user@example.com", "Test User", "1990-01-01")

        self.assertTrue(result["success"])
        self.assertEqual(
            events,
            [
                "proxy-start",
                ("registration", "socks5://127.0.0.1:25000"),
                "proxy-stop",
            ],
        )

    def test_explicit_proxy_skips_nordvpn_context(self):
        with mock.patch("config.roxybrowser.REGISTRATION_DRIVER", "roxy"), \
             mock.patch("core.nordvpn_wireguard.proxy_for_registration") as nordvpn_context, \
             mock.patch(
                 "core.roxy_registration.run_roxy_registration",
                 return_value={"success": True},
             ) as run_roxy:
            result = main.run_registration(
                "user@example.com",
                "Test User",
                "1990-01-01",
                proxy="http://127.0.0.1:8080",
            )

        self.assertTrue(result["success"])
        nordvpn_context.assert_not_called()
        self.assertEqual(run_roxy.call_args.kwargs["proxy"], "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
