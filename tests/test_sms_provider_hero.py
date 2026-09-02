import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import hero_sms_client, sms_provider
from core.hero_sms_country_store import HeroSmsCountryStore
from webui import config_editor


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self.responses.pop(0)

    def close(self):
        pass


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _ConcurrentHttp:
    def __init__(self, prices):
        self.prices = prices
        self.calls = []
        self.selected_countries = []
        self._lock = threading.Lock()
        self._prices_barrier = threading.Barrier(2)
        self._number_barrier = threading.Barrier(2)

    def get(self, url, params=None):
        request = dict(params or {})
        with self._lock:
            self.calls.append({"url": url, "params": request})
        if request.get("action") == "getPrices":
            self._prices_barrier.wait(timeout=5)
            return _Resp(json.dumps(self.prices))
        if request.get("action") == "getNumber":
            with self._lock:
                self.selected_countries.append(request["country"])
                number_index = len(self.selected_countries)
            self._number_barrier.wait(timeout=5)
            return _Resp(f"ACCESS_NUMBER:hero-concurrent-{number_index}:155500000{number_index}")
        raise AssertionError(f"unexpected HeroSMS action: {request.get('action')}")


class HeroSmsProviderTests(unittest.TestCase):
    def setUp(self):
        hero_sms_client._AUTO_COUNTRY_CURSOR.clear()
        sms_provider._ACQUIRED_AT.clear()
        sms_provider._ACQUIRED_METADATA.clear()
        self._state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_dir.cleanup)
        self._country_store = HeroSmsCountryStore(Path(self._state_dir.name) / "state.sqlite3")
        self._country_store_patch = patch.object(hero_sms_client, "_COUNTRY_STORE", self._country_store)
        self._country_store_patch.start()
        self.addCleanup(self._country_store_patch.stop)

    def test_hero_secret_and_settings_fields_are_registered(self):
        self.assertIn("HERO_SMS_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["HERO_SMS_SERVICE"]["type"], "str")
        self.assertEqual(fields["HERO_SMS_COUNTRY"]["type"], "str")
        self.assertEqual(fields["HERO_SMS_NUMBER_REJECT_THRESHOLD"]["type"], "int")
        self.assertTrue(fields["HERO_SMS_API_KEY"].get("secret"))
        self.assertIn("'HeroSMS'", Path("webui/templates/index.html").read_text(encoding="utf-8"))

    def _config(self, **overrides):
        values = {
            "SMS_PROVIDER": "hero",
            "SMS_REQUEST_TIMEOUT": 30,
            "SMS_SERVICE": "dr",
            "SMS_COUNTRY": "auto",
            "SMS_MAX_PRICE": "",
            "SMS_POLL_INTERVAL": 5,
            "HERO_SMS_API_BASE": "https://hero.test/stubs/handler_api.php",
            "HERO_SMS_API_KEY": "hero-key",
            "HERO_SMS_SERVICE": "dr",
            "HERO_SMS_COUNTRY": "auto",
            "HERO_SMS_MAX_PRICE": "",
        }
        values.update(overrides)
        return patch.multiple(codex_config, **values)

    def test_auto_country_uses_cheapest_openai_stock(self):
        http = _Http([
            _Resp(json.dumps({
                "16": {"dr": {"cost": 0.08, "count": 5}},
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "10": {"dr": {"cost": 0.01, "count": 0}},
            })),
            _Resp("ACCESS_NUMBER:hero-1:+66987654321"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-1", "66987654321"))
        self.assertEqual(http.calls[0]["params"]["action"], "getPrices")
        self.assertEqual(http.calls[0]["params"]["service"], "dr")
        self.assertEqual(http.calls[1]["params"]["action"], "getNumber")
        self.assertEqual(http.calls[1]["params"]["service"], "dr")
        self.assertEqual(http.calls[1]["params"]["country"], "52")
        self.assertEqual(http.calls[1]["params"]["maxPrice"], "0.03")

    def test_auto_country_orders_all_live_offers_by_cost_without_fixed_threshold(self):
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.04, "count": 1}},
                "16": {"dr": {"cost": 0.01, "count": 1}},
                "31": {"dr": {"cost": 0.025, "count": 1}},
                "44": {"dr": {"cost": 0.075, "count": 1}},
                "55": {"dr": {"cost": 0.1, "count": 1}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-dynamic-price:441234567890"),
        ])

        with self._config(HERO_SMS_MAX_PRICE="0.1"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-dynamic-price", "441234567890"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual(
            [call["params"]["country"] for call in get_number_calls],
            ["16", "31", "52", "44", "55"],
        )

    def test_auto_country_skips_no_numbers_and_tries_next_cheapest(self):
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-2:441234567890"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-2", "441234567890"))
        self.assertEqual(http.calls[1]["params"]["country"], "52")
        self.assertEqual(http.calls[2]["params"]["country"], "16")

    def test_auto_country_keeps_a_single_failed_country_eligible(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        self._country_store.mark_unusable(profile, "52", "phone verification rejected")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-blocked-1:441234567890"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-blocked-1", "441234567890"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52"])

    def test_auto_country_skips_a_high_failure_rate_country(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        for _ in range(4):
            self._country_store.mark_unusable(profile, "52", "otp timeout")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-blocked-2:441234567890"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-blocked-2", "441234567890"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["16"])

    def test_auto_country_uses_high_failure_country_only_as_fallback(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        for _ in range(4):
            self._country_store.mark_unusable(profile, "52", "otp timeout")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-recovery-1:525500000001"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-recovery-1", "525500000001"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["16", "52"])

    def test_auto_country_probes_one_recovery_country_when_all_are_high_risk(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        for country in ("52", "16"):
            for _ in range(4):
                self._country_store.mark_unusable(profile, country, "otp timeout")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
        ])

        with self._config(), self.assertRaises(sms_provider.SmsNoNumbersError):
            sms_provider.acquire_number(http=http)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual(len(get_number_calls), 1)

    def test_auto_country_prefers_cheaper_offer_over_expensive_sticky_country(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "0.1")
        self._country_store.mark_verified(profile, "16", "0.1")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.05, "count": 2}},
                "16": {"dr": {"cost": 0.1, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-preferred-1:441234567890"),
        ])

        with self._config(HERO_SMS_MAX_PRICE="0.1"), self.assertLogs(
            "core.hero_sms_client", level="INFO"
        ) as captured:
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-preferred-1", "441234567890"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52", "16"])
        self.assertEqual(
            [call["params"]["maxPrice"] for call in get_number_calls],
            ["0.1", "0.1"],
        )
        self.assertTrue(
            any(
                "stickyMode=deferred_for_price" in message
                and "primary=52:0.05,16:0.1" in message
                for message in captured.output
            )
        )
        self.assertTrue(any("offerCost=0.05 maxPrice=0.1" in message for message in captured.output))
        self.assertTrue(any("offerCost=0.1 maxPrice=0.1" in message for message in captured.output))

    def test_auto_country_keeps_verified_country_until_it_fails(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        self._country_store.mark_verified(profile, "52", "0.03")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-preferred-1:525500000001"),
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 1}},
                "16": {"dr": {"cost": 0.08, "count": 4}},
            })),
            _Resp("ACCESS_NUMBER:hero-next-1:165500000002"),
        ])

        with self._config():
            sms_provider.acquire_number(http=http)
            sms_provider.acquire_number(http=http)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52", "52"])

    def test_auto_country_switches_after_sticky_country_reports_no_numbers(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        self._country_store.mark_verified(profile, "52", "0.03")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
                "31": {"dr": {"cost": 0.09, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-sticky-fallback-1:315500000002"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-sticky-fallback-1", "315500000002"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52", "16", "31"])

    def test_auto_country_sticky_state_is_scoped_per_lane(self):
        lane_a = "worker-a"
        lane_b = "worker-b"
        self._country_store.mark_verified(
            hero_sms_client.make_profile_key(
                "https://hero.test/stubs/handler_api.php", "dr", "", lane_key=lane_a
            ),
            "52",
            "0.03",
        )
        self._country_store.mark_verified(
            hero_sms_client.make_profile_key(
                "https://hero.test/stubs/handler_api.php", "dr", "", lane_key=lane_b
            ),
            "16",
            "0.03",
        )
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.03, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-lane-a:525500000001"),
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.03, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-lane-b:165500000002"),
        ])

        with self._config():
            sms_provider.acquire_number(http=http, lane_key=lane_a)
            sms_provider.acquire_number(http=http, lane_key=lane_b)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52", "16"])

    def test_auto_country_sticky_failure_scans_the_current_pool(self):
        lane = "worker-a"
        profile = hero_sms_client.make_profile_key(
            "https://hero.test/stubs/handler_api.php", "dr", "", lane_key=lane
        )
        self._country_store.mark_verified(profile, "52", "0.03")
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
                "31": {"dr": {"cost": 0.09, "count": 5}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("ACCESS_NUMBER:hero-lane-scan:315500000003"),
        ])

        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http, lane_key=lane)

        self.assertEqual((activation_id, phone), ("hero-lane-scan", "315500000003"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["52", "16", "31"])

    def test_auto_country_reserves_distinct_candidates_for_concurrent_workers(self):
        http = _ConcurrentHttp({
            "52": {"dr": {"cost": 0.03, "count": 2}},
            "16": {"dr": {"cost": 0.03, "count": 5}},
        })

        def acquire():
            return hero_sms_client.acquire_number_with_metadata(
                http,
                api_base="https://hero.test/stubs/handler_api.php",
                api_key="hero-key",
                service="dr",
                country="auto",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: acquire(), range(2)))

        self.assertEqual(len(results), 2)
        self.assertEqual(set(http.selected_countries), {"52", "16"})

    def test_cancel_records_failure_without_immediate_block(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        http = _Http([
            _Resp(json.dumps({"52": {"dr": {"cost": 0.03, "count": 2}}})),
            _Resp("ACCESS_NUMBER:hero-failed-1:525500000001"),
            _Resp("ACCESS_CANCEL"),
        ])

        with self._config(), patch.object(sms_provider.time, "time", return_value=100.0):
            sms_provider.acquire_number(http=http)
        with self._config(), patch.object(sms_provider.time, "time", return_value=110.0), \
                patch.object(sms_provider.time, "sleep"), patch.object(sms_provider, "_http", return_value=http):
            sms_provider.cancel("hero-failed-1", background=False)

        self.assertEqual(self._country_store.blocked_countries(profile), set())

    def test_auto_country_avoids_repeatedly_used_phone_country(self):
        profile = hero_sms_client.make_profile_key(
            "https://hero.test/stubs/handler_api.php", "dr", ""
        )
        for _ in range(3):
            hero_sms_client.record_country_unusable(
                {
                    "remember_country": True,
                    "profile_key": profile,
                    "country": "52",
                },
                "phone_used_or_max",
            )
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.08, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-fallback-country:165500000001"),
        ])

        with self._config(HERO_SMS_NUMBER_REJECT_THRESHOLD=3):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-fallback-country", "165500000001"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["16"])

    def test_complete_persists_the_selected_country_as_verified(self):
        profile = hero_sms_client.make_profile_key("https://hero.test/stubs/handler_api.php", "dr", "")
        http = _Http([
            _Resp(json.dumps({"16": {"dr": {"cost": 0.08, "count": 2}}})),
            _Resp("ACCESS_NUMBER:hero-success-1:165500000001"),
            _Resp("ACCESS_ACTIVATION"),
        ])

        with self._config():
            sms_provider.acquire_number(http=http)
            sms_provider.complete("hero-success-1", http=http)

        self.assertEqual(self._country_store.verified_countries(profile), {"16": "0.08"})

    def test_auto_country_rotates_to_next_healthy_country_on_next_acquire(self):
        http = _Http([
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 2}},
                "16": {"dr": {"cost": 0.03, "count": 5}},
            })),
            _Resp("ACCESS_NUMBER:hero-rotate-1:+525500000001"),
            _Resp(json.dumps({
                "52": {"dr": {"cost": 0.03, "count": 1}},
                "16": {"dr": {"cost": 0.03, "count": 4}},
            })),
            _Resp("ACCESS_NUMBER:hero-rotate-2:+165500000002"),
        ])

        with self._config():
            sms_provider.acquire_number(http=http)
            sms_provider.acquire_number(http=http)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["16", "52"])

    def test_auto_country_scans_all_current_candidates_after_no_numbers(self):
        prices = {
            str(country): {"dr": {"cost": country / 100, "count": 1}}
            for country in range(1, 13)
        }
        http = _Http([_Resp(json.dumps(prices)), *(_Resp("NO_NUMBERS") for _ in range(12))])

        with self._config(), self.assertRaises(sms_provider.SmsNoNumbersError):
            sms_provider.acquire_number(http=http)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual(len(get_number_calls), 12)
        self.assertIn("11", [call["params"]["country"] for call in get_number_calls])
        self.assertIn("12", [call["params"]["country"] for call in get_number_calls])

    def test_auto_country_uses_healthy_country_within_hard_max(self):
        profile = hero_sms_client.make_profile_key(
            "https://hero.test/stubs/handler_api.php", "dr", "0.05"
        )
        for country in ("4", "31"):
            for _ in range(4):
                self._country_store.mark_unusable(profile, country, "otp timeout")
        http = _Http([
            _Resp(json.dumps({
                "4": {"dr": {"cost": 0.05, "count": 1}},
                "31": {"dr": {"cost": 0.05, "count": 1}},
                "44": {"dr": {"cost": 0.075, "count": 1}},
                "55": {"dr": {"cost": 0.1, "count": 1}},
            })),
            _Resp("ACCESS_NUMBER:hero-healthy-1:441234567890"),
        ])

        with self._config(HERO_SMS_MAX_PRICE="0.1"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("hero-healthy-1", "441234567890"))
        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["44"])
        self.assertEqual([call["params"]["maxPrice"] for call in get_number_calls], ["0.1"])

    def test_auto_country_does_not_exceed_configured_max_price(self):
        http = _Http([
            _Resp(json.dumps({
                "4": {"dr": {"cost": 0.05, "count": 1}},
                "44": {"dr": {"cost": 0.075, "count": 1}},
                "55": {"dr": {"cost": 0.1, "count": 1}},
                "66": {"dr": {"cost": 0.15, "count": 1}},
            })),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
            _Resp("NO_NUMBERS"),
        ])

        with self._config(HERO_SMS_MAX_PRICE="0.1"), \
                self.assertRaises(sms_provider.SmsNoNumbersError):
            sms_provider.acquire_number(http=http)

        get_number_calls = [call for call in http.calls if call["params"].get("action") == "getNumber"]
        self.assertEqual([call["params"]["country"] for call in get_number_calls], ["4", "44", "55"])
        self.assertTrue(all(call["params"]["maxPrice"] <= "0.1" for call in get_number_calls))

    def test_wait_complete_and_cancel_use_hero_status_actions(self):
        http = _Http([
            _Resp("STATUS_WAIT_CODE"),
            _Resp("STATUS_OK:123456"),
            _Resp("ACCESS_ACTIVATION"),
            _Resp("ACCESS_CANCEL"),
        ])

        with self._config(SMS_POLL_INTERVAL=0):
            code = sms_provider.wait_for_sms_code("hero-3", http=http, max_wait=1, poll_interval=0)
            sms_provider.complete("hero-3", http=http)
            sms_provider.cancel("hero-4", http=http, background=False)

        self.assertEqual(code, "123456")
        self.assertEqual(http.calls[0]["params"], {
            "api_key": "hero-key", "action": "getStatus", "id": "hero-3",
        })
        self.assertEqual(http.calls[2]["params"]["status"], "6")
        self.assertEqual(http.calls[3]["params"]["status"], "8")

    def test_wait_polls_at_deadline_after_capped_final_sleep(self):
        http = _Http([
            _Resp("STATUS_WAIT_CODE"),
            _Resp("STATUS_WAIT_CODE"),
            _Resp("STATUS_OK:123456"),
        ])
        clock = _Clock()

        with self._config(), \
                patch.object(sms_provider.time, "time", side_effect=clock.time), \
                patch.object(sms_provider.time, "sleep", side_effect=clock.sleep):
            code = sms_provider.wait_for_sms_code(
                "hero-deadline", http=http, max_wait=10, poll_interval=6
            )

        self.assertEqual(code, "123456")
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(clock.now, 10.0)

    def test_wait_resend_requests_next_sms(self):
        http = _Http([
            _Resp("STATUS_WAIT_RESEND:000000"),
            _Resp("ACCESS_RETRY_GET"),
            _Resp("STATUS_OK:654321"),
        ])

        with self._config(SMS_POLL_INTERVAL=0):
            code = sms_provider.wait_for_sms_code("hero-resend", http=http, max_wait=1, poll_interval=0)

        self.assertEqual(code, "654321")
        self.assertEqual(http.calls[1]["params"], {
            "api_key": "hero-key", "action": "setStatus", "id": "hero-resend", "status": "3",
        })

    def test_cancel_waits_for_recent_activation_before_status_eight(self):
        http = _Http([_Resp("ACCESS_CANCEL")])
        sms_provider._ACQUIRED_AT["hero-recent"] = 100.0

        with self._config(), patch.object(sms_provider.time, "time", return_value=110.0), \
                patch.object(sms_provider.time, "sleep"), patch.object(sms_provider, "_http", return_value=http):
            sms_provider.cancel("hero-recent", background=False)

        self.assertEqual(http.calls[0]["params"]["status"], "8")
        self.assertNotIn("hero-recent", sms_provider._ACQUIRED_AT)


if __name__ == "__main__":
    unittest.main()
