import unittest
from types import SimpleNamespace

from core import browser_registry


class BrowserRegistryTests(unittest.TestCase):
    def test_all_requested_aliases_resolve_to_canonical_names(self):
        expected = {
            "roxy": "roxy",
            "roxybrowser": "roxy",
            "fingerprint": "roxy",
            "browser": "roxy",
            "cloak": "cloak",
            "cloakbrowser": "cloak",
            "browser_use": "browser_use",
            "browseruse": "browser_use",
            "browser-use": "browser_use",
            "bu": "browser_use",
            "skyvern": "skyvern",
            "sv": "skyvern",
        }
        self.assertEqual(set(browser_registry.ALIASES), set(expected))
        for alias, canonical in expected.items():
            with self.subTest(alias=alias):
                self.assertEqual(browser_registry.normalize_driver(alias), canonical)

    def test_roxy_is_the_default_and_protocol_is_not_a_browser_alias(self):
        self.assertEqual(browser_registry.DEFAULT_DRIVER, "roxy")
        self.assertNotIn("protocol", browser_registry.ALIASES)
        self.assertNotIn("api", browser_registry.ALIASES)
        self.assertNotIn("http", browser_registry.ALIASES)
        self.assertEqual(browser_registry.resolve_registration_driver(SimpleNamespace()), "roxy")

    def test_configured_driver_is_case_and_whitespace_insensitive(self):
        config = SimpleNamespace(REGISTRATION_DRIVER="  CloAkBrOwSeR ")
        self.assertEqual(browser_registry.resolve_registration_driver(config), "cloak")

    def test_live_browser_predicate_uses_canonical_registry(self):
        self.assertTrue(browser_registry.is_live_browser_driver("browser-use"))
        self.assertTrue(browser_registry.is_live_browser_driver("skyvern"))
        self.assertFalse(browser_registry.is_live_browser_driver("protocol"))

    def test_each_driver_has_lazy_runner_and_profile_lifecycle(self):
        self.assertEqual(
            set(browser_registry.DRIVER_SPECS),
            {"protocol", "roxy", "cloak", "browser_use", "skyvern"},
        )
        for driver, spec in browser_registry.DRIVER_SPECS.items():
            with self.subTest(driver=driver):
                self.assertIsInstance(spec.runner, str)
                if driver != "protocol":
                    self.assertTrue(spec.profile_opener)

        message = browser_registry.unsupported_driver_message("unknown")
        self.assertIn("不支持的 REGISTRATION_DRIVER='unknown'", message)
        self.assertIn("protocol / roxy / cloak / browser_use / skyvern", message)


if __name__ == "__main__":
    unittest.main()
