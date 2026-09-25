import json
import unittest
from typing import ClassVar
from unittest.mock import patch

from core.browser_traffic import PlaywrightTrafficTracker, SeleniumTrafficTracker


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners.get(event, []).remove(callback)

    def emit(self, event, *args):
        for callback in list(self.listeners.get(event, [])):
            callback(*args)


class _Request:
    method = "POST"
    url = "https://example.test/register"
    headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}
    post_data = "{}"

    def sizes(self):
        return {
            "requestBodySize": 2,
            "requestHeadersSize": 10,
            "responseBodySize": 20,
            "responseHeadersSize": 8,
        }


class _DetailedResponse:
    status = 201
    headers = {"content-type": "application/json", "x-secret": "response-header-secret"}
    from_service_worker = False


class _DetailedRequest(_Request):
    resource_type = "xhr"
    url = "https://example.test/api/register?token=do-not-log&step=1"
    post_data = '{"password":"request-body-secret"}'

    def response(self):
        return _DetailedResponse()


class _FailedRequest(_Request):
    resource_type = "document"
    url = "https://chatgpt.com/auth/login?token=failed-secret"
    headers = {"accept": "text/html", "authorization": "header-secret"}
    post_data = "failed-body-secret"

    def __init__(self):
        self.sizes_called = 0
        self.response_called = 0

    def sizes(self):
        self.sizes_called += 1
        raise AssertionError("Request.sizes() must not be called for requestfailed")

    def response(self):
        self.response_called += 1
        raise AssertionError("Request.response() must not be called for requestfailed")


class _WebSocket(_Emitter):
    pass


def _performance_event(method, params):
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
    }


class _SeleniumDriver:
    def __init__(self, entries):
        self.entries = list(entries)
        self.cdp_commands = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        return {}

    def get_log(self, name):
        entries, self.entries = self.entries, []
        return entries

    def execute_script(self, script):
        return {}


class BrowserTrafficTests(unittest.TestCase):
    def test_playwright_counts_http_and_websocket_without_payload_capture(self):
        context = _Emitter()
        page = _Emitter()
        context.pages = [page]
        tracker = PlaywrightTrafficTracker(context)
        request = _Request()
        context.emit("request", request)
        context.emit("requestfinished", request)

        websocket = _WebSocket()
        page.emit("websocket", websocket)
        websocket.emit("framesent", "abc")
        websocket.emit("framereceived", b"1234")

        result = tracker.stop()
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["http_upload_bytes"], 12)
        self.assertEqual(result["http_download_bytes"], 28)
        self.assertEqual(result["websocket_upload_bytes"], 3)
        self.assertEqual(result["websocket_download_bytes"], 4)
        self.assertEqual(result["total_bytes"], 47)

    def test_playwright_logs_redacted_request_detail_without_body_or_headers(self):
        context = _Emitter()
        context.pages = []
        with patch("config.browser.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = PlaywrightTrafficTracker(context, label="detail")
            request = _DetailedRequest()
            context.emit("request", request)
            context.emit("requestfinished", request)
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 1)
        line = detail_lines[0]
        self.assertIn("xhr POST", line)
        self.assertIn("status=201", line)
        self.assertIn("token=<redacted>", line)
        self.assertNotIn("do-not-log", line)
        self.assertNotIn("request-body-secret", line)
        self.assertNotIn("response-header-secret", line)
        self.assertEqual(result["detail_recorded_count"], 1)

    def test_playwright_requestfailed_detail_skips_sync_request_apis(self):
        context = _Emitter()
        context.pages = []
        with patch("config.browser.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = PlaywrightTrafficTracker(context, label="failed")
            request = _FailedRequest()
            context.emit("request", request)
            context.emit("requestfailed", request)
            result = tracker.stop()

        self.assertEqual(request.sizes_called, 0)
        self.assertEqual(request.response_called, 0)
        self.assertEqual(result["failed_request_count"], 1)
        self.assertEqual(result["detail_recorded_count"], 1)

    def test_selenium_logs_redacted_status_cache_failure_and_unfinished_details(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "ok",
                    "type": "Script",
                    "request": {"method": "GET", "url": "https://example.test/app.js", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "ok",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "text/javascript"}},
                },
            ),
            _performance_event("Network.loadingFinished", {"requestId": "ok", "encodedDataLength": 40}),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "cached",
                    "type": "Image",
                    "request": {"method": "GET", "url": "https://cdn.example/cached.png", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "cached",
                    "response": {"status": 200, "statusText": "OK", "headers": {}, "fromMemoryCache": True},
                },
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "failed",
                    "type": "Fetch",
                    "request": {
                        "method": "POST",
                        "url": "https://example.test/api?secret=hidden",
                        "headers": {"authorization": "header-secret"},
                        "postData": "request-body-secret",
                    },
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "failed",
                    "response": {"status": 500, "statusText": "Error", "headers": {}},
                },
            ),
            _performance_event(
                "Network.loadingFailed",
                {"requestId": "failed", "errorText": "net::ERR_FAILED secret-error"},
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "unfinished",
                    "type": "XHR",
                    "request": {"method": "GET", "url": "https://example.test/pending", "headers": {}},
                },
            ),
            _performance_event("Network.dataReceived", {"requestId": "unfinished", "encodedDataLength": 7}),
        ]
        driver = _SeleniumDriver(entries)
        with patch("config.browser.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = SeleniumTrafficTracker(driver, label="detail")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 4)
        joined = "\n".join(detail_lines)
        self.assertIn("status=200", joined)
        self.assertIn("cache=hit", joined)
        self.assertIn("failed=1", joined)
        self.assertIn("unfinished=1", joined)
        self.assertIn("secret=<redacted>", joined)
        self.assertNotIn("hidden", joined)
        self.assertNotIn("header-secret", joined)
        self.assertNotIn("request-body-secret", joined)
        self.assertNotIn("secret-error", joined)
        self.assertEqual(result["detail_recorded_count"], 4)

    def test_selenium_resource_timing_fallback_logs_redacted_detail(self):
        class _FallbackDriver(_SeleniumDriver):
            def execute_script(self, script):
                return {
                    "href": "https://example.test/home?session=secret",
                    "timeOrigin": 10,
                    "resources": [
                        {
                            "name": "https://cdn.example/app.js?token=hidden",
                            "transferSize": 12,
                            "encodedBodySize": 9,
                        }
                    ],
                    "navigation": [],
                }

        driver = _FallbackDriver([])
        with patch("config.browser.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = SeleniumTrafficTracker(driver, label="fallback")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 1)
        self.assertIn("token=<redacted>", detail_lines[0])
        self.assertNotIn("hidden", detail_lines[0])
        self.assertEqual(result["detail_recorded_count"], 1)


if __name__ == "__main__":
    unittest.main()
