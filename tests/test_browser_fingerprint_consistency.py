import unittest
from pathlib import Path

from config import browser
from core import codex_agent


class BrowserFingerprintConsistencyTests(unittest.TestCase):
    def test_runtime_uses_the_supported_explicit_chrome_target(self):
        self.assertEqual(browser.IMPERSONATE, "chrome146")
        self.assertEqual(browser.CHROME_MAJOR, "146")

    def test_protocol_profile_matches_explicit_curl_target(self):
        target_major = browser.IMPERSONATE.removeprefix("chrome")
        profile = browser.build_browser_environment()

        self.assertTrue(target_major.isdigit())
        self.assertEqual(browser.CHROME_MAJOR, target_major)
        self.assertEqual(profile["chrome_major"], target_major)
        self.assertEqual(profile["chrome_full_version"], "146.0.7680.177")
        self.assertIn("Chrome/146.0.0.0", profile["user_agent"])
        self.assertIn(f'"Google Chrome";v="{target_major}"', profile["sec_ch_ua"])

    def test_validator_rejects_profile_with_mismatched_runtime_major(self):
        profile = browser.build_browser_environment()
        profile["chrome_major"] = "150"
        profile["chrome_full_version"] = "150.0.0.0"
        profile["user_agent"] = profile["user_agent"].replace("146.0.0.0", "150.0.0.0")

        issues = browser.validate_browser_profile(profile)

        self.assertTrue(any("IMPERSONATE" in issue for issue in issues))

    def test_standalone_sentinel_defaults_match_runtime_target(self):
        runner = Path(__file__).resolve().parents[1] / "sentinel" / "sentinel-runner.js"
        source = runner.read_text(encoding="utf-8")

        self.assertIn("Chrome/146.0.0.0 Safari/537.36", source)
        self.assertIn('process.env.SENTINEL_CHROME_MAJOR, "146"', source)
        self.assertIn("availHeight: Math.max(0, height - 48)", source)
        self.assertIn("wow64: false", source)
        self.assertNotIn("Chrome/149.0.0.0 Safari/537.36", source)

    def test_codex_agent_uses_explicit_target_matching_user_agent(self):
        self.assertEqual(codex_agent.IMPERSONATE, f"chrome{codex_agent.CHROME_VERSION}")


if __name__ == "__main__":
    unittest.main()
