import unittest
from unittest.mock import patch

from core import browser_registration


class _OtpElement:
    def __init__(self):
        self.value = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        attrs = {
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "type": "text",
        }
        return attrs.get(name)


class _OtpDriver:
    def __init__(self, element):
        self.element = element

    def find_elements(self, _by, _selector):
        return [self.element]


class BrowserRegistrationOtpTests(unittest.TestCase):
    def test_type_otp_retries_when_dom_value_drops_a_digit(self):
        element = _OtpElement()
        driver = _OtpDriver(element)
        states = iter(("12283", "122783"))

        def type_with_transient_drop(_driver, target, value, *, clear=True):
            target.value = next(states)

        def otp_state(_driver):
            return {
                "inputs": [{
                    "autocomplete": "one-time-code",
                    "inputmode": "numeric",
                    "type": "text",
                    "value": element.value,
                }],
            }

        with patch.object(browser_registration, "_human_type_text", side_effect=type_with_transient_drop) as type_text, \
            patch.object(browser_registration, "_email_otp_page_state", side_effect=otp_state), \
            patch.object(browser_registration.time, "sleep"):
            browser_registration._type_otp(driver, "122783")

        self.assertEqual(element.value, "122783")
        self.assertEqual(type_text.call_count, 2)

    def test_failed_otp_input_is_not_used_as_stale_guard_on_retry(self):
        driver = _OtpDriver(_OtpElement())
        with patch.object(
            browser_registration,
            "_type_otp",
            side_effect=[RuntimeError("input mismatch"), None],
        ), patch.object(browser_registration, "_clear_otp_inputs"), \
            patch.object(browser_registration, "_click_resend_email_otp"), \
            patch.object(browser_registration, "_click_continue"), \
            patch.object(browser_registration, "_wait_after_email_otp_submit", return_value="accepted"), \
            patch.object(browser_registration, "wait_for_otp", return_value="222222") as wait_for_otp, \
            patch.object(browser_registration, "snapshot_verification_code", return_value=None), \
            patch.object(browser_registration, "human_delay"), \
            patch.object(browser_registration.time, "time", side_effect=(100.0, 200.0)):
            browser_registration._complete_email_otp(
                driver,
                "user@example.com",
                otp_after_ts=50.0,
                otp_code="111111",
                max_attempts=2,
            )

        self.assertEqual(wait_for_otp.call_args.kwargs["after_ts"], 100.0)
        self.assertNotIn("before_code", wait_for_otp.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
