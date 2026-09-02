import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, free_plus_export
from core.account_locale import derive_account_locale
from webui.app import create_app


class AccountLocaleTests(unittest.TestCase):
    def test_derives_account_locale_from_exit_geo(self):
        result = derive_account_locale(geo={"ip": "203.0.113.10", "country": "VN"})

        self.assertEqual(result["account_locale"], "vi")
        self.assertEqual(result["account_country"], "VN")
        self.assertEqual(result["account_locale_source"], "geoip")

    def test_falls_back_to_cloud_proxy_country_without_calling_it_proxy_classification(self):
        result = derive_account_locale(proxy_country_code="us")

        self.assertEqual(result["account_locale"], "us")
        self.assertEqual(result["account_country"], "US")
        self.assertEqual(result["account_locale_source"], "proxy_country")

    def test_does_not_classify_from_default_browser_language_without_ip_country(self):
        result = derive_account_locale(extra={"browser_profile": {"locale_profile": "jp", "geo": {}}})

        self.assertEqual(result["account_locale"], "")
        self.assertEqual(result["account_country"], "")

    @patch("core.db.insert_account", return_value=11)
    def test_checkpoint_persists_geo_locale_on_account(self, insert_account):
        from core.account_export import checkpoint_account_data

        with patch("core.email_provider.mark_email_consumed"):
            checkpoint_account_data(
                email="geo@example.com",
                access_token="token-geo",
                extra={"browser_profile": {"geo": {"country": "JP"}}},
            )

        self.assertEqual(insert_account.call_args.kwargs["account_locale"], "jp")
        self.assertEqual(insert_account.call_args.kwargs["account_country"], "JP")
        self.assertEqual(insert_account.call_args.kwargs["account_locale_source"], "geoip")

    def test_accounts_are_persisted_and_filtered_by_account_locale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            patches = [
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_OUTLOOK_JSON", root / "outlook.json"),
                patch.object(db, "_OUTLOOK_TXT", root / "outlook.txt"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
                patch.object(db, "_render_static_viewer"),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                db.insert_account(
                    email="jp@example.com",
                    access_token="token-jp",
                    account_locale="jp",
                    account_country="JP",
                )
                db.insert_account(
                    email="us@example.com",
                    access_token="token-us",
                    account_locale="us",
                    account_country="US",
                )

                rows = db.list_accounts(account_locale_filter="jp")

        self.assertEqual([row["email"] for row in rows], ["jp@example.com"])
        self.assertEqual(rows[0]["account_country"], "JP")

    @patch("webui.app.db.list_accounts_page")
    def test_webui_passes_account_locale_filter_to_account_query(self, list_accounts_page):
        list_accounts_page.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50, "revision": "0"}
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts?paged=1&page=1&page_size=50&account_locale=jp",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_accounts_page.call_args.kwargs["account_locale_filter"], "jp")

    @patch("core.free_plus_export.db.list_accounts")
    def test_free_plus_all_filtered_export_passes_account_locale(self, list_accounts):
        list_accounts.return_value = []

        with self.assertRaises(ValueError):
            free_plus_export.prepare_export(
                scope="all_filtered",
                account_locale_filter="jp",
            )

        self.assertEqual(list_accounts.call_args.kwargs["account_locale_filter"], "jp")


if __name__ == "__main__":
    unittest.main()
