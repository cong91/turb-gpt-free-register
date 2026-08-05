import unittest

from core import roxy_registration


class RoxyProfileErrorTests(unittest.TestCase):
    def test_terms_account_creation_error_is_terminal(self):
        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "利用規約のため、お客様のアカウントを作成できません。",
            "errors": [],
        }

        error = roxy_registration._profile_submission_error(snapshot)

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
            roxy_registration._profile_submission_error(snapshot),
            "Cannot create your account due to the terms of use.",
        )

        snapshot = {
            "url": "https://auth.openai.com/about-you",
            "text": "名前\n年齢",
            "errors": [],
        }

        self.assertIsNone(roxy_registration._profile_submission_error(snapshot))


if __name__ == "__main__":
    unittest.main()
