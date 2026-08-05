import unittest
from unittest.mock import Mock, patch

from core import registration_service, roxy_registration


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
        ):
            with self.assertRaises(registration_service.StopRequested):
                roxy_registration._fetch_chatgpt_session(driver, timeout=120)


if __name__ == "__main__":
    unittest.main()
