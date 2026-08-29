# -*- coding: utf-8 -*-
"""Gmail API URL 邮箱池 DB 层单元测试。"""
import unittest
from unittest.mock import patch

from core import db


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

    @patch("core.db._load_gmail_api_url_emails")
    def test_summary_returns_count_by_status(self, mock_load):
        """summary 返回按状态统计的数量。"""
        mock_load.return_value = [
            {"id": 1, "email": "available1@gmail.com", "status": "available"},
            {"id": 2, "email": "available2@gmail.com", "status": "available"},
            {"id": 3, "email": "used@gmail.com", "status": "used"},
            {"id": 4, "email": "failed@gmail.com", "status": "failed"},
        ]

        summary = db.gmail_api_url_email_pool_summary()

        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["available"], 2)
        self.assertEqual(summary["used"], 1)
        self.assertEqual(summary["failed"], 1)

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


if __name__ == "__main__":
    unittest.main()
