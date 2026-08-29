# -*- coding: utf-8 -*-
"""Unit tests for NordVPN access-token to NordLynx configuration flow."""
import base64
import unittest
from unittest import mock

import requests

from core import nordvpn_account as account

_PRIVATE_KEY = base64.b64encode(b"p" * 32).decode("ascii")
_PUBLIC_KEY = base64.b64encode(b"q" * 32).decode("ascii")


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _server(hostname="jp749.nordvpn.com", station="93.118.43.3", load=5):
    return {
        "hostname": hostname,
        "station": station,
        "status": "online",
        "load": load,
        "locations": [{"country": {"code": "JP"}}],
        "technologies": [
            {
                "id": 35,
                "pivot": {"status": "online"},
                "metadata": [{"name": "public_key", "value": _PUBLIC_KEY}],
            }
        ],
    }


class NordVPNAccountClientTests(unittest.TestCase):
    def test_private_key_uses_bearer_and_caches(self):
        http = _Http([_Response({"nordlynx_private_key": _PRIVATE_KEY})])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        self.assertEqual(client.private_key(), _PRIVATE_KEY)
        self.assertEqual(client.private_key(), _PRIVATE_KEY)
        self.assertEqual(len(http.calls), 1)
        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/v1/users/services/credentials"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token:token-secret")

    def test_missing_token_fails_before_request(self):
        http = _Http([])
        client = account.NordVPNAccountClient(access_token="", http=http)

        with self.assertRaisesRegex(account.NordVPNAccountError, "ACCESS_TOKEN"):
            client.private_key()
        self.assertEqual(http.calls, [])

    def test_unauthorized_does_not_expose_token(self):
        http = _Http([_Response({}, status_code=401, text="bad token")])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        with self.assertRaises(account.NordVPNAccountError) as captured:
            client.private_key()
        self.assertNotIn("token-secret", str(captured.exception))

    def test_invalid_private_key_is_rejected(self):
        http = _Http([_Response({"nordlynx_private_key": "not-a-key"})])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        with self.assertRaisesRegex(account.NordVPNAccountError, "nordlynx_private_key"):
            client.private_key()

    def test_country_filter_maps_code_to_id_without_token_header(self):
        http = _Http([
            _Response([{"id": 108, "code": "JP"}]),
            _Response([_server()]),
        ])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        servers = client.servers("jp")

        self.assertEqual(servers[0].hostname, "jp749.nordvpn.com")
        self.assertEqual(http.calls[1][1]["params"]["filters[country_id]"], 108)
        self.assertNotIn("Authorization", http.calls[0][1]["headers"])
        self.assertNotIn("Authorization", http.calls[1][1]["headers"])

    def test_unknown_country_is_rejected_before_recommendation(self):
        http = _Http([_Response([{"id": 108, "code": "JP"}])])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        with self.assertRaisesRegex(account.NordVPNAccountError, "ZZ"):
            client.servers("zz")
        self.assertEqual(len(http.calls), 1)

    def test_invalid_servers_are_filtered(self):
        invalid = _server(station="not-an-ip")
        http = _Http([_Response([invalid])])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        with self.assertRaisesRegex(account.NordVPNAccountError, "online"):
            client.servers()

    def test_build_config_contains_nordlynx_fields(self):
        http = _Http([
            _Response({"nordlynx_private_key": _PRIVATE_KEY}),
            _Response([_server()]),
        ])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        config = client.build_config()

        self.assertEqual(config.server.hostname, "jp749.nordvpn.com")
        self.assertIn("Address = 10.5.0.2/16", config.text)
        self.assertIn(f"PrivateKey = {_PRIVATE_KEY}", config.text)
        self.assertIn(f"PublicKey = {_PUBLIC_KEY}", config.text)
        self.assertIn("Endpoint = 93.118.43.3:51820", config.text)
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", config.text)

    def test_choose_server_avoids_recent_hostname(self):
        client = account.NordVPNAccountClient(access_token="token-secret", http=_Http([]))
        servers = [
            account.NordLynxServer("jp1", "1.1.1.1", _PUBLIC_KEY, "JP", 1),
            account.NordLynxServer("jp2", "1.1.1.2", _PUBLIC_KEY, "JP", 2),
        ]
        with mock.patch.object(client, "servers", return_value=servers), \
             mock.patch.object(account.random, "choice", side_effect=lambda items: items[0]):
            first = client.choose_server("JP")
            second = client.choose_server("JP")

        self.assertEqual(first.hostname, "jp1")
        self.assertEqual(second.hostname, "jp2")

    def test_choose_server_excludes_persisted_active_hostnames(self):
        client = account.NordVPNAccountClient(access_token="token-secret", http=_Http([]))
        servers = [
            account.NordLynxServer("jp1", "1.1.1.1", _PUBLIC_KEY, "JP", 1),
            account.NordLynxServer("jp2", "1.1.1.2", _PUBLIC_KEY, "JP", 2),
        ]
        with mock.patch.object(client, "servers", return_value=servers), \
             mock.patch.object(account.random, "choice", side_effect=lambda items: items[0]):
            selected = client.choose_server("JP", excluded_hostnames={"jp1"})

        self.assertEqual(selected.hostname, "jp2")

    def test_request_exception_is_translated(self):
        http = _Http([requests.RequestException("network down")])
        client = account.NordVPNAccountClient(access_token="token-secret", http=http)

        with self.assertRaisesRegex(account.NordVPNAccountError, "network down"):
            client.private_key()


if __name__ == "__main__":
    unittest.main()
