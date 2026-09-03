import unittest
from unittest.mock import Mock, patch

from core import registration_service, roxy_registration
from core import browser_registration


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


if __name__ == "__main__":
    unittest.main()
