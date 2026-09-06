import io
import json
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from core.codex_account_import import parse_credential_lines
from webui.app import create_app


class CodexLocalBulkExportTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_local_bulk_export_requires_webui_authorization(self):
        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-one.json"]},
        )

        self.assertEqual(response.status_code, 401)

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_returns_two_file_zip_and_archives_exported_credentials(
        self, read_codex_credential, get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.side_effect = [
            (json.dumps({"email": "one@example.com", "access_token": "token-one"}), "codex-one.json"),
            (json.dumps({"email": "two@example.com", "access_token": "token-two"}), "codex-two.json"),
        ]
        get_account_by_email.side_effect = [
            {"email": "one@example.com", "registration_password": "openai-pass-one", "totp_secret": "TOTPONE"},
            {"email": "two@example.com", "registration_password": "openai-pass-two", "totp_secret": "TOTPTWO"},
        ]

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-one.json", "codex-two.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "2")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "0")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "0")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-one.json", "codex-two.json"],
            expected_payloads={
                "codex-one.json": {"email": "one@example.com", "access_token": "token-one"},
                "codex-two.json": {"email": "two@example.com", "access_token": "token-two"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            members = bundle.namelist()
            self.assertEqual(len(members), 2)
            self.assertEqual(sum(name.endswith(".json") for name in members), 1)
            self.assertEqual(sum(name.endswith(".txt") for name in members), 1)
            aggregate = json.loads(bundle.read(next(name for name in members if name.endswith(".json"))))
            account_lines = bundle.read(next(name for name in members if name.endswith(".txt")))

        self.assertEqual(aggregate["count"], 2)
        self.assertEqual(
            aggregate["credentials"],
            [
                {"filename": "codex-one.json", "data": {"email": "one@example.com", "access_token": "token-one"}},
                {"filename": "codex-two.json", "data": {"email": "two@example.com", "access_token": "token-two"}},
            ],
        )
        self.assertFalse(aggregate.get("errors"))
        self.assertEqual(
            account_lines.decode("utf-8"),
            "one@example.com | openai-pass-one | TOTPONE\n"
            "two@example.com | openai-pass-two | TOTPTWO\n",
        )
        self.assertEqual(
            parse_credential_lines(account_lines.decode("utf-8")),
            [
                {"email": "one@example.com", "registration_password": "openai-pass-one", "totp_secret": "TOTPONE"},
                {"email": "two@example.com", "registration_password": "openai-pass-two", "totp_secret": "TOTPTWO"},
            ],
        )

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_does_not_archive_credential_that_cannot_be_read(
        self, read_codex_credential, get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.side_effect = [
            (json.dumps({"email": "success@example.com", "access_token": "token"}), "codex-success.json"),
            ValueError("文件不存在: codex-missing.json"),
        ]
        get_account_by_email.return_value = {
            "email": "success@example.com", "registration_password": "openai-pass", "totp_secret": "TOTPSECRET",
        }

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-success.json", "codex-missing.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "0")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-success.json"],
            expected_payloads={
                "codex-success.json": {"email": "success@example.com", "access_token": "token"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            members = bundle.namelist()
            self.assertEqual(len(members), 2)
            aggregate = json.loads(bundle.read(next(name for name in members if name.endswith(".json"))))
        self.assertEqual(aggregate["count"], 1)
        self.assertEqual(aggregate["credentials"], [{"filename": "codex-success.json", "data": {"email": "success@example.com", "access_token": "token"}}])
        self.assertEqual(aggregate["errors"][0]["filename"], "codex-missing.json")

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email", return_value=None)
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_keeps_readable_json_when_no_account_line_can_be_paired(
        self, read_codex_credential, _get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.return_value = (
            json.dumps({"email": "missing@example.com", "access_token": "token"}), "codex-missing-account.json",
        )

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-missing-account.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "0")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "1")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-missing-account.json"],
            expected_payloads={
                "codex-missing-account.json": {"email": "missing@example.com", "access_token": "token"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            members = bundle.namelist()
            aggregate = json.loads(bundle.read(next(name for name in members if name.endswith(".json"))))
            account_lines = bundle.read(next(name for name in members if name.endswith(".txt")))
        self.assertEqual(aggregate["count"], 1)
        self.assertEqual(
            aggregate["credentials"],
            [{"filename": "codex-missing-account.json", "data": {"email": "missing@example.com", "access_token": "token"}}],
        )
        self.assertEqual(aggregate["account_line_errors"][0]["filename"], "codex-missing-account.json")
        self.assertEqual(account_lines, b"")

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_keeps_json_when_account_credentials_are_incomplete(
        self, read_codex_credential, get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.return_value = (
            json.dumps({"email": "incomplete@example.com", "access_token": "token"}), "codex-incomplete.json",
        )
        get_account_by_email.return_value = {
            "email": "incomplete@example.com", "registration_password": "openai-pass", "totp_secret": "",
        }

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-incomplete.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "1")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-incomplete.json"],
            expected_payloads={
                "codex-incomplete.json": {"email": "incomplete@example.com", "access_token": "token"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            aggregate = json.loads(bundle.read(next(name for name in bundle.namelist() if name.endswith(".json"))))
        self.assertEqual(aggregate["account_line_errors"][0]["email"], "incomplete@example.com")

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email", side_effect=RuntimeError("database is locked"))
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_keeps_json_when_account_pairing_raises(
        self, read_codex_credential, _get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.return_value = (
            json.dumps({"email": "lookup-error@example.com", "access_token": "token"}), "codex-lookup-error.json",
        )

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-lookup-error.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "0")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "1")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-lookup-error.json"],
            expected_payloads={
                "codex-lookup-error.json": {"email": "lookup-error@example.com", "access_token": "token"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            aggregate = json.loads(bundle.read(next(name for name in bundle.namelist() if name.endswith(".json"))))
        self.assertEqual(aggregate["count"], 1)
        self.assertIn("RuntimeError: database is locked", aggregate["account_line_errors"][0]["error"])

    @patch("webui.app.db.mark_codex_exported_and_archived")
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_deduplicates_filenames_before_export_state_change(
        self, read_codex_credential, get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.return_value = (
            json.dumps({"email": "one@example.com", "access_token": "token"}), "codex-one.json",
        )
        get_account_by_email.return_value = {
            "email": "one@example.com", "registration_password": "openai-pass", "totp_secret": "TOTPONE",
        }

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-one.json", "codex-one.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Account-Line-Skipped-Count"], "0")
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-one.json"],
            expected_payloads={
                "codex-one.json": {"email": "one@example.com", "access_token": "token"},
            },
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            aggregate = json.loads(bundle.read(next(name for name in bundle.namelist() if name.endswith(".json"))))
        self.assertEqual(aggregate["count"], 1)

    @patch(
        "webui.app.db.mark_codex_exported_and_archived",
        side_effect=ValueError("凭证在导出期间已更新，请重新导出: codex-one.json"),
    )
    @patch("webui.app.db.get_account_by_email")
    @patch("webui.app.db.read_codex_credential")
    def test_local_bulk_export_returns_conflict_when_snapshot_changed_before_archive(
        self, read_codex_credential, get_account_by_email, mark_codex_exported_and_archived,
    ):
        read_codex_credential.return_value = (
            json.dumps({"email": "one@example.com", "access_token": "old-token"}), "codex-one.json",
        )
        get_account_by_email.return_value = {
            "email": "one@example.com", "registration_password": "openai-pass", "totp_secret": "TOTPONE",
        }

        response = self.client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-one.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("导出期间已更新", response.get_json()["error"])
        mark_codex_exported_and_archived.assert_called_once_with(
            ["codex-one.json"],
            expected_payloads={
                "codex-one.json": {"email": "one@example.com", "access_token": "old-token"},
            },
        )


class CodexExportArchiveStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        accounts_path = root / "accounts.json"
        accounts_path.write_text("[]\n", encoding="utf-8")
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_JSON", accounts_path))
        self.stack.enter_context(patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"))
        self.stack.enter_context(patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"))
        self.stack.enter_context(patch.object(db, "_TOKENS_TXT", root / "tokens.txt"))
        self.stack.enter_context(patch.object(db, "_VIEWER_HTML", root / "viewer.html"))
        self.stack.enter_context(patch.object(db, "_render_static_viewer"))
        self.stack.enter_context(patch.object(db, "_SQLITE_READY", False))
        self.stack.enter_context(patch.object(db, "_SQLITE_READY_PATH", None))
        db.upsert_codex_credential({"email": "one@example.com"}, "codex-one.json")
        db.upsert_codex_credential({"email": "two@example.com"}, "codex-two.json")

    def tearDown(self):
        self.stack.close()
        self.temp_dir.cleanup()

    def test_state_commit_archives_all_exported_credentials_together(self):
        updated = db.mark_codex_exported_and_archived(["codex-one.json", "codex-two.json"])
        self.assertEqual([item["filename"] for item in updated], ["codex-one.json", "codex-two.json"])
        self.assertTrue(all(item["archived"] for item in updated))
        records = {item["filename"]: item for item in db.list_codex_accounts(archived="all")}
        self.assertEqual(records["codex-one.json"]["exported_count"], 1)
        self.assertTrue(records["codex-one.json"]["archived"])
        self.assertEqual(records["codex-two.json"]["exported_count"], 1)
        self.assertTrue(records["codex-two.json"]["archived"])

    def test_state_commit_rolls_back_all_credentials_when_one_is_missing(self):
        with self.assertRaisesRegex(ValueError, "文件不存在"):
            db.mark_codex_exported_and_archived(["codex-one.json", "codex-missing.json"])
        records = {item["filename"]: item for item in db.list_codex_accounts(archived="all")}
        self.assertEqual(records["codex-one.json"]["exported_count"], 0)
        self.assertFalse(records["codex-one.json"]["archived"])
        self.assertEqual(records["codex-two.json"]["exported_count"], 0)
        self.assertFalse(records["codex-two.json"]["archived"])

    def test_state_commit_rejects_a_credential_updated_after_export_snapshot(self):
        db.upsert_codex_credential(
            {"email": "one@example.com", "access_token": "fresh-token"},
            "codex-one.json",
            reset_export_state=True,
        )

        with self.assertRaisesRegex(ValueError, "导出期间已更新"):
            db.mark_codex_exported_and_archived(
                ["codex-one.json"],
                expected_payloads={"codex-one.json": {"email": "one@example.com"}},
            )

        record = {item["filename"]: item for item in db.list_codex_accounts(archived="all")}["codex-one.json"]
        self.assertFalse(record["archived"])
        self.assertEqual(record["exported_count"], 0)

    def test_route_exports_actual_sqlite_credential_and_account_line_then_archives_it(self):
        email = "integrated@example.com"
        db.insert_account(
            email=email,
            access_token="account-token",
            registration_password="openai-pass",
            totp_secret="TOTPSECRET",
        )
        db.upsert_codex_credential(
            {"email": email, "access_token": "codex-token"},
            "codex-integrated.json",
        )
        client = create_app(auth_code="test-auth").test_client()

        response = client.post(
            "/api/codex/download-bulk",
            json={"filenames": ["codex-integrated.json"]},
            headers={"X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertIn("no-store", response.headers["Cache-Control"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            members = bundle.namelist()
            self.assertEqual(len(members), 2)
            aggregate = json.loads(bundle.read(next(name for name in members if name.endswith(".json"))))
            account_text = bundle.read(next(name for name in members if name.endswith(".txt"))).decode("utf-8")

        self.assertEqual(aggregate["credentials"], [{
            "filename": "codex-integrated.json",
            "data": {"email": email, "access_token": "codex-token"},
        }])
        self.assertEqual(account_text, "integrated@example.com | openai-pass | TOTPSECRET\n")
        record = {
            item["filename"]: item for item in db.list_codex_accounts(archived="all")
        }["codex-integrated.json"]
        self.assertTrue(record["archived"])
        self.assertEqual(record["exported_count"], 1)

    def test_fresh_oauth_save_reopens_an_archived_credential(self):
        db.mark_codex_exported_and_archived(["codex-one.json"])

        db.upsert_codex_credential(
            {"email": "one@example.com", "access_token": "fresh-token"},
            "codex-one.json",
            reset_export_state=True,
        )

        active_records = {item["filename"]: item for item in db.list_codex_accounts()}
        self.assertIn("codex-one.json", active_records)
        self.assertFalse(active_records["codex-one.json"]["archived"])
        self.assertEqual(active_records["codex-one.json"]["exported_count"], 0)

    def test_callback_receipt_save_reopens_an_archived_credential(self):
        db.mark_codex_exported_and_archived(["codex-one.json"])

        db.upsert_codex_credential(
            {"email": "one@example.com", "type": "codex_sub2_callback"},
            "codex-one.json",
            reset_export_state=True,
        )

        active_records = {item["filename"]: item for item in db.list_codex_accounts()}
        self.assertIn("codex-one.json", active_records)
        self.assertFalse(active_records["codex-one.json"]["archived"])


if __name__ == "__main__":
    unittest.main()
