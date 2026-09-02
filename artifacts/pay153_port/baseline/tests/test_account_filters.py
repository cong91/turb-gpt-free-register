import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db, free_plus_export
from webui.app import create_app


class AccountFilterTests(unittest.TestCase):
    def _db_context(self, root: Path):
        stack = ExitStack()
        for target, value in (
            ("_ACCOUNTS_JSON", root / "accounts.json"),
            ("_ACCOUNTS_TXT", root / "accounts.txt"),
            ("_TOKENS_TXT", root / "tokens.txt"),
            ("_OUTLOOK_JSON", root / "outlook.json"),
            ("_OUTLOOK_TXT", root / "outlook.txt"),
            ("_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            ("_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
        ):
            stack.enter_context(patch.object(db, target, value))
        stack.enter_context(patch.object(db, "_schedule_static_viewer_refresh"))
        return stack

    def test_account_source_filter_matches_provider_and_unknown_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._db_context(root):
                db.insert_account(email="gmail@example.com", access_token="token", email_source="gmail_api_url")
                db.insert_account(email="paymesh@example.com", access_token="token", email_source="sms_paymesh")
                db.insert_account(email="unknown@example.com", access_token="token")

                gmail_rows = db.list_accounts(email_source_filter="gmail_api_url")
                paymesh_rows = db.list_accounts(email_source_filter="paymesh")
                unknown_rows = db.list_accounts(email_source_filter="unknown")

        self.assertEqual([row["email"] for row in gmail_rows], ["gmail@example.com"])
        self.assertEqual([row["email"] for row in paymesh_rows], ["paymesh@example.com"])
        self.assertEqual([row["email"] for row in unknown_rows], ["unknown@example.com"])

    def test_twofa_filter_matches_failed_setup_without_mixing_other_states(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._db_context(root):
                db.insert_account(
                    email="failed@example.com",
                    access_token="token",
                    twofa_status="failed",
                    twofa_error="setup timeout",
                )
                db.insert_account(
                    email="active@example.com",
                    access_token="token",
                    totp_secret="SECRET",
                )
                db.insert_account(
                    email="disabled@example.com",
                    access_token="token",
                )
                db.insert_account(
                    email="pending@example.com",
                    access_token="token",
                    twofa_status="pending",
                )

                failed_rows = db.list_accounts(twofa_filter="failed")
                active_rows = db.list_accounts(twofa_filter="active")
                disabled_rows = db.list_accounts(twofa_filter="disabled")

        self.assertEqual([row["email"] for row in failed_rows], ["failed@example.com"])
        self.assertEqual([row["email"] for row in active_rows], ["active@example.com"])
        self.assertEqual([row["email"] for row in disabled_rows], ["disabled@example.com"])

    def test_account_email_domain_filter_normalizes_case_and_groups_unknown_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._db_context(root):
                db.insert_account(email="first@Gmail.com", access_token="token")
                db.insert_account(email="second@gmail.com", access_token="token")
                db.insert_account(email="without-domain", access_token="token")

                gmail_rows = db.list_accounts(email_domain_filter="GMAIL.COM")
                unknown_rows = db.list_accounts(email_domain_filter="unknown")

        self.assertEqual({row["email"] for row in gmail_rows}, {"first@Gmail.com", "second@gmail.com"})
        self.assertEqual([row["email"] for row in unknown_rows], ["without-domain"])

    def test_plan_filter_supports_unknown_plan_without_matching_free_plus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._db_context(root):
                db.insert_account(email="free@example.com", access_token="token", plan_type="free")
                trial_id = db.insert_account(email="trial@example.com", access_token="token", plan_type="free")
                db.update_account_plan_check(
                    acc_id=trial_id,
                    result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
                )
                db.insert_account(email="plus@example.com", access_token="token", plan_type="plus")
                db.insert_account(email="unknown@example.com", access_token="token")

                free_rows = db.list_accounts(plan_filter="free")
                trial_rows = db.list_accounts(plan_filter="free_plus")
                plus_rows = db.list_accounts(plan_filter="plus")
                unknown_rows = db.list_accounts(plan_filter="unknown")

        self.assertEqual({row["email"] for row in free_rows}, {"free@example.com", "trial@example.com"})
        self.assertEqual([row["email"] for row in trial_rows], ["trial@example.com"])
        self.assertEqual([row["email"] for row in plus_rows], ["plus@example.com"])
        self.assertEqual([row["email"] for row in unknown_rows], ["unknown@example.com"])

    def test_plan_check_to_free_without_plus_trial_resets_export_and_unarchives_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._db_context(root):
                account_id = db.insert_account(
                    email="rechecked@example.com",
                    access_token="token",
                    plan_type="free",
                )
                db.update_account_plan_check(
                    acc_id=account_id,
                    result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
                )
                updated, skipped = db.mark_accounts_free_plus_exported(
                    [account_id], format_name="modern"
                )
                self.assertEqual(len(updated), 1)
                self.assertEqual(skipped, [])
                self.assertTrue(db.get_account(account_id)["free_plus_exported_at"])
                self.assertTrue(db.get_account(account_id)["archived"])

                db.update_account_plan_check(
                    acc_id=account_id,
                    result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": False},
                )
                row = db.get_account(account_id)

        self.assertIsNotNone(row)
        self.assertIsNone(row["free_plus_exported_at"])
        self.assertEqual(row["free_plus_export_count"], 0)
        self.assertIsNone(row["free_plus_export_format"])
        self.assertIsNone(row["free_plus_export_source"])
        self.assertFalse(row["archived"])
        self.assertIsNone(row["archived_at"])

    @patch("webui.app.db.list_accounts_page")
    def test_accounts_api_passes_source_filter(self, list_accounts_page):
        list_accounts_page.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50, "revision": "0"}
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts?paged=1&page=1&page_size=50&email_source=paymesh&email_domain=gmail.com&plan=pro&twofa_status=failed",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_accounts_page.call_args.kwargs["email_source_filter"], "paymesh")
        self.assertEqual(list_accounts_page.call_args.kwargs["email_domain_filter"], "gmail.com")
        self.assertEqual(list_accounts_page.call_args.kwargs["plan_filter"], "pro")
        self.assertEqual(list_accounts_page.call_args.kwargs["twofa_filter"], "failed")

    @patch("webui.app.db.list_accounts")
    def test_filtered_account_ids_api_uses_all_account_filters(self, list_accounts):
        list_accounts.return_value = [{"id": 7}, {"id": 9}]
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts/filtered-ids?archived=0&plan=free&codex_status=failed"
            "&email_source=paymesh&email_domain=gmail.com&account_locale=jp"
            "&free_plus_export=unexported&twofa_status=failed&q=alpha"
            "&date_from=2026-08-01&date_to=2026-08-27",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account_ids"], [7, 9])
        self.assertEqual(list_accounts.call_args.kwargs, {
            "limit": 5001,
            "archived": "0",
            "plan_filter": "free",
            "codex_filter": "failed",
            "q": "alpha",
            "free_plus_export_filter": "unexported",
            "date_from": "2026-08-01",
            "date_to": "2026-08-27",
            "account_locale_filter": "jp",
            "email_source_filter": "paymesh",
            "email_domain_filter": "gmail.com",
            "twofa_filter": "failed",
        })

    @patch("webui.app.db.list_accounts")
    def test_email_domain_taxonomy_api_returns_counts_without_account_data(self, list_accounts):
        list_accounts.return_value = [
            {"id": 1, "email": "one@gmail.com", "email_domain": "gmail.com"},
            {"id": 2, "email": "two@outlook.com", "email_domain": "outlook.com"},
            {"id": 3, "email": "missing-domain", "email_domain": "unknown"},
            {"id": 4, "email": "other@gmail.com", "email_domain": "gmail.com"},
        ]
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts/email-domains?archived=0",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["domains"], [
            {"value": "gmail.com", "count": 2},
            {"value": "outlook.com", "count": 1},
        ])
        self.assertEqual(response.get_json()["unknown_count"], 1)
        self.assertNotIn("email", response.get_json()["domains"][0])
        list_accounts.assert_called_once_with(limit=5001, archived="0")

    @patch("webui.app.db.list_account_plan_check_statuses")
    def test_plan_status_api_passes_source_filter(self, list_statuses):
        list_statuses.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50, "revision": "0"}
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts/plan-check-status?page=1&page_size=50&email_source=gmail_api_url&twofa_status=failed",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_statuses.call_args.kwargs["email_source_filter"], "gmail_api_url")
        self.assertEqual(list_statuses.call_args.kwargs["twofa_filter"], "failed")

    @patch("webui.app.db.list_account_plan_check_statuses")
    def test_plan_status_api_passes_totp_filter(self, list_statuses):
        list_statuses.return_value = {"items": [], "total": 0, "offset": 0, "limit": 50, "revision": "0"}
        client = create_app(auth_code="test-auth").test_client()

        response = client.get(
            "/api/accounts/plan-check-status?page=1&page_size=50&totp_status=enabled",
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_statuses.call_args.kwargs["totp_filter"], "enabled")

    def test_plan_status_snapshot_with_empty_filters_uses_sql_path(self):
        expected = {"items": [], "total": 0, "offset": 0, "limit": 20, "revision": "0"}
        with patch.object(db, "_query_collection_page", return_value=([], 0, "")) as query_page:
            result = db.list_account_plan_check_statuses(limit=20, archived="0")

        self.assertEqual(result["items"], expected["items"])
        self.assertEqual(result["total"], expected["total"])
        query_page.assert_called_once()

    @patch("core.free_plus_export.db.list_accounts")
    def test_free_plus_filtered_export_passes_source_filter(self, list_accounts):
        list_accounts.return_value = []

        with self.assertRaises(ValueError):
            free_plus_export.prepare_export(
                scope="all_filtered",
                email_source_filter="paymesh",
                email_domain_filter="gmail.com",
                twofa_filter="failed",
            )

        self.assertEqual(list_accounts.call_args.kwargs["email_source_filter"], "paymesh")
        self.assertEqual(list_accounts.call_args.kwargs["email_domain_filter"], "gmail.com")
        self.assertEqual(list_accounts.call_args.kwargs["twofa_filter"], "failed")

    @patch("webui.app.db.mark_accounts_free_plus_exported", return_value=([{"id": 7}], []))
    @patch("webui.app.free_plus_export.prepare_export")
    def test_free_plus_export_api_passes_the_complete_filter_contract(self, prepare_export, _mark_exported):
        prepare_export.return_value = {
            "content": b"account-line\n",
            "filename": "free-plus-modern.txt",
            "format": "modern",
            "accounts": [{"id": 7, "email": "user@gmail.com"}],
            "account_ids": [7],
            "count": 1,
            "skipped": [],
        }
        client = create_app(auth_code="test-auth").test_client()

        response = client.post(
            "/api/accounts/free-plus/export",
            json={
                "scope": "all_filtered",
                "format": "modern",
                "archived": "0",
                "plan": "free_plus",
                "codex_status": "failed",
                "free_plus_export": "unexported",
                "q": "gmail",
                "date_from": "2026-08-01",
                "date_to": "2026-08-27",
                "account_locale": "jp",
                "email_source": "paymesh",
                "email_domain": "gmail.com",
                "twofa_status": "failed",
            },
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(prepare_export.call_args.kwargs, {
            "scope": "all_filtered",
            "account_ids": None,
            "format_name": "modern",
            "archived": "0",
            "q": "gmail",
            "codex_filter": "failed",
            "date_from": "2026-08-01",
            "date_to": "2026-08-27",
            "account_locale_filter": "jp",
            "email_source_filter": "paymesh",
            "email_domain_filter": "gmail.com",
            "twofa_filter": "failed",
        })

    def test_account_template_exposes_source_and_plan_filters(self):
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")

        self.assertIn('id="accountSourceFilterV2"', template)
        self.assertIn('id="accountDomainFilterV2"', template)
        self.assertIn('value="gmail_api_url"', template)
        self.assertIn('value="paymesh"', template)
        self.assertIn('id="accountPlanFilterV2"', template)
        self.assertIn('value="free_plus"', template)
        self.assertIn('value="unknown"', template)
        self.assertIn('id="accountTwofaFilterV2"', template)
        self.assertIn('value="failed"', template)
        self.assertIn("twofa_status=", template)
        self.assertIn('id="accountBulkScopeV2"', template)
        self.assertIn('email_source=${encodeURIComponent(emailSource)}', template)
        self.assertIn('email_domain=${encodeURIComponent(emailDomain)}', template)


if __name__ == "__main__":
    unittest.main()
