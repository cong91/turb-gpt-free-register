"""Regression tests for NordVPN SOCKS5 attachment to Roxy profiles."""
import unittest
from contextlib import contextmanager
from unittest import mock

import main
from core import roxy_registration
from core.nordvpn_wireguard import RegistrationProxy, WireGuardProxy
from core.roxybrowser_client import RoxyBrowserClient


class RoxyProxyAttachmentTests(unittest.TestCase):
    def test_open_profile_attaches_proxy_before_open(self):
        calls = []

        def request(_client, method, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/browser/create":
                return {"id": "401"}
            if path == "/browser/detail":
                return {"data": {"rows": [{"proxyInfo": {
                    "protocol": "SOCKS5", "host": "127.0.0.1", "port": "25000"
                }}]}}
            if path == "/browser/open":
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        with mock.patch.object(RoxyBrowserClient, "request", new=request):
            opened = RoxyBrowserClient().open_profile(proxy="socks5://127.0.0.1:25000")

        self.assertEqual([path for path, _ in calls[:3]], [
            "/browser/create", "/browser/detail", "/browser/open"
        ])
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
            if path == "/browser/detail":
                return {"data": {"rows": [{"proxyInfo": {
                    "protocol": "SOCKS5", "host": "127.0.0.1", "port": "25001"
                }}]}}
            if path == "/browser/open":
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        with mock.patch("config.roxybrowser.ROXY_CREATE_USE_PROXY_POOL", True), \
             mock.patch("config.proxy.pick_proxy") as pick_proxy, \
             mock.patch.object(RoxyBrowserClient, "request", new=request):
            RoxyBrowserClient().open_profile(proxy="socks5://127.0.0.1:25001")

        pick_proxy.assert_not_called()
        self.assertEqual(payloads[0]["proxyInfo"]["port"], "25001")

    def test_nordvpn_lease_adds_chromium_proxy_enforcement_args(self):
        requests = []
        tunnel = WireGuardProxy(
            port=25000,
            process=mock.Mock(),
            conf_path="dynamic.conf",
            temp_conf="temp.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        proxy = RegistrationProxy(tunnel)

        def request(_client, method, path, **kwargs):
            requests.append((path, kwargs))
            if path == "/browser/create":
                return {"id": "405"}
            if path == "/browser/detail":
                return {"data": {"rows": [{"proxyInfo": {
                    "protocol": "SOCKS5", "host": "127.0.0.1", "port": "25000"
                }}]}}
            if path == "/browser/open":
                return {"debuggerAddress": "127.0.0.1:9222"}
            return {"ok": True}

        with mock.patch.object(RoxyBrowserClient, "request", new=request):
            RoxyBrowserClient().open_profile(proxy=proxy)

        open_call = next(kwargs for path, kwargs in requests if path == "/browser/open")
        args = open_call["json_body"]["args"]
        self.assertIn("--proxy-server=socks5://127.0.0.1:25000", args)
        self.assertIn("--proxy-bypass-list=<-loopback>", args)

    def test_profile_proxy_mismatch_fails_before_open(self):
        client = RoxyBrowserClient()
        with (
            mock.patch.object(client, "create_profile", return_value="404"),
            mock.patch.object(client, "_verify_profile_proxy", side_effect=RuntimeError("mismatch")),
            mock.patch.object(client, "request") as request,
            mock.patch.object(client, "cleanup_profile"),
            self.assertRaisesRegex(RuntimeError, "mismatch"),
        ):
            client.open_profile(proxy="socks5://127.0.0.1:25000")

        request.assert_not_called()

    def test_existing_profile_rejects_dynamic_proxy(self):
        client = RoxyBrowserClient()
        with mock.patch("config.roxybrowser.ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
             self.assertRaisesRegex(RuntimeError, "ROXY_PROFILE_ID"):
            client.open_profile(
                profile_id="existing-profile",
                proxy="socks5://127.0.0.1:25000",
            )

    def test_fresh_profile_ignores_configured_and_explicit_profile_ids(self):
        client = RoxyBrowserClient()
        with (
            mock.patch("config.roxybrowser.ROXY_PROFILE_ID", "saved-profile"),
            mock.patch.object(client, "create_profile", return_value="fresh-profile") as create,
            mock.patch.object(client, "request", return_value={"debuggerAddress": "127.0.0.1:9222"}),
        ):
            opened = client.open_profile(profile_id="another-saved-profile", fresh_profile=True)

        self.assertEqual(opened.profile_id, "fresh-profile")
        create.assert_called_once()

    def test_roxy_registration_verifies_tunnel_before_navigation(self):
        client = mock.MagicMock()
        client.open_profile.return_value = mock.Mock(
            profile_id="403", raw={}, debugger_address="127.0.0.1:9222"
        )
        driver = mock.MagicMock()
        tunnel = WireGuardProxy(
            port=25000,
            process=mock.Mock(pid=321),
            conf_path="dynamic.conf",
            temp_conf="temp.conf",
            proxy_url="socks5://127.0.0.1:25000",
            source_label="jp1.nordvpn.com",
            tunnel_egress_ip="8.8.8.8",
        )
        proxy = RegistrationProxy(tunnel)

        with mock.patch.object(roxy_registration, "RoxyBrowserClient", return_value=client), \
             mock.patch.object(roxy_registration, "_build_driver", return_value=driver), \
             mock.patch(
                 "core.registration_network_identity.verify_browser_tunnel_identity",
                 side_effect=RuntimeError("mismatch"),
             ):
            result = roxy_registration.run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="1990-01-01",
                proxy=proxy,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["network_identity"]["profile_id"], "403")
        self.assertFalse(result["network_identity"]["verified"])
        driver.get.assert_not_called()
        client.cleanup_profile.assert_called_once()

    def test_profile_proxy_rejection_returns_tunnel_identity(self):
        tunnel = WireGuardProxy(
            port=25000,
            process=mock.Mock(pid=321),
            conf_path="dynamic.conf",
            temp_conf="temp.conf",
            proxy_url="socks5://127.0.0.1:25000",
            source_label="jp1.nordvpn.com",
            tunnel_egress_ip="8.8.8.8",
        )
        client = mock.MagicMock()
        client.open_profile.side_effect = RuntimeError("stored proxy mismatch")
        with mock.patch.object(roxy_registration, "RoxyBrowserClient", return_value=client):
            result = roxy_registration.run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="1990-01-01",
                proxy=RegistrationProxy(tunnel),
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["network_identity"]["local_port"], 25000)
        self.assertFalse(result["network_identity"]["verified"])
        client.cleanup_profile.assert_not_called()

    def test_roxy_registration_forwards_proxy_to_profile_open(self):
        client = mock.MagicMock()
        client.open_profile.side_effect = RuntimeError("stop after open")
        with mock.patch.object(roxy_registration, "RoxyBrowserClient", return_value=client):
            result = roxy_registration.run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="1990-01-01",
                proxy="socks5://127.0.0.1:25000",
            )

        self.assertFalse(result["success"])
        self.assertIn("stop after open", result["error"])
        client.open_profile.assert_called_once_with(
            proxy="socks5://127.0.0.1:25000",
            stop_check=roxy_registration._check_manual_stop,
        )


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
             mock.patch("config.proxy.ROTATING_PROXY_ENABLED", False), \
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
