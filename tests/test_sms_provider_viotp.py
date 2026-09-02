import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import sms_provider


class _Resp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = "{}"

    def json(self):
        return self._data


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class ViOtpSmsProviderTests(unittest.TestCase):
    def setUp(self):
        sms_provider._ACQUIRED_AT.clear()

    def tearDown(self):
        sms_provider._ACQUIRED_AT.clear()

    def _config(self):
        return (
            patch.object(codex_config, "SMS_PROVIDER", "viotp"),
            patch.object(codex_config, "VIOTP_API_BASE", "https://api.viotp.test"),
            patch.object(codex_config, "VIOTP_API_TOKEN", "secret-token"),
            patch.object(codex_config, "VIOTP_SERVICE_ID", "123"),
            patch.object(codex_config, "VIOTP_COUNTRY", "vn"),
            patch.object(codex_config, "VIOTP_NETWORK", "VIETTEL|MOBIFONE"),
        )

    def test_viotp_defaults_use_live_openai_service_id_and_vinaphone(self):
        self.assertEqual(codex_config.VIOTP_SERVICE_ID, "1234")
        self.assertEqual(codex_config.VIOTP_NETWORK, "VINAPHONE")

    def test_openai_service_selection_prefers_chatgpt_openai_name(self):
        from core import viotp_sms_client

        http = _Http([{
            "status_code": 200,
            "success": True,
            "data": [
                {"id": 77, "name": "GPT Basic", "price": 100},
                {"id": 1234, "name": "OpenAI | ChatGPT", "price": 2900},
                {"id": 88, "name": "Facebook", "price": 1},
            ],
        }])
        service = viotp_sms_client.select_openai_service(
            http,
            api_base="https://api.viotp.test",
            token="secret-token",
            country="vn",
        )
        self.assertEqual(service["id"], 1234)
        self.assertEqual(http.calls[0]["params"], {"token": "secret-token", "country": "vn"})

    def test_openai_service_selection_rejects_unmatched_services(self):
        from core import viotp_sms_client

        http = _Http([{
            "status_code": 200,
            "success": True,
            "data": [{"id": 1, "name": "Facebook", "price": 1}],
        }])
        with self.assertRaisesRegex(viotp_sms_client.ViOtpClientError, "未找到"):
            viotp_sms_client.select_openai_service(
                http,
                api_base="https://api.viotp.test",
                token="secret-token",
                country="vn",
            )

    def test_secret_registry_and_webui_fields_include_viotp(self):
        http = _Http([{
            "status_code": 200,
            "success": True,
            "message": "successful",
            "data": {
                "request_id": "req-1",
                "phone_number": "987654321",
                "re_phone_number": "84987654321",
            },
        }])
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "req-1")
        self.assertEqual(phone, "84987654321")
        self.assertTrue(http.calls[0]["url"].endswith("/request/getv2"))
        self.assertEqual(http.calls[0]["params"], {
            "token": "secret-token",
            "serviceId": "123",
            "country": "vn",
            "network": "VIETTEL|MOBIFONE",
        })

    def test_acquire_number_adds_country_code_without_re_phone_number(self):
        http = _Http([{
            "status_code": 200,
            "success": True,
            "data": {
                "request_id": "req-2",
                "phone_number": "987654321",
                "countryCode": "84",
            },
        }])
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            _, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(phone, "84987654321")

    def test_acquire_number_maps_balance_and_inventory_errors(self):
        responses = [
            {"status_code": -2, "success": False, "message": "balance"},
            {"status_code": -3, "success": False, "message": "no phone"},
        ]
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider.acquire_number(http=_Http([responses[0]]))
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(http=_Http([responses[1]]))

    def test_acquire_number_rejects_missing_response_fields(self):
        http = _Http([{
            "status_code": 200,
            "success": True,
            "data": {"phone_number": "987654321"},
        }])
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             self.assertRaisesRegex(sms_provider.SmsProviderError, "request_id"):
            sms_provider.acquire_number(http=http)

    def test_wait_for_sms_code_polls_until_complete(self):
        http = _Http([
            {"status_code": 200, "success": True, "data": {"Status": 0}},
            {"status_code": 200, "success": True, "data": {"Status": 1, "Code": "486460"}},
        ])
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(sms_provider.time, "sleep"):
            code = sms_provider.wait_for_sms_code(
                "req-1", http=http, max_wait=10, poll_interval=1
            )

        self.assertEqual(code, "486460")
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0]["url"].endswith("/session/getv2"))
        self.assertEqual(http.calls[0]["params"]["requestId"], "req-1")

    def test_wait_for_sms_code_maps_expired_session_to_timeout(self):
        http = _Http([{
            "status_code": 200,
            "success": True,
            "data": {"Status": 2},
        }])
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             self.assertRaisesRegex(sms_provider.SmsCodeTimeout, "过期"):
            sms_provider.wait_for_sms_code("req-1", http=http, max_wait=1)

    def test_lifecycle_does_not_send_viotp_status_requests(self):
        http = _Http([])
        sms_provider._ACQUIRED_AT["req-1"] = 1.0
        patches = self._config()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertEqual(sms_provider.set_status("req-1", 1, http=http), "OK")
            sms_provider.complete("req-1", http=http)
            sms_provider._ACQUIRED_AT["req-2"] = 1.0
            sms_provider.cancel("req-2", http=http)

        self.assertEqual(http.calls, [])
        self.assertNotIn("req-1", sms_provider._ACQUIRED_AT)
        self.assertNotIn("req-2", sms_provider._ACQUIRED_AT)


if __name__ == "__main__":
    unittest.main()
