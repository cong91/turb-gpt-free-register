import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from core import db, registration_service
from core.qan8_gmail_api_store import Qan8GmailApiStore


class RegistrationServiceTwofaRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.driver_patcher = patch("config.roxybrowser.REGISTRATION_DRIVER", "roxy")
        self.driver_patcher.start()
        self.addCleanup(self.driver_patcher.stop)
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

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "browser_use")
    def test_twofa_retry_driver_comes_from_current_registration_setting(self):
        self.assertEqual(registration_service._configured_twofa_retry_driver(), "browser_use")

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "skyvern")
    def test_twofa_retry_driver_normalizes_skyvern_alias(self):
        self.assertEqual(registration_service._configured_twofa_retry_driver(), "skyvern")

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "skyvern")
    @patch("core.browser_twofa_retry.run_twofa_retry")
    def test_skyvern_setting_uses_shared_browser_retry(self, run_twofa_retry):
        run_twofa_retry.return_value = {"ok": True, "status": "success"}

        result = registration_service._run_configured_twofa_retry({"id": 7})

        self.assertEqual(result["status"], "success")
        run_twofa_retry.assert_called_once_with({"id": 7})

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "browser_use")
    @patch("core.browser_twofa_retry.run_twofa_retry")
    def test_browser_use_setting_uses_shared_browser_retry(self, run_twofa_retry):
        run_twofa_retry.return_value = {"ok": True, "status": "success"}

        result = registration_service._run_configured_twofa_retry({"id": 7})

        self.assertEqual(result["status"], "success")
        run_twofa_retry.assert_called_once_with({"id": 7})

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol")
    @patch("core.browser_twofa_retry.run_twofa_retry")
    def test_non_browser_setting_does_not_dispatch_to_shared_browser_retry(self, run_twofa_retry):
        result = registration_service._run_configured_twofa_retry({"id": 7})

        self.assertFalse(result["ok"])
        self.assertIn("protocol", result["message"])
        run_twofa_retry.assert_not_called()

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak")
    @patch("core.browser_twofa_retry.run_twofa_retry")
    def test_cloak_setting_uses_shared_browser_retry(self, run_twofa_retry):
        run_twofa_retry.return_value = {"ok": True, "status": "success"}

        result = registration_service._run_configured_twofa_retry({"id": 7})

        self.assertEqual(result["status"], "success")
        run_twofa_retry.assert_called_once_with({"id": 7})

    @patch("config.roxybrowser.REGISTRATION_DRIVER", "roxy")
    @patch("core.browser_twofa_retry.run_twofa_retry")
    def test_roxy_setting_uses_shared_browser_retry(self, run_twofa_retry):
        run_twofa_retry.return_value = {"ok": True, "status": "success"}

        result = registration_service._run_configured_twofa_retry({"id": 7})

        self.assertEqual(result["status"], "success")
        run_twofa_retry.assert_called_once_with({"id": 7})

    def test_failed_twofa_account_gets_twofa_retry_not_registration_or_codex(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="token",
            registration_password="password",
            twofa_status="failed",
            twofa_error="script timeout",
            extra={"registration_driver": "roxy"},
        )
        job = db.create_job(email_source="paymesh", email="user@example.com")
        db.update_job(job["id"], status="failed", email="user@example.com", account_id=account_id, error="2FA failed")

        info = registration_service.get_retry_info(db.get_job(job["id"]))

        self.assertTrue(info["retryable"])
        self.assertEqual(info["retry_action"], "2fa")
        self.assertNotEqual(info["retry_action"], "registration")
        self.assertNotEqual(info["retry_action"], "codex")

    def test_twofa_retry_is_rejected_without_browser_login_material(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="token",
            twofa_status="failed",
        )
        source = db.create_job(email_source="paymesh", email="user@example.com")
        db.update_job(source["id"], status="failed", email="user@example.com", account_id=account_id)

        result = registration_service.retry_job(source["id"], workers=1)

        self.assertFalse(result["ok"])
        self.assertIn("不能自动补做 2FA", result["error"])
        self.assertEqual(len(db.list_jobs(limit=20)), 1)

    def test_retry_job_dispatches_twofa_worker_with_account_id(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="token",
            registration_password="password",
            twofa_status="failed",
            extra={"registration_driver": "roxy"},
        )
        source = db.create_job(email_source="paymesh", email="user@example.com")
        db.update_job(source["id"], status="failed", email="user@example.com", account_id=account_id)
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch(
            "core.rotating_proxy_runtime.prepare_rotating_proxy_lanes"
        ) as prepare, patch.object(
            registration_service, "get_executor", return_value=ImmediateExecutor()
        ), patch.object(registration_service, "get_executor_workers", return_value=1):
            result = registration_service.retry_job(source["id"], workers=1)

        self.assertTrue(result["ok"])
        prepare.assert_called_once_with(1, scope="twofa_retry")
        self.assertEqual(result["retry_action"], "2fa")
        retry_job = db.get_job(result["job"]["id"])
        self.assertEqual(retry_job["job_type"], "twofa_retry")
        self.assertEqual(retry_job["account_id"], account_id)
        self.assertEqual(len(submitted), 1)
        self.assertIs(submitted[0][0], registration_service._run_twofa_retry_job)
        self.assertNotEqual(submitted[0][0], registration_service._run_one_job)

    def test_account_reactivate_allows_disabled_twofa_after_successful_registration(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="token",
            registration_password="password",
            twofa_status="disabled",
            extra={"registration_driver": "roxy"},
        )
        source = db.create_job(email_source="paymesh", email="user@example.com")
        db.update_job(source["id"], status="success", email="user@example.com", account_id=account_id)
        submitted = []

        class ImmediateExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        with patch.object(registration_service, "get_executor", return_value=ImmediateExecutor()), patch.object(
            registration_service, "get_executor_workers", return_value=1
        ), patch("core.rotating_proxy_runtime.prepare_rotating_proxy_lanes"):
            result = registration_service.retry_account_twofa(account_id, workers=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["retry_action"], "2fa")
        retry_job = db.get_job(result["job"]["id"])
        self.assertEqual(retry_job["job_type"], "twofa_retry")
        self.assertEqual(retry_job["parent_job_id"], source["id"])
        self.assertIs(submitted[0][0], registration_service._run_twofa_retry_job)

    def test_bulk_account_reactivate_deduplicates_and_classifies_results(self):
        with patch.object(
            registration_service,
            "retry_account_twofa",
            side_effect=[
                {"ok": True, "reused": False, "job": {"id": 101}, "message": "started"},
                {"ok": False, "error": "该账号的 2FA 已启用"},
                {"ok": True, "reused": True, "job": {"id": 102}, "message": "reused"},
            ],
        ) as retry_one:
            result = registration_service.retry_accounts_twofa([7, 7, 8, 9], workers=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["started"], [{"account_id": 7, "job_id": 101, "message": "started"}])
        self.assertEqual(result["reused"], [{"account_id": 9, "job_id": 102, "message": "reused"}])
        self.assertEqual(result["skipped"], [{"account_id": 8, "reason": "该账号的 2FA 已启用"}])
        retry_one.assert_has_calls([call(7, workers=3), call(8, workers=3), call(9, workers=3)])
    def test_registration_exception_after_checkpoint_keeps_account_link(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="token",
            registration_password="password",
            twofa_status="pending",
            extra={"registration_driver": "roxy"},
        )
        job = db.create_job(email_source="paymesh")
        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("user@example.com", "Test User", "1990-01-01"),
        ), patch("main.run_registration", side_effect=RuntimeError("post-checkpoint failure")), patch.object(
            registration_service, "_release_unconsumed_job_email"
        ) as release_email, patch.object(registration_service, "_disable_job_email") as disable_email:
            registration_service._run_one_job(job["id"], job["log_file"])

        completed = db.get_job(job["id"])
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["account_id"], account_id)
        release_email.assert_not_called()
        disable_email.assert_not_called()

    def test_recoverable_qan8_twofa_failure_consumes_alias_and_frees_lane(self):
        store = Qan8GmailApiStore(Path(self.temp_dir.name) / "qan8.sqlite3")
        batch = store.create_batch(2, requested_workers=1, aliases_per_source=2)
        store.create_source_group(
            batch["batch_id"],
            0,
            "source@gmail.com",
            "https://mail.example/source",
            ["source+one@gmail.com", "source+two@gmail.com"],
        )
        job = db.create_job(
            email_source="qan8_gmail_api",
            provider_context={
                "qan8_gmail_api_batch_id": batch["batch_id"],
                "qan8_gmail_api_lane_id": 0,
            },
        )
        assignment = store.claim_alias(batch["batch_id"], 0, job["id"])
        account_id = db.insert_account(
            email=assignment["alias"],
            access_token="token",
            registration_password="password",
            twofa_status="failed",
        )

        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=(assignment["alias"], "Test User", "1990-01-01"),
        ), patch(
            "main.run_registration",
            return_value={
                "success": False,
                "email": assignment["alias"],
                "account_id": account_id,
                "twofa_status": "failed",
                "error": "2FA timeout",
            },
        ), patch(
            "core.email_provider.resolve_email_source",
            return_value="qan8_gmail_api",
        ), patch(
            "core.email_provider.mark_email_consumed",
            side_effect=lambda _email: store.complete_assignment(job["id"]),
        ) as mark_consumed:
            registration_service._run_one_job(job["id"], job["log_file"])

        mark_consumed.assert_called_once_with(assignment["alias"])
        self.assertIsNone(store.get_lane(batch["batch_id"], 0)["active_job_id"])
        self.assertEqual(store.get_account_context(assignment["alias"])["alias_state"], "consumed")
        next_job = "next-job"
        next_assignment = store.claim_alias(batch["batch_id"], 0, next_job)
        self.assertIsNotNone(next_assignment)
        self.assertNotEqual(next_assignment["alias"], assignment["alias"])


if __name__ == "__main__":
    unittest.main()
