import unittest
from unittest.mock import patch

from config import proxy as proxy_config
from config.proxy import normalize_proxy_url
from core.roxybrowser_client import _proxy_url_to_roxy_info


class ProxyConfigTests(unittest.TestCase):
    def test_normalizes_host_port_username_password(self):
        self.assertEqual(
            normalize_proxy_url("14.224.198.226:57576:TqYFhp:GoAgKV"),
            "http://TqYFhp:GoAgKV@14.224.198.226:57576",
        )

    def test_quotes_credentials_and_preserves_colons_in_password(self):
        self.assertEqual(
            normalize_proxy_url("127.0.0.1:8080:user:p@ss:word"),
            "http://user:p%40ss%3Aword@127.0.0.1:8080",
        )

    def test_roxy_info_accepts_compact_proxy_format(self):
        info = _proxy_url_to_roxy_info("14.224.198.226:57576:TqYFhp:GoAgKV")
        self.assertEqual(info["protocol"], "HTTP")
        self.assertEqual(info["host"], "14.224.198.226")
        self.assertEqual(info["port"], "57576")
        self.assertEqual(info["proxyUserName"], "TqYFhp")
        self.assertEqual(info["proxyPassword"], "GoAgKV")

    def test_empty_proxy_stays_empty(self):
        self.assertEqual(normalize_proxy_url(""), "")

    def test_pick_proxy_skips_malformed_entries(self):
        with patch.object(
            proxy_config,
            "PROXY_POOL",
            ["bad-entry", "14.224.198.226:57576:TqYFhp:GoAgKV"],
        ):
            self.assertEqual(
                proxy_config.pick_proxy(),
                "http://TqYFhp:GoAgKV@14.224.198.226:57576",
            )

    @patch("config.proxy.requests.get")
    def test_pick_proxy_probe_skips_unreachable_proxy(self, get):
        get.side_effect = [
            proxy_config.requests.RequestException("CONNECT aborted"),
            type("Response", (), {"status_code": 403})(),
        ]
        with patch.object(
            proxy_config,
            "PROXY_POOL",
            [
                "14.224.198.226:57576:TqYFhp:GoAgKV",
                "14.224.198.227:57576:TqYFhp:GoAgKV",
            ],
        ), patch("config.proxy.random.sample", side_effect=lambda values, _size: values):
            selected = proxy_config.pick_proxy(
                probe_url="https://chatgpt.com/auth/login",
                max_probe=2,
            )

        self.assertTrue(selected.endswith("@14.224.198.227:57576"))
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
