import unittest
from types import SimpleNamespace

from webui.email_source_validation import validate_email_sources


class BambooMmoConfigTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "BAMBOOMMO_API_BASE": "https://api.bamboommo.com",
            "BAMBOOMMO_API_KEY": "key",
            "BAMBOOMMO_SERVER": 2,
            "BAMBOOMMO_MAIL_TYPE": "GM",
            "BAMBOOMMO_SERVICE": "OP",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_valid_bamboommo_configuration(self):
        self.assertIsNone(validate_email_sources(["bamboommo"], self._config()))

    def test_config_package_reexports_bamboommo_fields(self):
        import config

        for name in (
            "BAMBOOMMO_API_BASE",
            "BAMBOOMMO_API_KEY",
            "BAMBOOMMO_SERVER",
            "BAMBOOMMO_MAIL_TYPE",
            "BAMBOOMMO_SERVICE",
        ):
            self.assertTrue(hasattr(config, name))

    def test_requires_api_key(self):
        error = validate_email_sources(["bamboommo"], self._config(BAMBOOMMO_API_KEY=""))
        self.assertIn("BAMBOOMMO_API_KEY", error)

    def test_restricts_server_to_one_or_two(self):
        error = validate_email_sources(["bamboommo"], self._config(BAMBOOMMO_SERVER=3))
        self.assertIn("server", error.lower())


if __name__ == "__main__":
    unittest.main()
