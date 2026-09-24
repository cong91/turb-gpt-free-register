import unittest

from core import registration_profile_utils


class RoxyProfileErrorTests(unittest.TestCase):
    def test_terms_account_creation_error_is_terminal(self):
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "利用規約のため、お客様のアカウントを作成できません。",
            "errors": [],
        }

        error = registration_profile_utils.profile_submission_error(snapshot)

        self.assertEqual(
            error,
            "利用規約のため、お客様のアカウントを作成できません。",
        )

    def test_structured_profile_error_is_detected(self):
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "",
            "errors": ["Cannot create your account due to the terms of use."],
        }

        self.assertEqual(
            registration_profile_utils.profile_submission_error(snapshot),
            "Cannot create your account due to the terms of use.",
        )

        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "名前\n年齢",
            "errors": [],
        }

        self.assertIsNone(registration_profile_utils.profile_submission_error(snapshot))

    def test_unsupported_email_error_is_terminal(self):
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "This email is not supported.",
            "errors": [],
        }

        self.assertEqual(
            registration_profile_utils.profile_submission_error(snapshot),
            "This email is not supported.",
        )

    def test_user_already_exists_error_is_terminal(self):
        # Trang lỗi OpenAI (batch 2026-09-21): "An account already exists for
        # this email address or phone number. Please log in instead." +
        # error_code: user_already_exists — alias đã có account server-side,
        # phải bỏ alias này và lấy alias mới.
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": (
                "Oops, an error occurred! An account already exists for this "
                "email address or phone number. Please log in instead. "
                "error_code: user_already_exists"
            ),
            "errors": [],
        }

        error = registration_profile_utils.profile_submission_error(snapshot)

        self.assertIsNotNone(error)
        self.assertIn("already exists for this email", error)


if __name__ == "__main__":
    unittest.main()
