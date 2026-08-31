import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask

from config import browser_use as browser_use_config
from config import proxy as proxy_config
from core.rotating_proxy_client import RotatingProxyApiError, RotatingProxyClient
from core.rotating_proxy_manager import RotatingProxyManager
from core.rotating_proxy_store import RotatingProxyStore
from webui.config_editor import EDITABLE_FIELDS
from webui.rotating_proxy_api import register_rotating_proxy_routes


class _FakeResponse:
    def __init__(self, payload=None, text="", status_code=200):
        self.payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self.payload is None:
            raise ValueError("not a single JSON document")
        return self.payload

    def raise_for_status(self):
        return None


class _FakeRotatingProxyClient:
    def __init__(self, keys):
        self.keys = list(keys)
        self.get_calls = []
        self.purchase_calls = 0
        self.renew_calls = []

    def list_keys(self):
        return list(self.keys)

    def purchase_key(self):
        self.purchase_calls += 1
        key = {"key": f"purchased-{self.purchase_calls}", "expires_at": None}
        self.keys.append(key)
        return key

    def purchase_keys(self, quantity):
        self.purchase_calls += 1
        purchased = []
        for index in range(int(quantity)):
            key = {"key": f"purchased-{self.purchase_calls}-{index + 1}", "expires_at": None}
            self.keys.append(key)
            purchased.append(key)
        return purchased

    def renew_key(self, key):
        self.renew_calls.append(key)
        renewed = {"key": key, "expires_at": 1000.0}
        self.keys = [item for item in self.keys if item["key"] != key]
        self.keys.append(renewed)
        return renewed

    def get_proxy(self, key):
        self.get_calls.append(key)
        return {
            "proxy_url": f"http://198.51.100.{len(self.get_calls)}:8080",
            "ttl_seconds": 60,
        }


class _HealthAwareRotatingProxyClient(_FakeRotatingProxyClient):
    def __init__(self, keys, health_results):
        super().__init__(keys)
        self.health_results = list(health_results)
        self.health_calls = []

    def check_proxy(self, proxy_url):
        self.health_calls.append(proxy_url)
        return self.health_results.pop(0)


class _CooldownRotatingProxyClient(_FakeRotatingProxyClient):
    def check_proxy(self, proxy_url):
        return True

    def get_proxy(self, key):
        self.get_calls.append(key)
        raise RotatingProxyApiError(
            "proxy.vn API status=101: Con 23s moi co the doi proxy"
        )


class RotatingProxyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.now = [100.0]
        self.store = RotatingProxyStore(Path(self.temp_dir.name) / "rotating.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _manager(self, client):
        return RotatingProxyManager(
            client=client,
            store=self.store,
            clock=lambda: self.now[0],
        )

    def test_reuses_unexpired_proxy_for_same_lane(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        second = manager.acquire(0)

        self.assertEqual(first.proxy_url, second.proxy_url)
        self.assertEqual(client.get_calls, ["key-1"])

    def test_workflow_cleanup_retains_rotating_lease_until_proxy_expiry(self):
        from core.rotating_proxy_runtime import release_rotating_proxy

        with patch("core.rotating_proxy_manager.get_rotating_proxy_manager") as get_manager:
            result = release_rotating_proxy(
                scope="registration",
                lane_id=0,
                proxy_url="http://198.51.100.1:8080",
            )

        self.assertFalse(result)
        get_manager.return_value.release.assert_not_called()

    def test_health_checks_cached_proxy_before_reusing_it(self):
        client = _HealthAwareRotatingProxyClient(
            [{"key": "key-1", "expires_at": 1000.0}],
            [True, True],
        )
        manager = self._manager(client)

        first = manager.acquire(0)
        second = manager.acquire(0)

        self.assertEqual(first.proxy_url, second.proxy_url)
        self.assertEqual(client.get_calls, ["key-1"])
        self.assertEqual(client.health_calls, [first.proxy_url, first.proxy_url])

    def test_dead_cached_proxy_rotates_before_next_workflow_uses_it(self):
        client = _HealthAwareRotatingProxyClient(
            [{"key": "key-1", "expires_at": 1000.0}],
            [True, False, True],
        )
        manager = self._manager(client)

        first = manager.acquire(0)
        second = manager.acquire(0)

        self.assertNotEqual(first.proxy_url, second.proxy_url)
        self.assertEqual(client.get_calls, ["key-1", "key-1"])
        self.assertEqual(client.health_calls, [first.proxy_url, first.proxy_url, second.proxy_url])

    def test_repeated_health_failures_exclude_key_and_try_another_key(self):
        client = _HealthAwareRotatingProxyClient(
            [
                {"key": "key-1", "expires_at": 1000.0},
                {"key": "key-2", "expires_at": 1000.0},
            ],
            [False, False, True],
        )
        manager = self._manager(client)

        lease = manager.acquire(0)

        self.assertEqual(lease.key, "key-2")
        self.assertEqual(client.get_calls, ["key-1", "key-1", "key-2"])

    def test_provider_cooldown_reuses_a_live_previous_proxy(self):
        client = _CooldownRotatingProxyClient(
            [{"key": "key-1", "expires_at": 1000.0}]
        )
        manager = self._manager(client)
        self.store.upsert_lease(
            0,
            rotating_key="key-1",
            proxy_url="http://198.51.100.77:8080",
            proxy_expires_at=90.0,
            key_expires_at=1000.0,
            assigned_at=50.0,
        )

        first = manager.acquire(0)
        self.assertEqual(first.proxy_url, "http://198.51.100.77:8080")
        self.assertEqual(client.get_calls, ["key-1"])
        self.now[0] = 110.0
        second = manager.acquire(0)
        self.assertEqual(second.proxy_url, first.proxy_url)
        self.assertEqual(client.get_calls, ["key-1"])

    def test_status_removes_expired_leases_from_inventory(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)
        self.store.upsert_lease(
            0,
            rotating_key="key-1",
            proxy_url="http://198.51.100.77:8080",
            proxy_expires_at=90.0,
            key_expires_at=1000.0,
        )

        self.assertEqual(manager.status()["leases"], [])

    def test_new_default_manager_clears_leases_from_previous_process(self):
        import core.rotating_proxy_manager as manager_module

        self.store.upsert_lease(
            0,
            rotating_key="key-1",
            proxy_url="http://198.51.100.77:8080",
            proxy_expires_at=1000.0,
            key_expires_at=2000.0,
        )
        with patch.object(manager_module, "_DEFAULT_MANAGER", None), patch.object(
            manager_module, "RotatingProxyStore", return_value=self.store
        ):
            manager = manager_module.get_rotating_proxy_manager()

            self.assertEqual(manager.store.list_leases(), [])
            self.store.upsert_lease(
                1,
                rotating_key="key-2",
                proxy_url="http://198.51.100.78:8080",
                proxy_expires_at=1000.0,
                key_expires_at=2000.0,
            )
            self.assertIs(manager_module.get_rotating_proxy_manager(), manager)
            self.assertEqual(len(manager.store.list_leases()), 1)

    def test_assigns_a_new_key_when_another_lane_owns_the_only_key(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        second = manager.acquire(1)

        self.assertEqual(first.key, "key-1")
        self.assertEqual(second.key, "purchased-1")
        self.assertEqual(client.purchase_calls, 1)

    def test_same_lane_number_is_isolated_between_workflow_scopes(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        registration = manager.acquire(0, scope="registration")
        codex_retry = manager.acquire(0, scope="codex_retry")

        self.assertEqual(registration.key, "key-1")
        self.assertEqual(codex_retry.key, "purchased-1")
        self.assertEqual(client.purchase_calls, 1)
        self.assertEqual(
            {(item["scope"], item["lane_id"]) for item in manager.status()["leases"]},
            {("registration", 0), ("codex_retry", 0)},
        )

    def test_releases_completed_lane_for_another_workflow(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        registration = manager.acquire(0, scope="registration")
        manager.release(0, scope="registration")
        codex_retry = manager.acquire(0, scope="codex_retry")

        self.assertEqual(registration.key, "key-1")
        self.assertEqual(codex_retry.key, "key-1")
        self.assertEqual(client.purchase_calls, 0)
        self.assertEqual(manager.status()["leases"], [{
            "scope": "codex_retry",
            "lane_id": 0,
            "key": "ke...-1",
            "proxy": "http://198.51.100.2:8080",
            "proxy_expires_at": 160.0,
        }])

    def test_expired_proxy_lease_does_not_block_key_reuse(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        manager.acquire(0, scope="registration")
        self.now[0] = 161.0
        codex_retry = manager.acquire(0, scope="codex_retry")

        self.assertEqual(codex_retry.key, "key-1")
        self.assertEqual(client.purchase_calls, 0)

    def test_stale_release_does_not_remove_replacement_lease(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        self.now[0] = 161.0
        replacement = manager.acquire(0)
        manager.release(0, proxy_url=first.proxy_url)

        current = self.store.get_lease(0)
        self.assertEqual(replacement.proxy_url, "http://198.51.100.2:8080")
        self.assertIsNotNone(current)
        self.assertEqual(current["proxy_url"], replacement.proxy_url)

    def test_claim_race_retries_with_a_different_key(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        with patch.object(self.store, "try_upsert_lease", side_effect=[False, True]):
            lease = manager.acquire(0, scope="registration")

        self.assertEqual(lease.key, "purchased-1")
        self.assertEqual(client.get_calls, ["key-1", "purchased-1"])

    def test_purchased_key_gets_daily_expiry_when_provider_omits_expiry(self):
        client = _FakeRotatingProxyClient([])
        manager = self._manager(client)

        lease = manager.acquire(0)

        self.assertEqual(lease.key_expires_at, 100.0 + 24 * 60 * 60)
        self.assertEqual(
            manager.status()["keys"][0]["expires_at"],
            100.0 + 24 * 60 * 60,
        )

    def test_expired_key_inventory_falls_through_to_daily_purchase(self):
        http = Mock()
        http.get.side_effect = [
            _FakeResponse({"status": 101, "comen": "expired"}),
            _FakeResponse({"status": 100, "keyxoay": "purchased-key"}),
            _FakeResponse({
                "status": 100,
                "message": "proxy nay se die sau 60s",
                "proxyhttp": "203.0.113.30:8080",
            }),
            _FakeResponse({}, status_code=204),
        ]
        client = RotatingProxyClient(http)
        manager = self._manager(client)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            lease = manager.acquire(0)

        self.assertEqual(lease.key, "purchased-key")
        self.assertEqual(http.get.call_count, 4)

    def test_ensure_key_inventory_buys_missing_keys_in_one_batch(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        inventory = manager.ensure_key_inventory(3, scope="registration")

        self.assertEqual(len(inventory), 3)
        self.assertEqual(client.purchase_calls, 1)
        self.assertEqual(
            [item["rotating_key"] for item in inventory],
            ["key-1", "purchased-1-1", "purchased-1-2"],
        )

    def test_ensure_key_inventory_excludes_keys_claimed_by_another_scope(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)
        manager.acquire(0, scope="codex_retry")

        inventory = manager.ensure_key_inventory(2, scope="registration")

        self.assertEqual(client.purchase_calls, 1)
        self.assertEqual(len(inventory), 2)
        self.assertNotIn("key-1", [item["rotating_key"] for item in inventory])

    def test_expired_inventory_buys_as_many_keys_as_requested_lanes(self):
        http = Mock()
        http.get.side_effect = [
            _FakeResponse({"status": 101, "comen": "expired"}),
            _FakeResponse(
                text=(
                    '{"status":100,"keyxoay":"key-a"}'
                    '{"status":100,"keyxoay":"key-b"}'
                    '{"status":100,"keyxoay":"key-c"}'
                    '{"status":100,"comen":"successful transaction 3 key xoay","soluong":3}'
                )
            ),
        ]
        manager = self._manager(RotatingProxyClient(http))

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            inventory = manager.ensure_key_inventory(3, scope="registration")

        self.assertEqual([item["rotating_key"] for item in inventory], ["key-a", "key-b", "key-c"])
        self.assertEqual(
            http.get.call_args_list[1].args[0],
            "https://proxy.vn/proxyxoay/apimuangay.php?"
            "key=configured-key&&thoigian=1&&soluong=3",
        )

    def test_expired_inventory_invalidates_stale_local_keys_before_acquire(self):
        client = _FakeRotatingProxyClient([])
        manager = self._manager(client)
        self.store.upsert_key("stale-key", None)

        manager.ensure_key_inventory(1, scope="registration")
        lease = manager.acquire(0, scope="registration")

        self.assertEqual(lease.key, "purchased-1")

    def test_refresh_expires_local_keys_missing_from_provider_inventory(self):
        client = _FakeRotatingProxyClient([{"key": "active-key", "expires_at": 1000.0}])
        manager = self._manager(client)
        self.store.upsert_key("stale-key", None)

        manager.refresh_keys()
        lease = manager.acquire(0, scope="registration")

        self.assertEqual(lease.key, "active-key")

    def test_fetches_a_new_proxy_after_ttl_expiration(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        self.now[0] = 161.0
        second = manager.acquire(0)

        self.assertNotEqual(first.proxy_url, second.proxy_url)
        self.assertEqual(client.get_calls, ["key-1", "key-1"])

    def test_creates_a_new_key_when_existing_key_expires(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 160.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        self.now[0] = 161.0
        second = manager.acquire(0)

        self.assertEqual(first.key, "key-1")
        self.assertEqual(second.key, "purchased-1")
        self.assertEqual(client.renew_calls, [])
        self.assertEqual(client.purchase_calls, 1)
        self.assertEqual(client.get_calls, ["key-1", "purchased-1"])

    def test_status_masks_secret_material_and_reports_lane_assignments(self):
        client = _FakeRotatingProxyClient([{"key": "key-123456", "expires_at": 1000.0}])
        manager = self._manager(client)
        manager.acquire(0)

        status = manager.status()

        self.assertEqual(status["keys"][0]["key"], "key-1...3456")
        self.assertEqual(status["keys"][0]["lanes"], [0])
        self.assertEqual(status["leases"][0]["proxy"], "http://198.51.100.1:8080")


class RotatingProxyClientTests(unittest.TestCase):
    def test_get_proxy_parses_provider_proxy_and_ttl(self):
        http = Mock()
        http.get.return_value = _FakeResponse({
            "status": 100,
            "message": "proxy nay se die sau 1777s",
            "proxyhttp": "203.0.113.10:10836::",
        })
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            result = client.get_proxy("rotating-key")

        self.assertEqual(result["proxy_url"], "http://203.0.113.10:10836")
        self.assertEqual(result["ttl_seconds"], 1777)
        self.assertEqual(http.get.call_args.kwargs["params"]["key"], "rotating-key")

    def test_get_proxy_preserves_socks5_for_compact_credentials(self):
        http = Mock()
        http.get.return_value = _FakeResponse({
            "status": 100,
            "message": "proxy nay se die sau 60s",
            "proxysocks5": "203.0.113.10:30836:user name:p@ss",
        })
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"), patch.object(
            proxy_config, "ROTATING_PROXY_PROTOCOL", "socks5"
        ):
            result = client.get_proxy("rotating-key")

        self.assertEqual(
            result["proxy_url"],
            "socks5://user%20name:p%40ss@203.0.113.10:30836",
        )

    def test_check_proxy_probes_without_requesting_a_new_ip(self):
        http = Mock()
        http.get.return_value = _FakeResponse({}, status_code=403)
        client = RotatingProxyClient(http)

        self.assertTrue(client.check_proxy("http://203.0.113.10:8080"))
        http.get.assert_called_once_with(
            "https://chatgpt.com/",
            proxies={
                "http": "http://203.0.113.10:8080",
                "https": "http://203.0.113.10:8080",
            },
            timeout=5.0,
            allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        http.get.return_value = _FakeResponse({}, status_code=407)
        self.assertFalse(client.check_proxy("http://203.0.113.10:8080"))

    def test_list_keys_accepts_concatenated_json_documents(self):
        http = Mock()
        http.get.return_value = _FakeResponse(
            text='{"status":100,"keyxoay":"key-a","expired":"21:43 29-03-25"}'
                 '{"status":100,"keyxoay":"key-b","expired":"14:20 05-03-25"}'
        )
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            keys = client.list_keys()

        self.assertEqual([item["key"] for item in keys], ["key-a", "key-b"])
        self.assertIsInstance(keys[0]["expires_at"], float)

    def test_list_keys_treats_expired_inventory_as_empty(self):
        http = Mock()
        http.get.return_value = _FakeResponse({"status": 101, "comen": "expired"})
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            keys = client.list_keys()

        self.assertEqual(keys, [])

    def test_purchase_keys_requests_the_exact_missing_quantity(self):
        http = Mock()
        http.get.return_value = _FakeResponse(
            text=(
                '{"status":100,"keyxoay":"key-a"}'
                '{"status":100,"keyxoay":"key-b"}'
                '{"status":100,"comen":"successful transaction 2 key xoay","soluong":2}'
            )
        )
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            keys = client.purchase_keys(2)

        self.assertEqual([item["key"] for item in keys], ["key-a", "key-b"])
        self.assertEqual(
            http.get.call_args.args[0],
            "https://proxy.vn/proxyxoay/apimuangay.php?"
            "key=configured-key&&thoigian=1&&soluong=2",
        )
        self.assertNotIn("json", http.get.call_args.kwargs)
        self.assertNotIn("data", http.get.call_args.kwargs)

    def test_purchase_keys_uses_documented_raw_query_without_json_or_form_body(self):
        http = Mock()
        http.get.return_value = _FakeResponse(
            text='{"status":100,"keyxoay":"key-a"}'
                  '{"status":100,"comen":"successful transaction 1 key xoay","soluong":1}'
        )
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            keys = client.purchase_keys(1)

        self.assertEqual([item["key"] for item in keys], ["key-a"])
        request = http.get.call_args
        self.assertEqual(
            request.args[0],
            "https://proxy.vn/proxyxoay/apimuangay.php?"
            "key=configured-key&&thoigian=1&&soluong=1",
        )
        self.assertNotIn("params", request.kwargs)
        self.assertNotIn("json", request.kwargs)
        self.assertNotIn("data", request.kwargs)

    def test_purchase_does_not_call_provider_without_settings_api_key(self):
        http = Mock()
        client = RotatingProxyClient(http)

        with (
            patch.object(proxy_config, "ROTATING_PROXY_API_KEY", ""),
            self.assertRaisesRegex(RotatingProxyApiError, "ROTATING_PROXY_API_KEY"),
        ):
            client.purchase_key()

        http.get.assert_not_called()

    def test_http_error_preserves_status_and_endpoint_without_secret(self):
        http = Mock()
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        http.get.return_value = response
        client = RotatingProxyClient(http)

        with (
            patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"),
            self.assertRaisesRegex(RotatingProxyApiError, r"HTTP 404.*apimuangay\.php") as caught,
        ):
            client.purchase_key()

        self.assertNotIn("configured-key", str(caught.exception))

    def test_list_keys_treats_status_101_as_empty_inventory(self):
        http = Mock()
        http.get.return_value = _FakeResponse({"status": 101, "comen": "key does not exist"})
        client = RotatingProxyClient(http)

        with patch.object(proxy_config, "ROTATING_PROXY_API_KEY", "configured-key"):
            self.assertEqual(client.list_keys(), [])


class RotatingProxyConfigTests(unittest.TestCase):
    def test_settings_expose_rotating_proxy_option_and_provider_controls(self):
        fields = {field["key"]: field for field in EDITABLE_FIELDS}

        for key in (
            "ROTATING_PROXY_ENABLED",
            "ROTATING_PROXY_API_KEY",
            "ROTATING_PROXY_PROTOCOL",
            "ROTATING_PROXY_NHAMANG",
            "ROTATING_PROXY_TINHTHANH",
            "ROTATING_PROXY_WHITELIST",
            "ROTATING_PROXY_REQUEST_TIMEOUT",
        ):
            self.assertIn(key, fields)
            self.assertEqual(fields[key]["group"], "代理池")
        self.assertTrue(fields["ROTATING_PROXY_API_KEY"]["secret"])

    def test_enabled_rotation_lease_is_forwarded_to_the_selected_driver(self):
        import main

        lease = Mock(proxy_url="http://203.0.113.20:8080", lane_id=3, proxy_expires_at=200.0)
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "roxy"), patch.object(
            proxy_config, "ROTATING_PROXY_ENABLED", True
        ), patch(
            "core.rotating_proxy_manager.get_rotating_proxy_manager"
        ) as get_manager, patch(
            "core.roxy_registration.run_roxy_registration",
            return_value={"success": True},
        ) as run_roxy, patch(
            "core.rotating_proxy_runtime.release_rotating_proxy",
        ) as release_proxy:
            get_manager.return_value.acquire.return_value = lease
            result = main.run_registration(
                "user@example.com",
                "Test User",
                "1990-01-01",
                proxy_lane_id=3,
            )

        self.assertTrue(result["success"])
        get_manager.return_value.acquire.assert_called_once_with(3)
        self.assertEqual(run_roxy.call_args.kwargs["proxy"], lease.proxy_url)
        release_proxy.assert_called_once_with(
            scope="registration",
            lane_id=3,
            proxy_url=lease.proxy_url,
        )

    def test_cli_batch_assigns_a_stable_lane_id_to_each_worker_slot(self):
        import main

        with patch.object(main, "prepare_registration_inputs", return_value=("user@example.com", "Test", "2000-01-01")), patch.object(
            main,
            "run_registration",
            return_value={"success": True},
        ) as run_registration:
            main.run_one_batch_item(5, 10, None, proxy_lane_id=2)

        self.assertEqual(run_registration.call_args.kwargs["proxy_lane_id"], 2)


class RotatingProxyWorkflowRoutingTests(unittest.TestCase):
    def test_preferred_account_proxy_releases_rotating_lease_after_context(self):
        from core.account_network import preferred_account_proxy

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False),
            patch("core.account_network.resolve_rotating_proxy", return_value="http://203.0.113.55:8080"),
            patch("core.account_network.release_rotating_proxy") as release,
            preferred_account_proxy(None, rotating_scope="codex_retry", lane_id=6) as route,
        ):
            self.assertEqual(route, ("http://203.0.113.55:8080", "rotating_proxy"))

        release.assert_called_once_with(
            scope="codex_retry",
            lane_id=6,
            proxy_url="http://203.0.113.55:8080",
        )

    def test_browser_use_cloud_session_receives_custom_rotating_proxy(self):
        from core.browser_use_client import BrowserUseClient

        response = Mock(status_code=201)
        response.json.return_value = {"id": "browser-1", "cdpUrl": "ws://browser"}
        http = Mock()
        http.post.return_value = response
        client = BrowserUseClient(api_key="browser-api-key", http_client=http)

        session = client.open_session(proxy="http://user:pass@203.0.113.60:8080")

        self.assertEqual(session.connect_url, "ws://browser")
        payload = http.post.call_args.kwargs["json"]
        self.assertEqual(payload["customProxy"]["host"], "203.0.113.60")
        self.assertEqual(payload["customProxy"]["port"], 8080)
        self.assertEqual(payload["customProxy"]["username"], "user")
        self.assertEqual(payload["customProxy"]["password"], "pass")
        self.assertNotIn("username", session.raw["custom_proxy"])
        self.assertNotIn("password", session.raw["custom_proxy"])

    def test_browser_use_fresh_profile_ignores_configured_profile_ids(self):
        from core.browser_use_client import BrowserUseClient

        client = BrowserUseClient(api_key="browser-api-key")
        with (
            patch.object(browser_use_config, "BROWSER_USE_PROFILE_ID", "saved-profile"),
            patch.object(browser_use_config, "BROWSER_USE_EXTRA_QUERY", {"profileId": "extra-profile"}),
        ):
            session = client.build_connect_url(fresh_profile=True)

        query = parse_qs(urlparse(session.connect_url).query)
        self.assertNotIn("profileId", query)
        self.assertEqual(session.profile_id, "")

    def test_codex_oauth_resolves_a_rotating_proxy_for_standalone_login(self):
        from core import codex_oauth

        lease = Mock(proxy_url="http://203.0.113.30:8080")
        with (
            patch("core.rotating_proxy_runtime.resolve_rotating_proxy", return_value=lease.proxy_url) as resolve,
            patch("core.rotating_proxy_runtime.release_rotating_proxy") as release,
            patch("core.roxy_codex_oauth.run_roxy_codex_oauth", return_value={"ok": True, "status": "success"}) as run_roxy,
            patch.object(codex_oauth._cfg, "ENABLE_CODEX_AUTO", True),
            patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "roxy"),
        ):
            result = codex_oauth.run_codex_oauth(
                "user@example.com",
                force=True,
                proxy_lane_id=4,
            )

        self.assertTrue(result["ok"])
        resolve.assert_called_once_with(None, scope="codex_oauth", lane_id=4)
        self.assertEqual(run_roxy.call_args.kwargs["proxy"], lease.proxy_url)
        release.assert_called_once_with(scope="codex_oauth", lane_id=4, proxy_url=lease.proxy_url)

    def test_plan_check_resolves_a_rotating_proxy_for_a_worker(self):
        from core import plan_check_service

        with (
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False),
            patch("core.account_network.resolve_rotating_proxy", return_value="http://203.0.113.40:8080") as resolve,
            patch.object(plan_check_service, "check_account_plan", return_value={"ok": True}) as check_plan,
            patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True),
            patch.object(plan_check_service.db, "update_account_plan_check"),
            patch.object(plan_check_service, "_wait_for_rate_slot"),
            patch.object(plan_check_service, "_registration_recheck_delay", return_value=0),
            patch.object(plan_check_service._QUEUE_SLOTS, "release"),
        ):
            result = plan_check_service._run_plan_check(
                account_id=7,
                email="user@example.com",
                access_token="token",
                trigger="manual",
                proxy=None,
                timezone_offset_min="-",
                proxy_lane_id=2,
            )

        self.assertTrue(result["ok"])
        resolve.assert_called_once_with(None, scope="plan_check", lane_id=2)
        self.assertEqual(check_plan.call_args.kwargs["proxy"], "http://203.0.113.40:8080")

    def test_retry_proxy_inventory_is_not_prepared_when_wireguard_is_enabled(self):
        from core.rotating_proxy_runtime import prepare_rotating_proxy_lanes

        with (
            patch.object(proxy_config, "ROTATING_PROXY_ENABLED", True),
            patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True),
            patch("core.rotating_proxy_manager.get_rotating_proxy_manager") as get_manager,
        ):
            prepare_rotating_proxy_lanes(3, scope="twofa_retry")
            prepare_rotating_proxy_lanes(3, scope="codex_retry")
            prepare_rotating_proxy_lanes(3, scope="plan_check")

        get_manager.assert_not_called()

    def test_totp_setup_prepares_rotating_proxy_lanes(self):
        from core import twofa_service

        with patch.object(proxy_config, "ROTATING_PROXY_ENABLED", True), patch.object(
            twofa_service, "_PROXY_INVENTORY_READY", False
        ), patch.object(twofa_service, "prepare_rotating_proxy_lanes") as prepare:
            twofa_service._prepare_proxy_inventory()

        prepare.assert_called_once_with(2, scope="twofa_setup")

    def test_totp_setup_worker_uses_rotating_proxy_lease(self):
        from core import twofa_service

        session = Mock(proxy="http://203.0.113.80:8080", device_id="device-1")
        session.fingerprint_summary_text.return_value = "fingerprint"
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "twofa.log"
            with patch.object(twofa_service, "log_path", return_value=log_file), patch.object(
                twofa_service.db, "mark_account_totp_setup_running", return_value=True
            ), patch.object(twofa_service.db, "update_account_totp_secret"), patch.object(
                twofa_service, "resolve_rotating_proxy", return_value=session.proxy
            ) as resolve, patch.object(twofa_service, "release_rotating_proxy") as release, patch.object(
                twofa_service, "BrowserSession", return_value=session
            ) as browser, patch.object(
                twofa_service, "setup_2fa", return_value="SECRET1234"
            ), patch.object(twofa_service._QUEUE_SLOTS, "release"):
                result = twofa_service._run_twofa(
                    account_id=1,
                    email="user@example.com",
                    access_token="token",
                    proxy=None,
                    trigger="manual",
                    proxy_lane_id=3,
                )

        self.assertTrue(result["ok"])
        resolve.assert_called_once_with(None, scope="twofa_setup", lane_id=3)
        browser.assert_called_once_with(proxy=session.proxy, fingerprint_seed="account:user@example.com")
        release.assert_called_once_with(scope="twofa_setup", lane_id=3, proxy_url=session.proxy)

    def test_extract_link_worker_routes_create_request_through_rotating_proxy(self):
        from core import extract_link_service

        response = Mock(status_code=201)
        response.json.return_value = {"job_id": "job-1"}
        session = Mock()
        session.post.return_value = response

        with (
            patch.object(extract_link_service, "_api_base", return_value="https://extract.test"),
            patch.object(extract_link_service, "_cdk", return_value="cdk"),
            patch.object(extract_link_service, "_session", return_value=session),
            patch.object(
                extract_link_service,
                "resolve_rotating_proxy",
                return_value="http://203.0.113.70:8080",
            ) as resolve,
        ):
            result = extract_link_service._create_extract_job(
                token="token",
                link_type="pix",
                cdk="cdk",
                proxy=None,
                proxy_lane_id=3,
            )

        self.assertEqual(result["job_id"], "job-1")
        resolve.assert_called_once_with(None, scope="extract_link", lane_id=3)
        self.assertEqual(
            session.post.call_args.kwargs["proxies"],
            {"http": "http://203.0.113.70:8080", "https": "http://203.0.113.70:8080"},
        )

    def test_extract_link_job_reuses_one_proxy_for_create_and_event_stream(self):
        from core import extract_link_service

        active_proxy = "http://203.0.113.71:8080"
        with (
            patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True),
            patch.object(extract_link_service, "resolve_rotating_proxy", return_value=active_proxy) as resolve,
            patch.object(extract_link_service, "_create_extract_job", return_value={"job_id": "job-2"}) as create,
            patch.object(
                extract_link_service,
                "_iter_sse_events",
                return_value=iter([("result", {"result": {"ok": True}})]),
            ) as events,
            patch.object(extract_link_service, "release_rotating_proxy") as release,
            patch.object(extract_link_service.db, "update_account_extract"),
            patch.object(extract_link_service._QUEUE_SLOTS, "release"),
        ):
            result = extract_link_service._run_extract(
                account_id=8,
                email="user@example.com",
                access_token="token",
                link_type="pix",
                cdk="cdk",
                trigger="manual",
                proxy_lane_id=4,
            )

        self.assertTrue(result["ok"])
        resolve.assert_called_once_with(None, scope="extract_link", lane_id=4)
        self.assertEqual(create.call_args.kwargs["proxy"], active_proxy)
        self.assertEqual(events.call_args.kwargs["proxy"], active_proxy)
        release.assert_called_once_with(scope="extract_link", lane_id=4, proxy_url=active_proxy)


class RotatingProxyWebUiTests(unittest.TestCase):
    def test_status_route_only_returns_masked_manager_status(self):
        app = Flask(__name__)
        register_rotating_proxy_routes(app)
        manager = Mock()
        manager.status.return_value = {
            "enabled": True,
            "configured": True,
            "keys": [{"key": "key-1...3456", "lanes": [0]}],
            "leases": [],
        }

        with patch("webui.rotating_proxy_api.get_rotating_proxy_manager", return_value=manager):
            response = app.test_client().get("/api/proxy/rotating")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("configured-provider-api-key", response.get_data(as_text=True))
        self.assertEqual(response.get_json()["keys"][0]["key"], "key-1...3456")

    def test_create_app_registers_rotating_proxy_status_route(self):
        from webui.app import create_app

        manager = Mock()
        manager.status.return_value = {"enabled": False, "configured": False, "keys": [], "leases": []}
        app = create_app(auth_code="test-auth")

        with patch("webui.rotating_proxy_api.get_rotating_proxy_manager", return_value=manager):
            response = app.test_client().get(
                "/api/proxy/rotating",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["enabled"])

    def test_status_route_requires_webui_authentication(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")

        response = app.test_client().get(
            "/api/proxy/rotating",
            headers={"Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
