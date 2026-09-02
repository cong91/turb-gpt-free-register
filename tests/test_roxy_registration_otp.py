import unittest
from unittest.mock import Mock, patch

from core import roxy_registration


class RoxyRegistrationOtpTests(unittest.TestCase):
    @patch("core.roxy_registration._wait_after_email_otp_submit", side_effect=["accepted"])
    @patch("core.roxy_registration._click_continue")
    @patch(
        "core.roxy_registration._type_otp",
        side_effect=[RuntimeError("找不到 OTP 输入框"), None],
    )
    @patch("core.roxy_registration._clear_otp_inputs")
    @patch("core.roxy_registration._click_resend_email_otp")
    @patch("core.roxy_registration.wait_for_otp", return_value="654321")
    @patch("core.roxy_registration.time.time", side_effect=[100.0, 200.0])
    def test_missing_otp_input_resends_and_fetches_a_new_code(
        self,
        _time,
        wait_for_otp,
        resend,
        _clear,
        type_otp,
        _continue,
        _wait_submit,
    ):
        driver = Mock()

        with patch("core.roxy_registration.human_delay"):
            roxy_registration._complete_email_otp(
                driver,
                "user@example.com",
                otp_after_ts=50.0,
                otp_code="123456",
                max_attempts=2,
            )

        resend.assert_called_once_with(driver, timeout=25)
        wait_for_otp.assert_called_once_with(
            "user@example.com",
            after_ts=100.0,
            before_code="123456",
            stage="registration_email_otp",
        )
        self.assertEqual(type_otp.call_args_list[0].args[1], "123456")

    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    @patch("core.roxy_registration._click_resend_email_otp")
    @patch(
        "core.roxy_registration.wait_for_otp",
        side_effect=[RuntimeError("stale/timeout"), "222222"],
    )
    @patch("core.roxy_registration.time.time", side_effect=[100.0, 200.0])
    def test_wait_failure_resends_without_fallback_to_epoch_zero(
        self,
        _time,
        wait_for_otp,
        resend,
        _clear,
        _type_otp,
        _continue,
        _wait_submit,
    ):
        driver = Mock()

        with patch("core.roxy_registration.human_delay"):
            roxy_registration._complete_email_otp(
                driver,
                "user@example.com",
                otp_after_ts=50.0,
                max_attempts=2,
            )

        resend.assert_called_once_with(driver, timeout=25)
        self.assertEqual(
            [call.kwargs["after_ts"] for call in wait_for_otp.call_args_list],
            [50.0, 100.0],
        )
        self.assertEqual(
            [call.kwargs["stage"] for call in wait_for_otp.call_args_list],
            ["registration_email_otp", "registration_email_otp"],
        )

    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="invalid")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    @patch("core.roxy_registration._click_resend_email_otp")
    @patch("core.roxy_registration.wait_for_otp", return_value="222222")
    @patch("core.roxy_registration.time.time", side_effect=[100.0, 200.0])
    def test_retry_uses_submitted_code_as_stale_guard(
        self,
        _time,
        wait_for_otp,
        resend,
        _clear,
        _type_otp,
        _continue,
        _wait_submit,
    ):
        driver = Mock()
        _wait_submit.side_effect = ["invalid", "accepted"]

        with patch("core.roxy_registration.human_delay"):
            roxy_registration._complete_email_otp(
                driver,
                "user@example.com",
                otp_after_ts=50.0,
                otp_code="111111",
                max_attempts=2,
            )

        resend.assert_called_once_with(driver, timeout=25)
        self.assertEqual(wait_for_otp.call_args.kwargs["before_code"], "111111")
        self.assertEqual(wait_for_otp.call_args.kwargs["stage"], "registration_email_otp")


if __name__ == "__main__":
    unittest.main()
