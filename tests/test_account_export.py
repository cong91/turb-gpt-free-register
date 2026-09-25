import json
import unittest
from unittest import mock

import core.db as db


class AccountExportTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "email": "a@b.com",
            "email_source": "gptmail",
            "extra_json": json.dumps({"registration_password": "Pass123"}),
            "totp_secret": "ABCDEF123",
        }
        row.update(overrides)
        return row

    def test_basic_format(self):
        self.assertEqual(db._account_full_export_line(self._row()), "a@b.com---gptmail---Pass123---https://2fa.run/----2FA:ABCDEF123")

    def test_missing_password_and_totp_preserve_fields(self):
        row = self._row(extra_json=json.dumps({}), totp_secret="")
        self.assertEqual(db._account_full_export_line(row), "a@b.com---gptmail------https://2fa.run/----2FA:")

    def test_generic_api_resolves_stored_code_url(self):
        with mock.patch.object(db, "get_generic_api_email_by_email", return_value={"code_url": "http://pool.test/code"}):
            self.assertIn("---http://pool.test/code---", db._account_full_export_line(self._row(email_source="generic_api")))


if __name__ == "__main__":
    unittest.main()
