import unittest
from unittest.mock import patch

from core import generic_api_mail_client, outlook_client


class RuntimeStateSourceTests(unittest.TestCase):
    def test_outlook_claim_does_not_auto_import_txt_export(self):
        row = {
            "id": 1,
            "email": "account@example.test",
            "password": "password",
            "client_id": "client",
            "refresh_token": "refresh",
        }
        with (
            patch("core.db.claim_next_outlook", return_value=row),
            patch.object(outlook_client, "import_outlook_from_file") as importer,
        ):
            account = outlook_client.pick_account()

        self.assertEqual(account.email, row["email"])
        importer.assert_not_called()

    def test_generic_api_claim_does_not_auto_import_txt_export(self):
        row = {
            "id": 1,
            "email": "account@example.test",
            "code_url": "https://example.test/code",
        }
        with (
            patch("core.db.claim_next_generic_api_email", return_value=row),
            patch.object(generic_api_mail_client, "import_from_file") as importer,
        ):
            account = generic_api_mail_client.pick_account()

        self.assertEqual(account.email, row["email"])
        importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
