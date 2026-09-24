import unittest
from unittest import mock

from core import registration_profile_utils as utils


class RegistrationProfileUtilsTests(unittest.TestCase):
    def test_profile_submission_failure_message_is_the_contract(self):
        # registration_service 的 alias matcher / poison check 都 pattern-match
        # "about-you 提交失败" 前缀 —— message 契约必须从这里出。
        self.assertEqual(
            utils.profile_submission_failure_message("user_already_exists"),
            "about-you 提交失败：user_already_exists",
        )

    def test_profile_submission_error_requires_profile_url_gate(self):
        # URL 已离开 about-you/profile —— 同样文本不算提交错误。
        snapshot = {
            "url": "https://chatgpt.com/",
            "text": "This email is not supported.",
            "errors": [],
        }

        self.assertIsNone(utils.profile_submission_error(snapshot))

    def test_profile_submission_error_returns_marker_slice(self):
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "Oops, an error occurred! error_code: user_already_exists",
            "errors": [],
        }

        self.assertEqual(
            utils.profile_submission_error(snapshot),
            "user_already_exists",
        )

    def test_poll_returns_error_when_classifier_hits_at_tick_n(self):
        calls = {"n": 0}

        def fake_snapshot():
            calls["n"] += 1
            if calls["n"] < 3:
                return {"url": "https://auth.openai.com/about-you", "text": "", "errors": []}
            return {
                "url": "https://auth.openai.com/about-you",
                "text": "error_code: user_already_exists",
                "errors": [],
            }

        with mock.patch("core.registration_profile_utils.time.sleep"):
            result = utils.poll_profile_submission_error(
                fake_snapshot,
                duration=5.0,
                interval=0.01,
                left_profile=lambda _snapshot: False,
            )

        self.assertEqual(calls["n"], 3)
        self.assertEqual(result, "user_already_exists")

    def test_poll_returns_none_when_left_profile(self):
        events = []

        def fake_snapshot():
            events.append("snapshot")
            return {"url": "https://auth.openai.com/about-you", "text": "", "errors": []}

        with mock.patch(
            "core.registration_profile_utils.time.sleep",
            side_effect=lambda _seconds: events.append("sleep"),
        ):
            result = utils.poll_profile_submission_error(
                fake_snapshot,
                duration=5.0,
                interval=0.01,
                left_profile=lambda _snapshot: True,
            )

        self.assertIsNone(result)
        # sleep-first 语义：先 sleep 再 snapshot（roxy 循环 verbatim）。
        self.assertEqual(events, ["sleep", "snapshot"])

    def test_poll_returns_none_when_duration_exhausted(self):
        def fake_snapshot():
            return {"url": "https://auth.openai.com/about-you", "text": "", "errors": []}

        with mock.patch("core.registration_profile_utils.time.sleep"):
            result = utils.poll_profile_submission_error(
                fake_snapshot,
                duration=0.03,
                interval=0.01,
                left_profile=lambda _snapshot: False,
            )

        self.assertIsNone(result)

    def test_is_unsupported_email_error(self):
        self.assertTrue(
            utils.is_unsupported_email_error(
                "RuntimeError: about-you 提交失败：This email is not supported."
            )
        )
        # 缺少 message 契约前缀 → 不算。
        self.assertFalse(
            utils.is_unsupported_email_error("some failure: this email is not supported")
        )
        # already-exists 是另一类，不算 unsupported email。
        self.assertFalse(
            utils.is_unsupported_email_error(
                "RuntimeError: about-you 提交失败：user_already_exists"
            )
        )

    def test_is_already_exists_error_mirrors_service_matcher(self):
        self.assertTrue(
            utils.is_already_exists_error(
                "RuntimeError: about-you 提交失败：An account already exists for this email address or phone number."
            )
        )
        self.assertTrue(utils.is_already_exists_error("user_already_exists"))
        self.assertTrue(utils.is_already_exists_error("Please log in instead."))
        self.assertFalse(utils.is_already_exists_error("This email is not supported."))
        self.assertFalse(utils.is_already_exists_error(""))


if __name__ == "__main__":
    unittest.main()
