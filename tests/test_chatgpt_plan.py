# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import chatgpt_plan


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, responses):
        self._responses = iter(responses)

    def get(self, *args, **kwargs):
        return next(self._responses)

    def close(self):
        pass


class _BrowserSession:
    def __init__(self, proxy, responses):
        self.proxy = proxy
        self.device_id = "device"
        self.session = _Session(responses)
        self.get_calls = []

    def _get_common_headers(self):
        return {}

    def get_chatgpt_headers(self, referer="https://chatgpt.com/"):
        return {
            **self._get_common_headers(),
            "oai-client-build-number": "8370486",
            "oai-client-version": "test-build",
            "oai-session-id": "session",
        }

    def navigator_language(self):
        return "en-US"

    def js_timezone_offset_min(self):
        return -540

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.session.get(url, **kwargs)


class ChatgptPlanTests(unittest.TestCase):
    def test_subscription_free_plan_is_normalized_to_free_account_plan(self):
        result = chatgpt_plan.parse_accounts_check(
            {
                "accounts": {
                    "default": {
                        "account": {"account_id": "account-1"},
                        "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    }
                }
            }
        )

        self.assertEqual(result["current_plan_type"], "free")
        self.assertTrue(result["ok"])

    def test_auto_route_excludes_failed_proxy_from_pool(self):
        from config import proxy as proxy_cfg

        with (
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto"),
            patch.object(proxy_cfg, "PLAN_CHECK_PROXY", ""),
            patch.object(
                proxy_cfg,
                "pick_proxy",
                side_effect=["http://broken:1", "http://working:2"],
            ),
            patch.object(chatgpt_plan, "_local_proxy_status", return_value=(False, True, None)),
        ):
            route = chatgpt_plan.resolve_plan_check_route(
                None,
                exclude_proxy="http://broken:1",
            )

        self.assertEqual(route["proxy"], "http://working:2")
        self.assertEqual(route["network_route"], "proxy")

    def test_auto_route_changes_proxy_after_cloudflare_403(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        routes = [
            {
                "proxy": "http://blocked:1",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "http://***:***@blocked:1",
                "proxy_fallback_reason": None,
            },
            {
                "proxy": "http://working:2",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "http://***:***@working:2",
                "proxy_fallback_reason": None,
            },
        ]
        sessions = [
            _BrowserSession("http://blocked:1", [_Response(403, text="cloudflare")]),
            _BrowserSession("http://working:2", [_Response(200, payload=payload)]),
        ]

        with (
            patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve,
            patch.object(chatgpt_plan, "BrowserSession", side_effect=sessions),
            patch.object(chatgpt_plan.time, "sleep"),
        ):
            result = chatgpt_plan.check_account_plan("token", max_attempts=2, retry_delay=0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["proxy_used"], "http://***:***@working:2")
        self.assertEqual(resolve.call_count, 2)

    def test_plan_check_uses_browser_session_wrapper_and_version_target_route(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        session = _BrowserSession("http://working:2", [_Response(200, payload=payload)])
        route = {
            "proxy": "http://working:2",
            "proxy_mode": "auto",
            "network_route": "proxy",
            "proxy_used": "http://***:***@working:2",
            "proxy_fallback_reason": None,
        }

        with (
            patch.object(chatgpt_plan, "resolve_plan_check_route", return_value=route),
            patch.object(chatgpt_plan, "BrowserSession", return_value=session),
        ):
            result = chatgpt_plan.check_account_plan("token", max_attempts=1, retry_delay=0)

        self.assertTrue(result["ok"])
        self.assertEqual(len(session.get_calls), 1)
        url = session.get_calls[0][0]
        headers = session.get_calls[0][1]["headers"]
        self.assertIn("timezone_offset_min=-540", url)
        self.assertIn("oai-client-build-number", headers)
        self.assertEqual(headers["x-openai-target-route"], "/backend-api/accounts/check/{version}")

    def test_plan_check_uses_supplied_authenticated_browser_transport(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        browser_transport = _BrowserSession("http://registration:1", [_Response(200, payload=payload)])

        with (
            patch.object(chatgpt_plan, "BrowserSession", side_effect=AssertionError("must not create a new session")),
            patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=AssertionError("must use browser route")),
        ):
            result = chatgpt_plan.check_account_plan(
                "token",
                proxy="http://registration-proxy:1",
                browser_transport=browser_transport,
                max_attempts=1,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["network_route"], "browser")
        self.assertEqual(len(browser_transport.get_calls), 1)

    def test_browser_transport_retries_fetch_status_zero_in_same_session(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        browser_transport = _BrowserSession(
            "http://registration:1",
            [
                _Response(0, text="TypeError: Failed to fetch"),
                _Response(200, payload=payload),
            ],
        )

        with (
            patch.object(chatgpt_plan, "BrowserSession", side_effect=AssertionError("must use browser session")),
            patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=AssertionError("must use browser route")),
            patch.object(chatgpt_plan.time, "sleep"),
        ):
            result = chatgpt_plan.check_account_plan(
                "token",
                browser_transport=browser_transport,
                max_attempts=2,
                retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(browser_transport.get_calls), 2)

    def test_network_failure_switches_to_another_auto_route(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        failed = _BrowserSession("http://broken:1", [])
        failed.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("SOCKS5 connection failed"))
        working = _BrowserSession("", [_Response(200, payload=payload)])
        routes = [
            {
                "proxy": "http://broken:1",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "http://broken:1",
                "proxy_fallback_reason": None,
            },
            {
                "proxy": "",
                "proxy_mode": "auto",
                "network_route": "direct_fallback",
                "proxy_used": "http://broken:1",
                "proxy_fallback_reason": "proxy request failed",
            },
        ]

        with (
            patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve,
            patch.object(chatgpt_plan, "BrowserSession", side_effect=[failed, working]),
            patch.object(chatgpt_plan.time, "sleep"),
        ):
            result = chatgpt_plan.check_account_plan("token", max_attempts=2, retry_delay=0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["network_route"], "direct_fallback")
        self.assertEqual(resolve.call_count, 2)

    def test_403_switches_to_direct_fallback_route(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "account-1", "plan_type": "free"},
                    "entitlement": {},
                }
            }
        }
        routes = [
            {
                "proxy": "http://blocked:1",
                "proxy_mode": "auto",
                "network_route": "proxy",
                "proxy_used": "http://blocked:1",
                "proxy_fallback_reason": None,
            },
            {
                "proxy": "",
                "proxy_mode": "auto",
                "network_route": "direct_fallback",
                "proxy_used": "http://blocked:1",
                "proxy_fallback_reason": "proxy request rejected",
            },
        ]
        sessions = [
            _BrowserSession("http://blocked:1", [_Response(403, text="forbidden")]),
            _BrowserSession("", [_Response(200, payload=payload)]),
        ]

        with (
            patch.object(chatgpt_plan, "resolve_plan_check_route", side_effect=routes) as resolve,
            patch.object(chatgpt_plan, "BrowserSession", side_effect=sessions),
            patch.object(chatgpt_plan.time, "sleep"),
        ):
            result = chatgpt_plan.check_account_plan("token", max_attempts=2, retry_delay=0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["network_route"], "direct_fallback")
        self.assertEqual(resolve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
