# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import roxy_registration


class RoxyRegistrationRetryTests(unittest.TestCase):
    @patch("core.roxy_registration._assert_not_external_idp")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._wait_email_submit_next_state", side_effect=["unknown", "otp"])
    @patch("core.roxy_registration._submit_email_step")
    @patch("core.roxy_registration._email_input_value_state")
    def test_retry_reloads_login_page_after_spa_clears_email_inputs(
        self,
        email_state,
        _submit,
        _wait_state,
        _human_delay,
        maybe_accept,
        assert_not_external,
    ):
        driver = Mock()
        email = "user@example.com"
        email_state.side_effect = [
            {"url": "https://chatgpt.com/auth/login", "inputs": [{"value": email}]},
            {"url": "https://chatgpt.com/auth/login", "inputs": []},
            {"url": "https://chatgpt.com/auth/login", "inputs": [{"value": email}]},
        ]

        def type_email(_driver, _email, timeout):
            if type_email.calls and not driver.get.called:
                raise RuntimeError("stale SPA login DOM")
            type_email.calls += 1

        type_email.calls = 0
        with patch("core.roxy_registration._type_email_address", side_effect=type_email):
            result = roxy_registration._submit_email_and_wait_next(driver, email, attempts=2)

        self.assertEqual(result, "otp")
        driver.get.assert_called_once_with("https://chatgpt.com/auth/login")
        maybe_accept.assert_called_once_with(driver)
        assert_not_external.assert_called_once_with(driver, "retry login page")
        self.assertEqual(type_email.calls, 2)


if __name__ == "__main__":
    unittest.main()
