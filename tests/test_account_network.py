import unittest
from contextlib import contextmanager
from unittest.mock import patch


class AccountNetworkSelectionTests(unittest.TestCase):
    def test_required_proxy_ignores_registration_proxy_and_uses_rotating_lease(self):
        from core.account_network import required_account_proxy

        with (
            patch(
                "core.account_network.resolve_rotating_proxy",
                return_value="http://rotating.example:8080",
            ) as resolve_proxy,
            patch("core.account_network.release_rotating_proxy") as release_proxy,
            required_account_proxy(
                "http://nord-registration.example:1080",
                rotating_scope="plan_check",
                lane_id=3,
            ) as route,
        ):
            self.assertEqual(route, ("http://rotating.example:8080", "rotating_proxy"))

        resolve_proxy.assert_called_once_with(
            None,
            scope="plan_check",
            lane_id=3,
        )
        release_proxy.assert_called_once_with(
            scope="plan_check",
            lane_id=3,
            proxy_url="http://rotating.example:8080",
        )

    def test_required_proxy_falls_back_to_pool_when_rotating_lease_missing(self):
        from core.account_network import required_account_proxy

        with (
            patch("core.account_network.resolve_rotating_proxy", return_value=None),
            patch(
                "config.proxy.pick_proxy",
                return_value="socks5://pool.example:1080",
            ) as pick_proxy,
            required_account_proxy(
                "http://nord-registration.example:1080",
                rotating_scope="plan_check",
            ) as route,
        ):
            self.assertEqual(route, ("socks5://pool.example:1080", "proxy_pool"))

        pick_proxy.assert_called_once_with(
            probe_url="https://chatgpt.com/auth/login",
            probe_timeout=4.0,
        )

    def test_proxy_pool_mode_uses_configured_pool(self):
        from core.account_network import selected_account_proxy

        with (
            patch("config.proxy.pick_proxy", return_value="socks5://proxy.example:1080") as pick_proxy,
            selected_account_proxy("proxy_pool", rotating_scope="plan_import_login") as route,
        ):
            self.assertEqual(route, ("socks5://proxy.example:1080", "proxy_pool"))

        pick_proxy.assert_called_once_with(
            probe_url="https://chatgpt.com/auth/login",
            probe_timeout=4.0,
        )

    def test_nord_wire_mode_requires_and_returns_wireguard_proxy(self):
        from core.account_network import selected_account_proxy

        @contextmanager
        def wire_proxy(**_kwargs):
            yield "socks5://127.0.0.1:25001"

        with (
            patch("core.nordvpn_wireguard.proxy_for_registration", wire_proxy),
            selected_account_proxy("nord_wire", rotating_scope="plan_import_login") as route,
        ):
            self.assertEqual(route, ("socks5://127.0.0.1:25001", "nord_wire"))

    def test_rotating_proxy_mode_releases_its_lease(self):
        from core.account_network import selected_account_proxy

        with (
            patch("core.account_network.resolve_rotating_proxy", return_value="http://proxy.example:8080"),
            patch("core.account_network.release_rotating_proxy") as release,
            selected_account_proxy("rotating_proxy", rotating_scope="plan_import_login", lane_id=4) as route,
        ):
            self.assertEqual(route, ("http://proxy.example:8080", "rotating_proxy"))

        release.assert_called_once_with(
            scope="plan_import_login",
            lane_id=4,
            proxy_url="http://proxy.example:8080",
        )


if __name__ == "__main__":
    unittest.main()
