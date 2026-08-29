import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from config import proxy as proxy_config
from core.rotating_proxy_client import RotatingProxyClient
from core.rotating_proxy_manager import RotatingProxyManager
from core.rotating_proxy_store import RotatingProxyStore
from webui.config_editor import EDITABLE_FIELDS
from webui.rotating_proxy_api import register_rotating_proxy_routes


class _FakeResponse:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

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

    def test_fetches_a_new_proxy_after_ttl_expiration(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 1000.0}])
        manager = self._manager(client)

        first = manager.acquire(0)
        self.now[0] = 161.0
        second = manager.acquire(0)

        self.assertNotEqual(first.proxy_url, second.proxy_url)
        self.assertEqual(client.get_calls, ["key-1", "key-1"])

    def test_renews_expired_key_before_fetching_proxy(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 160.0}])
        manager = self._manager(client)

        manager.acquire(0)
        self.now[0] = 161.0
        manager.acquire(0)

        self.assertEqual(client.renew_calls, ["key-1"])
        self.assertEqual(client.get_calls, ["key-1", "key-1"])

    def test_successful_renewal_without_expiry_does_not_renew_on_every_request(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 160.0}])
        client.renew_key = lambda key: (client.renew_calls.append(key) or {"key": key, "expires_at": None})
        manager = self._manager(client)

        manager.acquire(0)
        self.now[0] = 161.0
        manager.acquire(0)
        self.now[0] = 162.0
        manager.acquire(0)

        self.assertEqual(client.get_calls, ["key-1", "key-1"])
        self.assertEqual(client.renew_calls, ["key-1"])

    def test_renewal_without_expiry_gets_daily_expiry_for_next_cycle(self):
        client = _FakeRotatingProxyClient([{"key": "key-1", "expires_at": 160.0}])
        client.renew_key = lambda key: (client.renew_calls.append(key) or {"key": key, "expires_at": None})
        manager = self._manager(client)

        manager.acquire(0)
        self.now[0] = 161.0
        manager.acquire(0)
        self.now[0] = 161.0 + 24 * 60 * 60 + 1
        manager.acquire(0)

        self.assertEqual(client.renew_calls, ["key-1", "key-1"])

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
        ) as run_roxy:
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

    def test_codex_oauth_resolves_a_rotating_proxy_for_standalone_login(self):
        import core.codex_oauth as codex_oauth

        lease = Mock(proxy_url="http://203.0.113.30:8080")
        with (
            patch("core.rotating_proxy_runtime.resolve_rotating_proxy", return_value=lease.proxy_url) as resolve,
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

    def test_plan_check_resolves_a_rotating_proxy_for_a_worker(self):
        from core import plan_check_service

        with (
            patch.object(plan_check_service, "resolve_rotating_proxy", return_value="http://203.0.113.40:8080") as resolve,
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
