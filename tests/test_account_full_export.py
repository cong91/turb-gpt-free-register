import json
import unittest
from unittest import mock

import core.db as db


class AccountFullExportTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "email": "a@b.com",
            "email_source": "gptmail",
            "extra_json": json.dumps({"registration_password": "Pass123"}),
            "totp_secret": "ABCDEF123",
        }
        row.update(overrides)
        return row

    def test_generic_api_prefers_stored_code_url(self):
        with mock.patch.object(db, "get_generic_api_email_by_email", return_value={"code_url": "http://pool.test/code"}), mock.patch("config.email.EMAIL_API_BASE_URL", "https://base.test/api"):
            self.assertEqual(db.resolve_email_api_link("a@b.com", "generic_api"), "http://pool.test/code")

    def test_generic_api_uses_configured_base_when_store_has_no_url(self):
        with mock.patch.object(db, "get_generic_api_email_by_email", return_value=None), mock.patch("config.email.EMAIL_API_BASE_URL", "https://base.test/api/"):
            self.assertEqual(db.resolve_email_api_link("a@b.com", "generic_api"), "https://base.test/api/messages?mailbox=a@b.com")

    def test_generic_api_keeps_source_without_configured_base(self):
        with mock.patch.object(db, "get_generic_api_email_by_email", return_value=None), mock.patch("config.email.EMAIL_API_BASE_URL", ""):
            self.assertEqual(db.resolve_email_api_link("a@b.com", "generic_api"), "generic_api")

    def test_other_sources_are_unchanged(self):
        self.assertEqual(db.resolve_email_api_link("a@b.com", "gptmail"), "gptmail")

    def test_full_export_uses_resolved_provider_url(self):
        with mock.patch.object(db, "get_generic_api_email_by_email", return_value={"code_url": "http://pool.test/code"}):
            self.assertEqual(db._account_full_export_line(self._row(email_source="generic_api")), "a@b.com---http://pool.test/code---Pass123---https://2fa.run/----2FA:ABCDEF123")


if __name__ == "__main__":
    unittest.main()
