import unittest
from unittest.mock import patch

import requests

from core.roxybrowser_client import RoxyBrowserClient


class RoxyBrowserCreateProfileTests(unittest.TestCase):
    def setUp(self):
        self.client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="t")

    @staticmethod
    def _ok_response():
        return {"code": 0, "data": {"id": "profile-1"}}

    def test_create_profile_succeeds_after_transient_connection_failures(self):
        with patch.object(
            self.client,
            "request",
            side_effect=[
                requests.ConnectionError("Connection refused"),
                requests.ConnectionError("Connection refused"),
                self._ok_response(),
            ],
        ) as request_mock, patch("core.roxybrowser_client.time.sleep") as sleep_mock:
            profile_id = self.client.create_profile()

        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(request_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_create_profile_gives_up_after_five_retryable_failures(self):
        with patch.object(
            self.client,
            "request",
            side_effect=requests.ConnectionError("WinError 10061"),
        ) as request_mock, patch("core.roxybrowser_client.time.sleep"), self.assertRaises(RuntimeError) as ctx:
            self.client.create_profile()

        self.assertIn("连续失败 5 次", str(ctx.exception))
        self.assertEqual(request_mock.call_count, 5)

    def test_create_profile_does_not_retry_ambiguous_response_timeout(self):
        with patch.object(
            self.client,
            "request",
            side_effect=requests.ReadTimeout("read timed out"),
        ) as request_mock, patch("core.roxybrowser_client.time.sleep") as sleep_mock, self.assertRaises(requests.ReadTimeout):
            self.client.create_profile()

        self.assertEqual(request_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_create_profile_retries_server_side_timeout_response(self):
        # Roxy server trả lỗi rõ ràng (axios timeout bên trong Roxy app): server
        # đã phản hồi là fail — không phải client-timeout ambiguous, phải chờ retry
        # (batch 2026-09-21 job 2919: 1 timeout = chết 1 job + đốt alias).
        with patch.object(
            self.client,
            "request",
            side_effect=[
                RuntimeError("Roxy API 返回失败 POST /browser/create: timeout of 15000ms exceeded"),
                self._ok_response(),
            ],
        ) as request_mock, patch("core.roxybrowser_client.time.sleep") as sleep_mock:
            profile_id = self.client.create_profile()

        self.assertEqual(profile_id, "profile-1")
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_create_profile_gives_up_after_five_server_timeouts(self):
        with patch.object(
            self.client,
            "request",
            side_effect=RuntimeError("Roxy API 返回失败 POST /browser/create: timeout of 15000ms exceeded"),
        ) as request_mock, patch("core.roxybrowser_client.time.sleep"), self.assertRaises(RuntimeError) as ctx:
            self.client.create_profile()

        self.assertIn("连续失败 5 次", str(ctx.exception))
        self.assertEqual(request_mock.call_count, 5)

    def test_create_profile_raises_immediately_on_non_retryable_error(self):
        with patch.object(
            self.client,
            "request",
            side_effect=RuntimeError("Roxy API 返回失败: token 无效"),
        ) as request_mock, patch("core.roxybrowser_client.time.sleep") as sleep_mock, self.assertRaises(RuntimeError):
            self.client.create_profile()

        self.assertEqual(request_mock.call_count, 1)
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
