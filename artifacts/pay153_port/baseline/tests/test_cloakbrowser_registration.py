import unittest
from unittest.mock import patch

from core import cloakbrowser_registration


class _OtpStateDriver:
    current_url = "https://auth.openai.com/email-verification"

    def __init__(self):
        self.states = iter(
            [
                {"url": self.current_url, "inputs": [], "buttons": [], "errors": []},
                {
                    "url": self.current_url,
                    "inputs": [{"autocomplete": "one-time-code", "inputmode": "numeric"}],
                    "buttons": [],
                    "errors": [],
                },
            ]
        )

    def execute_script(self, _script):
        return next(self.states)


class _CallbackPerformanceDriver:
    def __init__(self):
        self.current_url = "https://auth.openai.com/about-you"

    def execute_script(self, _script):
        return [
            {"name": "https://auth.openai.com/about-you"},
            {"name": "https://auth.openai.com/authorize/continue?code=redacted&state=redacted"},
        ]


class CloakOtpReadinessTests(unittest.TestCase):
    def test_wait_for_otp_inputs_polls_until_input_is_mounted(self):
        wait_for_inputs = getattr(cloakbrowser_registration, "_wait_for_otp_inputs", None)
        self.assertTrue(callable(wait_for_inputs))
        if not callable(wait_for_inputs):
            return

        with patch.object(
            cloakbrowser_registration,
            "_browser_challenge_state",
            return_value={"is_challenge": False},
        ):
            state = wait_for_inputs(_OtpStateDriver(), timeout=1)

        self.assertEqual(state["inputs"][0]["autocomplete"], "one-time-code")

    def test_wait_for_otp_inputs_waits_out_browser_challenge(self):
        driver = object()
        with patch.object(
            cloakbrowser_registration,
            "_browser_challenge_state",
            side_effect=[
                {"is_challenge": True, "reason": "cloudflare"},
                {"is_challenge": False},
            ],
        ), patch.object(
            cloakbrowser_registration,
            "_wait_for_browser_challenge",
        ) as wait_for_challenge, patch.object(
            cloakbrowser_registration,
            "_email_otp_page_state",
            return_value={
                "inputs": [{"autocomplete": "one-time-code", "inputmode": "numeric"}],
            },
        ):
            state = cloakbrowser_registration._wait_for_otp_inputs(driver, timeout=10)

        self.assertEqual(state["inputs"][0]["autocomplete"], "one-time-code")
        wait_for_challenge.assert_called_once()
        self.assertGreater(wait_for_challenge.call_args.kwargs["timeout"], 0)

    def test_extracts_pending_signup_callback_from_performance_entries(self):
        extract_callback = getattr(
            cloakbrowser_registration, "_extract_signup_callback_url", None
        )
        self.assertTrue(callable(extract_callback))
        if not callable(extract_callback):
            return

        callback = extract_callback(_CallbackPerformanceDriver())

        self.assertEqual(
            callback,
            "https://auth.openai.com/authorize/continue?code=redacted&state=redacted",
        )

    def test_profile_submit_waits_for_transition_and_retries_only_while_still_on_form(self):
        wait_for_transition = getattr(
            cloakbrowser_registration, "_wait_for_profile_submit_transition", None
        )
        self.assertTrue(callable(wait_for_transition))
        if not callable(wait_for_transition):
            return

        profile = {
            "url": "https://auth.openai.com/about-you",
            "text": "",
            "inputs": [{"name": "name"}, {"name": "age"}],
            "widgets": [],
            "errors": [],
        }
        after_transition = {
            "url": "https://chatgpt.com/",
            "text": "",
            "inputs": [],
            "widgets": [],
            "errors": [],
        }
        clock = iter(range(20))
        with patch.object(
            cloakbrowser_registration,
            "_page_snapshot",
            side_effect=[profile, after_transition],
        ), patch.object(
            cloakbrowser_registration,
            "_click_if_enabled_submit",
            return_value=True,
        ) as submit, patch.object(
            cloakbrowser_registration.time,
            "monotonic",
            side_effect=lambda: next(clock),
        ), patch.object(cloakbrowser_registration.time, "sleep"):
            transitioned = wait_for_transition(
                _OtpStateDriver(), timeout=10, retry_interval=0
            )

        self.assertTrue(transitioned)
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
