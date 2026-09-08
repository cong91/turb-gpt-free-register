import unittest
from unittest.mock import patch

from config import browser
from core import cloakbrowser_driver


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

    def test_exit_ip_does_not_change_fixed_locale(self):
        profile = browser._build_locale_from_geo({"country": "ID", "timezone": "Asia/Jakarta"})

        self.assertEqual(profile["locale_profile"], browser.BROWSER_LOCALE_PROFILE)
        self.assertEqual(profile["navigator_language"], "vi-VN")
        self.assertEqual(profile["timezone_iana"], "Asia/Ho_Chi_Minh")
        self.assertEqual(profile["navigator_languages"], ["vi-VN"])
        self.assertEqual(profile["accept_language"], "vi-VN")

    def test_cloak_geoip_does_not_lookup_locale_in_fixed_mode(self):
        with patch.object(
            cloakbrowser_driver._cfg,
            "CLOAK_GEOIP",
            True,
        ), patch.object(
            cloakbrowser_driver,
            "_detect_cloak_exit_geo",
        ) as detect_geo:
            options = cloakbrowser_driver._build_cloak_locale_options("socks5://127.0.0.1:25000")

        self.assertEqual(options["locale"], "vi-VN")
        self.assertEqual(options["timezone"], "Asia/Ho_Chi_Minh")
        self.assertEqual(options["accept_language"], "vi-VN")
        self.assertNotIn("geo", options)
        detect_geo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
