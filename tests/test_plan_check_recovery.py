import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


def _db_context(root: Path) -> ExitStack:
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


def _seed_interrupted_plan_check(account_id: int, *, token_expires_at: str | None) -> None:
    """Drive a real account into an interrupted (running) plan check state."""
    db.update_account_plan_check(
        acc_id=account_id,
        result={"ok": False, "error": "seed", "token_expires_at": token_expires_at},
    )
    db.claim_account_plan_check(acc_id=account_id, trigger="manual")
    db.mark_account_plan_check_running(account_id)


class PlanCheckRecoveryTests(unittest.TestCase):
    def test_recovery_marks_interrupted_failed_and_returns_requeueable_ids(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
            usable = db.insert_account(email="usable@example.com", access_token="token-a")
            expired = db.insert_account(email="expired@example.com", access_token="token-b")
            _seed_interrupted_plan_check(usable, token_expires_at=future)
            _seed_interrupted_plan_check(
                expired,
                token_expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            )

            recovery = db.recover_interrupted_plan_checks()

            self.assertEqual(recovery["recovered"], [usable, expired])
            self.assertEqual(recovery["requeueable"], [usable])
            row = db.get_account(expired)
            self.assertEqual(row["plan_check_status"], "failed")
            self.assertIn("WebUI 重启", str(row["plan_check_error"]))

    def test_recovery_does_not_requeue_accounts_without_token(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = db.insert_account(email="notoken@example.com", access_token="")
            _seed_interrupted_plan_check(account_id, token_expires_at=None)

            recovery = db.recover_interrupted_plan_checks()

            self.assertEqual(recovery["recovered"], [account_id])
            self.assertEqual(recovery["requeueable"], [])

    def test_startup_recovery_reenqueues_requeueable_accounts(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
            account_id = db.insert_account(
                email="recover@example.com", access_token="token-live"
            )
            _seed_interrupted_plan_check(account_id, token_expires_at=future)

            with patch(
                "core.plan_check_service.enqueue_account_plan_check",
                return_value={"accepted": True, "status": "queued"},
            ) as enqueue:
                create_app(auth_code="test-auth")

        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["account_id"], account_id)
        self.assertEqual(kwargs["email"], "recover@example.com")
        self.assertEqual(kwargs["access_token"], "token-live")
        self.assertEqual(kwargs["trigger"], "startup_recovery")


if __name__ == "__main__":
    unittest.main()
