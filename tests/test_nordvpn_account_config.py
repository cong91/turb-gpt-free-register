"""Configuration exposure tests for NordVPN access-token proxies."""
import unittest

from config import _RELOADABLE_SUBMODULES
from config.env_loader import SECRET_ENV_KEYS
from webui import config_editor


class NordVPNAccountConfigTests(unittest.TestCase):
    def test_access_token_is_registered_as_secret(self):
        self.assertIn("NORDVPN_ACCESS_TOKEN", SECRET_ENV_KEYS)

    def test_webui_exposes_wireguard_fields(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertIn("NORDVPN_WG_ENABLED", fields)
        self.assertIn("NORDVPN_WG_COUNTRY_FILTER", fields)
        self.assertIn("NORDVPN_WG_WIREPROXY_EXE", fields)
        self.assertIn("NORDVPN_WG_AUTO_DOWNLOAD", fields)
        self.assertIn("NORDVPN_API_BASE", fields)
        self.assertTrue(fields["NORDVPN_ACCESS_TOKEN"]["secret"])
        self.assertEqual(fields["NORDVPN_ACCESS_TOKEN"]["storage"], "sqlite")

    def test_modules_are_hot_reloadable(self):
        self.assertIn("config.nordvpn_account", _RELOADABLE_SUBMODULES)
        self.assertIn("config.nordvpn_wireguard", _RELOADABLE_SUBMODULES)


if __name__ == "__main__":
    unittest.main()
