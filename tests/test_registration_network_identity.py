import unittest
from unittest import mock

from core import registration_network_identity as identity


class NetworkIdentityTests(unittest.TestCase):
    def test_normalize_public_ip_rejects_private_address(self):
        with self.assertRaises(identity.NetworkIdentityError):
            identity.normalize_public_ip("127.0.0.1")

    def test_probe_socks_uses_proxy_and_fallback(self):
        failed = mock.MagicMock()
        failed.raise_for_status.side_effect = RuntimeError("down")
        succeeded = mock.MagicMock()
        succeeded.raise_for_status.return_value = None
        succeeded.json.return_value = {"ip": "8.8.8.8"}
        with mock.patch(
            "curl_cffi.requests.get", side_effect=[failed, succeeded]
        ) as request:
            result = identity.probe_socks_public_ip(
                "socks5://127.0.0.1:25000",
                endpoints=("https://one.test", "https://two.test"),
            )

        self.assertEqual(result, "8.8.8.8")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args.kwargs["proxies"]["https"],
            "socks5://127.0.0.1:25000",
        )

    def test_browser_tunnel_match_returns_observed_ip(self):
        with mock.patch.object(
            identity, "probe_browser_public_ip", return_value="8.8.8.8"
        ):
            self.assertEqual(
                identity.verify_browser_tunnel_identity(mock.Mock(), "8.8.8.8"),
                "8.8.8.8",
            )

    def test_browser_tunnel_mismatch_fails_closed(self):
        with mock.patch.object(
            identity, "probe_browser_public_ip", return_value="1.1.1.1"
        ), self.assertRaisesRegex(identity.NetworkIdentityError, "不匹配"):
            identity.verify_browser_tunnel_identity(mock.Mock(), "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
