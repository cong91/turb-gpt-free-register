"""Gmail API URL 邮箱池 DB 层单元测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.gmail_aliases import generate_gmail_dual_domain_aliases
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.qan8_gmail_api_store import Qan8GmailApiStore


class GmailApiUrlPoolTests(unittest.TestCase):
    """Gmail API URL 邮箱池测试套件。"""

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_import_parses_format_and_returns_counts(self, mock_save, mock_load):
        """导入解析 email----code_url 格式并返回插入/跳过数。"""
        mock_load.return_value = []

        inserted, skipped = db.import_gmail_api_url_emails([
            {"email": "test1@gmail.com", "code_url": "http://example.com/otp1"},
            {"email": "test2@gmail.com", "code_url": "http://example.com/otp2"},
        ])

        self.assertEqual(inserted, 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(mock_save.call_count, 1)
        saved_rows = mock_save.call_args[0][0]
        self.assertEqual(len(saved_rows), 2)
        self.assertEqual(saved_rows[0]["email"], "test1@gmail.com")
        self.assertEqual(saved_rows[0]["code_url"], "http://example.com/otp1")
        self.assertEqual(saved_rows[0]["status"], "available")

    @patch("core.db._load_gmail_api_url_emails", return_value=[])
    @patch("core.db._save_gmail_api_url_emails")
    def test_record_purchased_source_persists_it_as_used(self, mock_save, _mock_load):
        recorded = db.record_gmail_api_url_email(
            "Purchased@gmail.com",
            "https://example.com/otp",
            status="used",
            note="QAN8 purchased source",
        )

        self.assertTrue(recorded)
        saved_rows = mock_save.call_args.args[0]
        self.assertEqual(saved_rows[0]["email"], "purchased@gmail.com")
        self.assertEqual(saved_rows[0]["code_url"], "https://example.com/otp")
        self.assertEqual(saved_rows[0]["status"], "used")
        self.assertEqual(saved_rows[0]["note"], "QAN8 purchased source")

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_record_does_not_reactivate_a_failed_code_url(self, mock_save, mock_load):
        mock_load.return_value = [
            {
                "id": 1,
                "email": "failed@gmail.com",
                "code_url": "https://example.com/otp",
                "status": "failed",
                "used_at": "2026-09-04T00:00:00+00:00",
                "note": "code=602",
            },
        ]

        db.record_gmail_api_url_email(
            "failed@gmail.com",
            "https://example.com/otp",
            status="exhausted",
            note="QAN8 source has no globally available Gmail aliases",
        )

        saved_row = mock_save.call_args.args[0][0]
        self.assertEqual(saved_row["status"], "failed")
        self.assertEqual(saved_row["used_at"], "2026-09-04T00:00:00+00:00")
        self.assertEqual(saved_row["note"], "code=602")

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_provider_602_quarantine_cannot_be_reenabled(self, mock_save, mock_load):
        mock_load.return_value = [{
            "id": 1,
            "email": "quarantined@gmail.com",
            "code_url": "https://example.com/otp",
            "status": "failed",
            "used_at": "2026-09-04T00:00:00+00:00",
            "note": "Provider error code=602",
        }]

        db.release_gmail_api_url_email(
            "quarantined@gmail.com",
            status="available",
            note="operator attempted to re-enable",
        )

        saved_row = mock_save.call_args.args[0][0]
        self.assertEqual(saved_row["status"], "failed")
        self.assertTrue(saved_row["quarantined"])
        self.assertEqual(saved_row["note"], "Provider error code=602")

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_import_skips_duplicates_and_invalid_emails(self, mock_save, mock_load):
        """导入跳过重复邮箱和无效邮箱。"""
        mock_load.return_value = [
            {"id": 1, "email": "existing@gmail.com", "code_url": "http://old.com", "status": "available"}
        ]

        inserted, skipped = db.import_gmail_api_url_emails([
            {"email": "", "code_url": "http://example.com/otp1"},
            {"email": "existing@gmail.com", "code_url": "http://example.com/otp2"},
            {"email": "new@gmail.com", "code_url": "http://example.com/otp3"},
        ])

        self.assertEqual(inserted, 1)
        self.assertEqual(skipped, 2)

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_claim_returns_first_available_and_marks_used(self, mock_save, mock_load):
        """claim 返回第一个可用账号并标记为已用。"""
        mock_load.return_value = [
            {"id": 1, "email": "available@gmail.com", "code_url": "http://example.com/otp", "status": "available"},
            {"id": 2, "email": "used@gmail.com", "code_url": "http://example.com/otp2", "status": "used"},
        ]

        row = db.claim_next_gmail_api_url_email()

        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "available@gmail.com")
        self.assertEqual(row["status"], "used")
        self.assertEqual(mock_save.call_count, 1)

    @patch("core.db._load_gmail_api_url_emails", return_value=[])
    def test_claim_returns_none_when_pool_empty(self, mock_load):
        """邮箱池为空时 claim 返回 None。"""
        row = db.claim_next_gmail_api_url_email()

        self.assertIsNone(row)

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_claim_can_reuse_used_source_for_alias_batch(self, mock_save, mock_load):
        """A used source remains eligible while its alias inventory has capacity."""
        mock_load.return_value = [
            {
                "id": 1,
                "email": "used@gmail.com",
                "code_url": "http://example.com/otp",
                "status": "used",
                "note": "previous alias batch",
            },
        ]

        row = db.claim_next_gmail_api_url_email(include_used=True)

        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "used@gmail.com")
        self.assertEqual(row["status"], "used")
        self.assertFalse(row["_claimed_from_available"])
        mock_save.assert_not_called()

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_release_updates_status_and_note(self, mock_save, mock_load):
        """release 更新邮箱状态和备注。"""
        mock_load.return_value = [
            {"id": 1, "email": "test@gmail.com", "code_url": "http://example.com/otp", "status": "used", "note": ""},
        ]

        db.release_gmail_api_url_email("test@gmail.com", status="failed", note="code=602 退款")

        self.assertEqual(mock_save.call_count, 1)
        saved_rows = mock_save.call_args[0][0]
        self.assertEqual(saved_rows[0]["status"], "failed")
        self.assertEqual(saved_rows[0]["note"], "code=602 退款")

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_fail_code_url_marks_all_matching_sources_failed(self, mock_save, mock_load):
        rows = [
            {
                "email": "first@example.com",
                "code_url": "https://provider.example/code/shared",
                "status": "available",
                "used_at": "",
                "note": "",
            },
            {
                "email": "second@example.com",
                "code_url": "https://provider.example/code/shared",
                "status": "used",
                "used_at": "2026-01-01T00:00:00+00:00",
                "note": "old",
            },
            {
                "email": "healthy@example.com",
                "code_url": "https://provider.example/code/healthy",
                "status": "available",
                "used_at": "",
                "note": "",
            },
        ]
        mock_load.return_value = rows

        failed = db.fail_gmail_api_url_sources_for_code_url(
            "https://provider.example/code/shared",
            "Gmail API error code=602",
        )

        self.assertEqual(failed, 2)
        mock_save.assert_called_once_with(rows)
        self.assertEqual([row["status"] for row in rows], ["failed", "failed", "available"])
        self.assertTrue(rows[0]["quarantined"])
        self.assertTrue(rows[1]["quarantined"])
        self.assertEqual(rows[0]["note"], "Gmail API error code=602")
        self.assertEqual(rows[1]["used_at"], "2026-01-01T00:00:00+00:00")

    @patch("core.db._load_gmail_api_url_emails")
    def test_failed_code_url_is_not_available_to_another_worker(self, mock_load):
        mock_load.return_value = [
            {
                "email": "failed@example.com",
                "code_url": "https://provider.example/code/failed",
                "status": "failed",
            },
            {
                "email": "healthy@example.com",
                "code_url": "https://provider.example/code/healthy",
                "status": "used",
            },
        ]

        self.assertTrue(
            db.is_gmail_api_url_code_url_failed("https://provider.example/code/failed")
        )
        self.assertFalse(
            db.is_gmail_api_url_code_url_failed("https://provider.example/code/healthy")
        )

    @patch("core.db._load_gmail_api_url_emails")
    def test_list_filters_by_status_and_respects_limit(self, mock_load):
        """list 按状态过滤并遵守 limit。"""
        mock_load.return_value = [
            {"id": 1, "email": "available1@gmail.com", "status": "available"},
            {"id": 2, "email": "available2@gmail.com", "status": "available"},
            {"id": 3, "email": "failed@gmail.com", "status": "failed"},
        ]

        rows = db.list_gmail_api_url_email_pool(status="available", limit=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "available")

    @patch("core.db._load_accounts", return_value=[])
    @patch("core.db.GmailApiUrlBatchStore.alias_root_owners", return_value={})
    @patch("core.db.GmailApiUrlBatchStore.alias_usage_for_code_urls")
    @patch("core.db._load_gmail_api_url_emails")
    def test_list_exposes_alias_inventory_for_gmail_api_url(
        self, mock_load, usage_for_urls, _root_owners, _accounts
    ):
        """Gmail API URL pool rows expose total/available/used alias counts."""
        mock_load.return_value = [{
            "id": 1,
            "email": "source@gmail.com",
            "code_url": "http://example.com/otp",
            "status": "available",
        }]
        usage_for_urls.return_value = {
            "http://example.com/otp": {
                "allocated": set(),
                "consumed": set(),
                "failed": set(),
                "reserved": set(),
            },
        }

        row = db.list_gmail_api_url_email_pool()[0]

        self.assertEqual(row["alias_total"], 12)
        self.assertEqual(row["alias_available"], 12)
        self.assertEqual(row["alias_used"], 0)
        self.assertEqual(row["alias_reserved"], 0)
        usage_for_urls.assert_called_once_with({"http://example.com/otp"})

    def test_alias_inventory_hides_root_owned_by_another_code_url(self):
        """A dotted source cannot report capacity owned by another API URL."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pool_json = root / "gmail-pool.json"
            pool_txt = root / "gmail-pool.txt"
            state = root / "turb.sqlite3"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
                patch.object(db, "_SQLITE_PATH", state),
                patch.object(db, "_DEFAULT_SQLITE_PATH", state),
                patch.object(db, "_SQLITE_READY", False),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "s.ame@gmail.com",
                        "code_url": "https://mail.example/source-b",
                    }
                ])
                store = GmailApiUrlBatchStore(state)
                store.create_batch_multi([
                    {
                        "source_email": "same@gmail.com",
                        "code_url": "https://mail.example/source-a",
                        "aliases": generate_gmail_dual_domain_aliases(
                            "same@gmail.com", limit=12
                        ),
                    }
                ])

                row = db.list_gmail_api_url_email_pool()[0]

                self.assertEqual(row["alias_total"], 12)
                self.assertEqual(row["alias_available"], 0)
                self.assertEqual(row["alias_allocated"], 12)
    def test_alias_inventory_merges_gmail_and_qan8_ownership(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            email = "source@gmail.com"
            code_url = "https://provider.example/shared"
            candidates = generate_gmail_dual_domain_aliases(email, limit=12)

            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch = gmail_store.create_batch_multi([
                {"source_email": email, "code_url": code_url, "aliases": candidates},
            ])
            for index in range(2):
                assignment = gmail_store.claim(gmail_batch, f"gmail-job-{index}")
                self.assertTrue(gmail_store.complete(assignment.assignment_id))

            qan8_store = Qan8GmailApiStore(path)
            qan8_batch = qan8_store.create_batch(1, requested_workers=1, aliases_per_source=12)
            qan8_store.create_source_group(
                qan8_batch["batch_id"], 0, email, code_url, candidates,
            )
            assignment = qan8_store.claim_alias(qan8_batch["batch_id"], 0, "qan8-job")
            self.assertIsNotNone(assignment)
            self.assertTrue(qan8_store.complete_assignment("qan8-job"))

            with patch.object(db, "_SQLITE_PATH", path):
                rows = db._attach_gmail_api_url_alias_stats([
                    {"email": email, "code_url": code_url},
                ])

            self.assertEqual(rows[0]["alias_total"], 12)
            self.assertEqual(rows[0]["alias_used"], 3)
            self.assertEqual(rows[0]["alias_available"], 9)

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_delete_removes_email_and_returns_true(self, mock_save, mock_load):
        """delete 删除邮箱并返回 True。"""
        mock_load.return_value = [
            {"id": 1, "email": "delete@gmail.com", "code_url": "http://example.com/otp", "status": "available"},
            {"id": 2, "email": "keep@gmail.com", "code_url": "http://example.com/otp2", "status": "available"},
        ]

        deleted = db.delete_gmail_api_url_email("delete@gmail.com")

        self.assertTrue(deleted)
        self.assertEqual(mock_save.call_count, 1)
        saved_rows = mock_save.call_args[0][0]
        self.assertEqual(len(saved_rows), 1)
        self.assertEqual(saved_rows[0]["email"], "keep@gmail.com")

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._save_gmail_api_url_emails")
    def test_delete_returns_false_when_email_not_found(self, mock_save, mock_load):
        """邮箱不存在时 delete 返回 False。"""
        mock_load.return_value = [
            {"id": 1, "email": "keep@gmail.com", "code_url": "http://example.com/otp", "status": "available"},
        ]

        deleted = db.delete_gmail_api_url_email("missing@gmail.com")

        self.assertFalse(deleted)
        self.assertEqual(mock_save.call_count, 0)

    @patch("core.db._attach_gmail_api_url_alias_stats")
    @patch("core.db._load_gmail_api_url_emails")
    def test_summary_returns_count_by_status(self, mock_load, mock_attach):
        """summary 返回按状态统计的数量。"""
        mock_load.return_value = [
            {"id": 1, "email": "available1@gmail.com", "status": "available"},
            {"id": 2, "email": "available2@gmail.com", "status": "available"},
            {"id": 3, "email": "used@gmail.com", "status": "used"},
            {"id": 4, "email": "failed@gmail.com", "status": "failed"},
        ]
        mock_attach.return_value = [
            {"status": "available", "alias_total": 12, "alias_available": 12, "alias_used": 0, "alias_failed": 0, "alias_reserved": 0},
            {"status": "available", "alias_total": 12, "alias_available": 0, "alias_used": 12, "alias_failed": 0, "alias_reserved": 0},
            {"status": "used", "alias_total": 12, "alias_available": 10, "alias_used": 2, "alias_failed": 0, "alias_reserved": 0},
            {"status": "failed", "alias_total": 12, "alias_available": 12, "alias_used": 0, "alias_failed": 0, "alias_reserved": 0},
        ]

        summary = db.gmail_api_url_email_pool_summary()

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["available"], 2)
        self.assertEqual(summary["used"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["alias_available"], 22)
        self.assertEqual(summary["alias_source_available"], 2)

    @patch("core.db._load_gmail_api_url_emails")
    def test_get_by_email_returns_matching_account(self, mock_load):
        """get_by_email 返回匹配的账号。"""
        mock_load.return_value = [
            {"id": 1, "email": "find@gmail.com", "code_url": "http://example.com/otp", "status": "available"},
        ]

        row = db.get_gmail_api_url_email_by_email("find@gmail.com")

        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "find@gmail.com")

    @patch("core.db._load_gmail_api_url_emails", return_value=[])
    def test_get_by_email_returns_none_when_not_found(self, mock_load):
        """账号不存在时 get_by_email 返回 None。"""
        row = db.get_gmail_api_url_email_by_email("missing@gmail.com")

        self.assertIsNone(row)

    @patch("core.db._attach_gmail_api_url_alias_stats")
    @patch("core.db.GmailApiUrlBatchStore")
    @patch("core.db.release_gmail_api_url_email")
    @patch("core.db._load_accounts", return_value=[])
    @patch("core.db._load_gmail_api_url_emails")
    def test_reset_aliases_releases_source_and_returns_inventory(
        self, mock_load, _accounts, release_email, store_class, attach_stats
    ):
        """Reset removes unused batch slots and returns refreshed counts."""
        record = {
            "email": "source@gmail.com",
            "code_url": "http://example.com/otp",
            "status": "used",
        }
        mock_load.return_value = [record]
        store_class.return_value.reset_unused_aliases_for_code_url.return_value = 10
        attach_stats.return_value = [{
            **record,
            "alias_total": 12,
            "alias_allocated": 2,
            "alias_available": 10,
            "alias_used": 2,
            "alias_failed": 0,
            "alias_reserved": 0,
        }]

        result = db.reset_gmail_api_url_aliases("source@gmail.com")

        self.assertEqual(result["reset_aliases"], 10)
        self.assertEqual(result["alias_available"], 10)
        release_email.assert_called_once_with(
            "source@gmail.com",
            status="available",
            note="手动重置未消费 alias：10",
        )

    @patch("core.db._load_gmail_api_url_emails")
    @patch("core.db._load_accounts", return_value=[])
    @patch("core.db.GmailApiUrlBatchStore")
    @patch("core.db.release_gmail_api_url_email")
    @patch("core.db._attach_gmail_api_url_alias_stats")
    def test_reset_aliases_with_no_unused_slots_keeps_source_state(
        self, attach_stats, release_email, store_class, _accounts, mock_load
    ):
        """A no-op reset must not reopen a source with no remaining aliases."""
        record = {
            "email": "source@gmail.com",
            "code_url": "http://example.com/otp",
            "status": "used",
        }
        mock_load.return_value = [record]
        store_class.return_value.reset_unused_aliases_for_code_url.return_value = 0
        attach_stats.return_value = [{
            **record,
            "alias_total": 12,
            "alias_allocated": 12,
            "alias_available": 0,
            "alias_used": 12,
            "alias_failed": 0,
            "alias_reserved": 0,
        }]

        result = db.reset_gmail_api_url_aliases("source@gmail.com")

        self.assertEqual(result["reset_aliases"], 0)
        self.assertEqual(result["source_status"], "used")
        release_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
