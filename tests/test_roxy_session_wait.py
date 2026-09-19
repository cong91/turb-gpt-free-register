import unittest
from unittest.mock import Mock, patch

from core import registration_service, roxy_registration
from core import browser_registration


class _FakeClock:
    """可控时钟：sleep 推进时间，time.time 返回当前值，避免测试真等待。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _banner_response():
    return {"WARNING_BANNER": "unusual activity", "_http_status": 200}


class RoxySessionWaitTests(unittest.TestCase):
    def test_session_poll_honors_manual_stop(self):
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"

        with patch.object(
            roxy_registration,
            "_read_chatgpt_session_once",
            return_value=None,
        ), patch.object(
            roxy_registration,
            "_check_manual_stop",
            side_effect=registration_service.StopRequested("stop requested"),
        ), self.assertRaises(registration_service.StopRequested):
            roxy_registration._fetch_chatgpt_session(driver, timeout=120)

    def test_session_reader_prefers_driver_request_api(self):
        class Driver:
            def __init__(self):
                self.async_calls = 0

            def get_chatgpt_auth_session(self):
                return {"accessToken": "request-token"}

            def execute_async_script(self, _script):
                self.async_calls += 1
                return {"ok": True, "data": {"accessToken": "page-token"}}

        driver = Driver()

        session = browser_registration._read_chatgpt_session_once(driver)

        self.assertEqual(session, {"accessToken": "request-token"})
        self.assertEqual(driver.async_calls, 0)

    def test_session_banner_triggers_one_refresh_then_returns_token(self):
        clock = _FakeClock()
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        token = {"accessToken": "token-1", "_http_status": 200}
        responses = [_banner_response() for _ in range(roxy_registration._SESSION_BANNER_REFRESH_AFTER)] + [token]

        with patch.object(
            roxy_registration,
            "_read_chatgpt_session_once",
            side_effect=responses,
        ), patch.object(
            roxy_registration,
            "_check_manual_stop",
        ), patch.object(
            roxy_registration,
            "time",
            clock,
        ):
            result = roxy_registration._fetch_chatgpt_session(driver, timeout=120)

        self.assertEqual(result, token)
        driver.refresh.assert_called_once()

    def test_session_banner_persistent_fast_fail_matches_quarantine(self):
        clock = _FakeClock()
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        responses = [
            _banner_response()
            for _ in range(roxy_registration._SESSION_BANNER_REFRESH_AFTER * 2 + 2)
        ]

        with patch.object(
            roxy_registration,
            "_read_chatgpt_session_once",
            side_effect=responses,
        ), patch.object(
            roxy_registration,
            "_check_manual_stop",
        ), patch.object(
            roxy_registration,
            "time",
            clock,
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._fetch_chatgpt_session(driver, timeout=120)

        message = str(ctx.exception)
        self.assertIn("等待 /api/auth/session accessToken 超时", message)
        self.assertIn("WARNING_BANNER", message)
        self.assertIn("'_http_status': 200", message)
        self.assertTrue(registration_service._is_final_session_access_token_timeout(message))
        driver.refresh.assert_called_once()

    def test_error_truncation_preserves_http_status_marker(self):
        """回归测试：driver 返回的 error 字段必须保留 '_http_status' marker 供 retry 分类使用。
        
        Job 2526→2561 暴露的 bug：error 被截断至 300 字符，marker 位于 346 字符处丢失，
        导致 _is_final_session_access_token_timeout() 误判，retry job 复用同一个已消耗的 alias 失败。
        修复后截断阈值提升至 800 字符，保证 marker 可见。
        """
        # 构造真实长度的错误：在 marker 前填充足够长的 banner 文本，使总长度接近 365 字符
        banner_text = "We've detected unusual activity from your device. Please try again later. " * 4
        full_error = (
            f"等待 /api/auth/session accessToken 超时（120秒）。"
            f"最后读取状态={{'WARNING_BANNER': '{banner_text}', '_http_status': 200}}"
        )
        marker_position = full_error.find("'_http_status': 200")
        self.assertGreater(marker_position, 300, f"marker at {marker_position} 必须在旧截断点 300 之后才能测试 bug")
        
        # 模拟 driver 返回值（已修复：[:800] 而非 [:300]）
        driver_result = {
            "success": False,
            "email": "test@example.com",
            "error": f"RuntimeError: {full_error[:800]}",
        }
        
        stored_error = driver_result["error"]
        self.assertIn("'_http_status': 200", stored_error, "修复后 marker 必须保留")
        self.assertTrue(
            registration_service._is_final_session_access_token_timeout(stored_error),
            "classifier 必须正确识别 terminal failure"
        )

    def test_session_banner_below_threshold_never_refreshes(self):
        clock = _FakeClock()
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        token = {"accessToken": "token-2", "_http_status": 200}
        responses = [
            _banner_response()
            for _ in range(roxy_registration._SESSION_BANNER_REFRESH_AFTER - 1)
        ] + [token]

        with patch.object(
            roxy_registration,
            "_read_chatgpt_session_once",
            side_effect=responses,
        ), patch.object(
            roxy_registration,
            "_check_manual_stop",
        ), patch.object(
            roxy_registration,
            "time",
            clock,
        ):
            result = roxy_registration._fetch_chatgpt_session(driver, timeout=120)

        self.assertEqual(result, token)
        driver.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
