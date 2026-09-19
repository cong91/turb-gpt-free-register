import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_failure_stats


class RegistrationFailureStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for name, value in (
            ("_ACCOUNTS_JSON", root / "accounts.json"),
            ("_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            ("_JOBS_JSON", root / "jobs.json"),
            ("_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            ("_LOG_DIR", root / "logs"),
            ("_ACCOUNTS_TXT", root / "accounts.txt"),
            ("_TOKENS_TXT", root / "tokens.txt"),
            ("_VIEWER_HTML", root / "viewer.html"),
        ):
            patcher = patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_classify_failure_maps_known_messages_to_stable_classes(self):
        cases = {
            "GmailApiUrlError: No Gmail API URL source available": "alloc_failed",
            "RuntimeError: 所有邮箱来源均领取失败: ['gmail_api_url']": "alloc_failed",
            "RuntimeError: 等待 /api/auth/session accessToken 超时，最后响应: session 暂无 accessToken": "session_token_timeout",
            "RuntimeError: 找不到可点击的重新发送验证码按钮: last=None": "resend_button_missing",
            "GmailApiUrlError: Timeout after 60s waiting for new OTP": "otp_timeout",
            "2FA 设置失败，账号已保存：RuntimeError: re-auth 未进入 email-verification 页面": "twofa_reauth_no_otp_page",
            "2FA 设置失败，账号已保存：TimeoutException: 停在登录页": "twofa_setup_failed_saved",
            "RuntimeError: [QAN8] Provider error code=602: Account is unavailable": "provider_602",
            "RuntimeError: 邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用": "email_already_registered",
            "RuntimeError: 找不到邮箱输入框/邮箱入口，state={'url': 'chrome-error://chromewebdata/'}": "email_input_missing",
            "RuntimeError: 密码页提交后未进入邮箱验证码页，仍停留在注册密码页": "password_page_stuck",
            "RuntimeError: 密码页提交失败：vui lòng thử lại": "password_submit_retry",
            "RuntimeError: Codex 未完成: 套餐查询失败: HTTP 403": "codex_plan_403",
        }
        for message, expected in cases.items():
            self.assertEqual(registration_failure_stats.classify_failure(message), expected, message)

    def test_classify_failure_falls_back_to_other(self):
        self.assertEqual(registration_failure_stats.classify_failure(None), "other")
        self.assertEqual(registration_failure_stats.classify_failure(""), "other")
        self.assertEqual(registration_failure_stats.classify_failure("mystery failure"), "other")

    def test_failure_class_counts_orders_by_count_desc(self):
        seeded = {
            "timeout-a": "RuntimeError: 等待 /api/auth/session accessToken 超时",
            "timeout-b": "RuntimeError: 等待 /api/auth/session accessToken 超时",
            "timeout-c": "RuntimeError: 等待 /api/auth/session accessToken 超时",
            "alloc-a": "GmailApiUrlError: No Gmail API URL source available",
            "weird": "某个全新错误",
        }
        for message in seeded.values():
            job = db.create_job(email_source="gmail_api_url")
            db.update_job(job["id"], status="failed", error=message)

        counts = registration_failure_stats.failure_class_counts()

        self.assertEqual(counts.get("session_token_timeout"), 3)
        self.assertEqual(counts.get("alloc_failed"), 1)
        self.assertEqual(counts.get("other"), 1)
        items = list(counts.items())
        self.assertEqual(items[0], ("session_token_timeout", 3))
        self.assertNotIn("email", counts)


if __name__ == "__main__":
    unittest.main()
