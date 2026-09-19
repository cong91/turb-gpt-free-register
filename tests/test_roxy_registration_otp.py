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

        with patch("core.roxy_registration.human_delay"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ):
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

        with patch("core.roxy_registration.human_delay"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ):
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

        with patch("core.roxy_registration.human_delay"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ):
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

    @patch("core.roxy_registration._wait_after_email_otp_submit")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    @patch("core.roxy_registration._click_resend_email_otp")
    @patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp")
    @patch("core.roxy_registration._fill_password_page_if_present", return_value="pw")
    @patch("core.roxy_registration._click_continue_with_password_link")
    @patch("core.roxy_registration._reset_login_page_for_retry")
    @patch("core.roxy_registration._is_chrome_error_page", return_value=True)
    @patch("core.roxy_registration.wait_for_otp", return_value="222222")
    @patch("core.roxy_registration.time.time", side_effect=[100.0, 200.0])
    def test_otp_submit_lands_on_500_page_restarts_flow_instead_of_failing(
        self,
        _time,
        wait_for_otp,
        _is_error,
        _reset,
        _click_pwd_link,
        _fill_pwd,
        _resubmit,
        resend,
        _clear,
        _type_otp,
        _continue,
        _wait_submit,
    ):
        """Job 2423: sau khi submit OTP, auth.openai.com trả HTTP 500 (chrome-error://).

        Trước fix: _click_resend_email_otp tìm nút resend trên trang lỗi 25s rồi
        RuntimeError làm chết cả job. Sau fix: phát hiện trang lỗi, mở lại login
        page, submit lại email để trigger OTP mới và tiếp tục vòng retry.
        """
        driver = Mock()
        _wait_submit.side_effect = ["invalid", "accepted"]

        with patch("core.roxy_registration.human_delay"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ):
            roxy_registration._complete_email_otp(
                driver,
                "user@example.com",
                otp_after_ts=50.0,
                otp_code="111111",
                max_attempts=2,
            )

        resend.assert_not_called()
        _reset.assert_called_once_with(driver)
        _resubmit.assert_called_once()
        self.assertEqual(_resubmit.call_args.args[1], "user@example.com")
        _click_pwd_link.assert_called_once_with(driver)
        self.assertEqual(wait_for_otp.call_args.kwargs["after_ts"], 100.0)

    def test_is_chrome_error_page_detects_url_and_http_500_text(self):
        driver = Mock()
        driver.current_url = "chrome-error://chromewebdata/"
        self.assertTrue(roxy_registration._is_chrome_error_page(driver))

        driver = Mock()
        driver.current_url = "https://auth.openai.com/log-in/otp"
        state = {
            "text": "This page isn’t working auth.openai.com is currently unable to handle this request. HTTP ERROR 500",
            "errors": ["HTTP ERROR 500"],
        }
        with patch("core.roxy_registration._email_otp_page_state", return_value=state):
            self.assertTrue(roxy_registration._is_chrome_error_page(driver))

        driver = Mock()
        driver.current_url = "https://auth.openai.com/log-in/otp"
        state = {"text": "Enter code", "errors": []}
        with patch("core.roxy_registration._email_otp_page_state", return_value=state):
            self.assertFalse(roxy_registration._is_chrome_error_page(driver))


if __name__ == "__main__":
    unittest.main()
