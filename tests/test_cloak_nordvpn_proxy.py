"""Regression tests for NordVPN SOCKS5 attachment to Cloak registration."""
import unittest
from contextlib import contextmanager
from unittest import mock

import main


class MainCloakNordVPNProxyLifecycleTests(unittest.TestCase):
    def test_cloak_registration_uses_proxy_inside_nordvpn_context(self):
        events = []

        @contextmanager
        def proxy_context():
            events.append("proxy-start")
            try:
                yield "socks5://127.0.0.1:25000"
            finally:
                events.append("proxy-stop")

        def run_cloak(**kwargs):
            events.append(("registration", kwargs["proxy"]))
            return {"success": True}

        with mock.patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak"), \
             mock.patch(
                 "core.nordvpn_wireguard.proxy_for_registration",
                 side_effect=proxy_context,
             ), \
             mock.patch(
                 "core.cloakbrowser_registration.run_cloak_registration",
                 side_effect=run_cloak,
             ):
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


if __name__ == "__main__":
    unittest.main()
