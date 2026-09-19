import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import db, registration_service


class RegistrationNetworkIdentityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for target, value in (
            ("_JOBS_JSON", root / "jobs.json"),
            ("_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            ("_LOG_DIR", root / "logs"),
        ):
            patcher = patch.object(db, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, result):
        job = db.create_job(email_source="paymesh")
        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("user@example.com", "Test User", "1990-01-01"),
        ), patch("main.run_registration", return_value=result), patch.object(
            registration_service, "_release_unconsumed_job_email"
        ), patch.object(registration_service, "_disable_job_email"):
            registration_service._run_one_job(job["id"], job["log_file"])
        return db.get_job(job["id"])

    def test_success_job_persists_network_identity(self):
        identity = {"browser_egress_ip": "8.8.8.8", "verified": True}
        job = self._run({
            "success": True,
            "email": "user@example.com",
            "account_id": 17,
            "network_identity": identity,
        })

        self.assertEqual(job["status"], "success")
        self.assertEqual(job["network_identity"], identity)

    def test_failed_job_persists_network_identity(self):
        identity = {"tunnel_egress_ip": "8.8.8.8", "verified": False}
        job = self._run({
            "success": False,
            "email": "user@example.com",
            "error": "browser mismatch",
            "network_identity": identity,
        })

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["network_identity"], identity)

    def test_automated_alias_holds_mailbox_lock_through_failure_cleanup(self):
        job = db.create_job(email_source="automated_email_api")
        mailbox_lock = MagicMock()

        def run_registration(**_kwargs):
            self.assertTrue(mailbox_lock.acquire.called)
            self.assertFalse(mailbox_lock.release.called)
            return {"success": False, "email": "alias@gmail.com", "error": "failed"}

        def release_email(*_args, **_kwargs):
            self.assertTrue(mailbox_lock.acquire.called)
            self.assertFalse(mailbox_lock.release.called)
            return False

        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("alias@gmail.com", "Test User", "1990-01-01"),
        ), patch(
            "core.automated_email_api_client.get_account_context",
            return_value=SimpleNamespace(query_email="source@gmail.com"),
        ), patch(
            "core.automated_email_api_client.registration_mailbox_lock",
            return_value=mailbox_lock,
        ), patch(
            "main.run_registration", side_effect=run_registration
        ), patch.object(
            registration_service,
            "_release_unconsumed_job_email",
            side_effect=release_email,
        ):
            registration_service._run_one_job(job["id"], job["log_file"])

        mailbox_lock.acquire.assert_called_once_with()
        mailbox_lock.release.assert_called_once_with()

    def test_otpmail_alias_holds_order_lock_through_failure_cleanup(self):
        job = db.create_job(email_source="otpmail")
        order_lock = MagicMock()
        released = []

        def run_registration(**_kwargs):
            self.assertTrue(order_lock.acquire.called)
            self.assertFalse(order_lock.release.called)
            return {"success": False, "email": "root@gmail.com", "error": "failed"}

        def release_email(*_args, **_kwargs):
            self.assertTrue(order_lock.acquire.called)
            self.assertFalse(order_lock.release.called)
            released.append((_args, _kwargs))
            return False

        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("alias@gmail.com", "Test User", "1990-01-01"),
        ), patch(
            "core.automated_email_api_client.get_account_context",
            return_value=None,
        ), patch(
            "core.otpgmail_client.get_account_context",
            return_value=SimpleNamespace(order_id="order-1"),
        ), patch(
            "core.otpgmail_client.registration_order_lock",
            return_value=order_lock,
        ), patch(
            "main.run_registration", side_effect=run_registration
        ), patch.object(
            registration_service,
            "_release_unconsumed_job_email",
            side_effect=release_email,
        ):
            registration_service._run_one_job(job["id"], job["log_file"])

        order_lock.acquire.assert_called_once_with()
        order_lock.release.assert_called_once_with()
        self.assertEqual(
            released,
            [(("alias@gmail.com", "failed"), {"discard_on_failure": True})],
        )

    def test_otpmail_stop_preserves_assigned_alias_when_driver_returns_parent_mailbox(self):
        job = db.create_job(email_source="otpmail")
        with patch.object(
            registration_service,
            "_prepare_registration_args",
            return_value=("alias@gmail.com", "Test User", "1990-01-01"),
        ), patch.object(
            registration_service,
            "_activate_job",
            return_value=True,
        ), patch.object(
            registration_service,
            "is_stop_requested",
            side_effect=[False, True],
        ), patch(
            "core.otpgmail_client.get_account_context",
            return_value=SimpleNamespace(order_id="order-1"),
        ), patch(
            "core.otpgmail_client.registration_order_lock",
            return_value=MagicMock(),
        ), patch(
            "main.run_registration",
            return_value={"success": False, "email": "root@gmail.com"},
        ), patch.object(
            registration_service,
            "_release_unconsumed_job_email",
        ):
            registration_service._run_one_job(job["id"], job["log_file"])

        self.assertEqual(db.get_job(job["id"])["email"], "alias@gmail.com")


if __name__ == "__main__":
    unittest.main()
