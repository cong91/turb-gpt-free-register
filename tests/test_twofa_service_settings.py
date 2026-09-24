import unittest
from unittest.mock import patch

from core import twofa_service


class TwofaServiceSettingsTests(unittest.TestCase):
    def test_queue_settings_exposes_bounded_import_settings(self):
        settings = twofa_service.queue_settings()
        self.assertGreaterEqual(settings["workers"], 1)
        self.assertLessEqual(settings["workers"], 16)
        self.assertGreaterEqual(settings["queue_limit"], settings["workers"])

    def test_saved_mode_prefers_explicit_proxy_and_preserves_lane(self):
        with patch.object(twofa_service, "resolve_rotating_proxy", return_value="http://pool:8080") as resolve:
            real, leased = twofa_service._resolve_twofa_proxy("http://saved:8080", proxy_lane_id=3)
        self.assertEqual(real, "http://pool:8080")
        self.assertIsNone(leased)
        resolve.assert_called_once_with("http://saved:8080", scope=twofa_service.TWOFA_SETUP_PROXY_SCOPE, lane_id=3)

    def test_pool_mode_forces_fresh_proxy_and_returns_lease(self):
        with patch.object(twofa_service._twofa_cfg, "TWOFA_PROXY_MODE", "pool"), patch.object(
            twofa_service, "resolve_rotating_proxy", return_value="http://fresh:8080"
        ) as resolve:
            real, leased = twofa_service._resolve_twofa_proxy("http://saved:8080", proxy_lane_id=4)
        self.assertEqual(real, "http://fresh:8080")
        self.assertEqual(leased, "http://fresh:8080")
        resolve.assert_called_once_with(None, scope=twofa_service.TWOFA_SETUP_PROXY_SCOPE, lane_id=4)

    def test_invalid_proxy_mode_is_rejected(self):
        with patch.object(twofa_service._twofa_cfg, "TWOFA_PROXY_MODE", "invalid"):
            with self.assertRaises(ValueError):
                twofa_service._resolve_twofa_proxy(None)


if __name__ == "__main__":
    unittest.main()
