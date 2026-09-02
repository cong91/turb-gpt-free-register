import unittest

from config import browser


class BrowserLocaleTests(unittest.TestCase):
    def test_vietnamese_locale_profile_is_supported(self):
        profile = browser._build_locale_from_geo({"country": "VN", "timezone": "Asia/Ho_Chi_Minh"})

        self.assertEqual(profile["locale_profile"], "vi")
        self.assertEqual(profile["navigator_language"], "vi-VN")
        self.assertEqual(profile["timezone_iana"], "Asia/Ho_Chi_Minh")

    def test_configured_vietnamese_profile_does_not_raise(self):
        profile = browser.build_browser_environment()

        self.assertTrue(profile["navigator_language"])
        self.assertIn(profile["navigator_language"], profile["navigator_languages"])


if __name__ == "__main__":
    unittest.main()
