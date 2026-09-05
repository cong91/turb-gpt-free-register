"""Gmail API URL 取码客户端单元测试（全程 mock HTTP 请求）。"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from core import registration_service
from core.gmail_aliases import generate_gmail_dual_domain_aliases
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.gmail_api_url_client import (
    GmailApiUrlAccount,
    GmailApiUrlBatchError,
    GmailApiUrlError,
    _fetch_code_once,
    _reconcile_batch_queue,
    acknowledge_verification_code,
    create_registration_batch,
    get_account_context,
    get_email_from_batch,
    pick_account,
    poll_verification_code,
    release_account,
)
from core.gmail_batch_store_base import Assignment


class FakeHttpError(RuntimeError):
    """HTTP error used only by the response test double."""


class _Response:
    """模拟 HTTP 响应对象。"""
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHttpError(f"HTTP {self.status_code}")


class GmailApiUrlClientTests(unittest.TestCase):
    """Gmail API URL 客户端测试套件。"""

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_create_registration_batch_generates_twelve_aliases_per_source(
        self, mock_claim, mock_store_factory
    ):
        sources = [
            {
                "email": "haldirk517517@gmail.com",
                "code_url": "https://api.example/haldirk",
            },
            {
                "email": "brookebudd8354@gmail.com",
                "code_url": "https://api.example/brookebudd",
            },
            {
                "email": "charleskeith1351@gmail.com",
                "code_url": "https://api.example/charles",
            },
        ]
        mock_claim.side_effect = sources
        mock_store_factory.return_value.create_batch_multi.return_value = "batch-36"

        batch_id = create_registration_batch(36, aliases_per_email=12)

        self.assertEqual(batch_id, "batch-36")
        groups = mock_store_factory.return_value.create_batch_multi.call_args.args[0]
        self.assertEqual([len(group["aliases"]) for group in groups], [12, 12, 12])
        self.assertEqual(sum(len(group["aliases"]) for group in groups), 36)
        for source, group in zip(sources, groups):
            self.assertNotIn(source["email"], group["aliases"])

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_create_registration_batch_reuses_used_sources_with_alias_capacity(
        self, mock_claim, mock_store_factory
    ):
        source = {
            "email": "used@gmail.com",
            "code_url": "https://api.example/source",
            "status": "used",
            "_claimed_from_available": False,
        }

        def claim_source(*, include_used=False, exclude_emails=None):
            if not include_used or source["email"] in (exclude_emails or set()):
                return None
            return dict(source)

        mock_claim.side_effect = claim_source
        mock_store_factory.return_value.list_unavailable_aliases_for_code_url.return_value = set()
        mock_store_factory.return_value.create_batch_multi.return_value = "batch-used-source"

        batch_id = create_registration_batch(1, aliases_per_email=1)

        self.assertEqual(batch_id, "batch-used-source")
        mock_store_factory.return_value.create_batch_multi.assert_called_once()

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_create_registration_batch_skips_alias_used_by_same_source_record(
        self, mock_claim, mock_store_factory
    ):
        source = {
            "email": "source@gmail.com",
            "code_url": "https://api.example/source",
        }
        mock_claim.return_value = source
        store = mock_store_factory.return_value
        store.list_allocated_aliases_for_code_url.return_value = {"s.ource@gmail.com"}
        store.create_batch_multi.return_value = "batch-next"

        batch_id = create_registration_batch(1, aliases_per_email=1)

        self.assertEqual(batch_id, "batch-next")
        groups = store.create_batch_multi.call_args.args[0]
        self.assertEqual(groups[0]["aliases"], ["so.urce@gmail.com"])
        store.list_allocated_aliases_for_code_url.assert_called_once_with(source["code_url"])

    def test_create_registration_batch_skips_root_collision_and_uses_later_source(self):
        """A dotted/plus variant owned by another URL must not abort selection."""
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_json = root / "gmail-pool.json"
            raw_txt = root / "gmail-pool.txt"
            state = root / "turb.sqlite3"
            first_source = "same@gmail.com"
            second_source = "s.ame@gmail.com"
            third_source = "different@gmail.com"
            first_url = "https://api.example/first"
            second_url = "https://api.example/second"
            third_url = "https://api.example/third"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", raw_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", raw_txt),
                patch.object(db, "_SQLITE_PATH", state),
                patch.object(db, "_DEFAULT_SQLITE_PATH", state),
                patch.object(db, "_SQLITE_READY", False),
            ):
                db.import_gmail_api_url_emails([
                    {"email": first_source, "code_url": first_url},
                    {"email": second_source, "code_url": second_url},
                    {"email": third_source, "code_url": third_url},
                ])
                store = GmailApiUrlBatchStore(state)
                existing = generate_gmail_dual_domain_aliases(first_source, limit=1)[0]
                store.create_batch_multi([{
                    "source_email": first_source,
                    "code_url": first_url,
                    "aliases": [existing],
                }])

                with patch("core.gmail_api_url_client._batch_store", return_value=store):
                    batch_id = create_registration_batch(3, aliases_per_email=2)

                with closing(sqlite3.connect(state)) as connection:
                    rows = connection.execute(
                        "SELECT code_url, email FROM gmail_api_url_batch_items "
                        "WHERE batch_id = ? ORDER BY position",
                        (batch_id,),
                    ).fetchall()
                items = [{"code_url": row[0], "email": row[1]} for row in rows]
                self.assertEqual(len(items), 3)
                self.assertEqual(sum(item["code_url"] == first_url for item in items), 2)
                self.assertEqual(sum(item["code_url"] == third_url for item in items), 1)
                self.assertNotIn(second_url, {item["code_url"] for item in items})

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_create_registration_batch_skips_alias_owned_by_another_code_url(
        self, mock_claim, mock_store_factory
    ):
        source = {
            "email": "source@gmail.com",
            "code_url": "https://api.example/source",
        }
        mock_claim.return_value = source
        store = mock_store_factory.return_value
        store.list_allocated_aliases_for_code_url.return_value = set()
        store.list_globally_unavailable_aliases.return_value = set()
        store.has_alias_for_other_code_url.side_effect = (
            lambda alias, code_url: alias == "so.urce@gmail.com"
        )
        store.create_batch_multi.return_value = "batch-next"

        batch_id = create_registration_batch(1, aliases_per_email=1)

        self.assertEqual(batch_id, "batch-next")
        groups = store.create_batch_multi.call_args.args[0]
        self.assertEqual(groups[0]["aliases"], ["source+41cf6@gmail.com"])
        store.has_alias_for_other_code_url.assert_any_call(
            "so.urce@gmail.com", source["code_url"]
        )

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.release_gmail_api_url_email")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_exhausted_source_is_not_reopened_by_batch_rollback(
        self, mock_claim, mock_release, mock_store_factory
    ):
        source = {
            "email": "source@gmail.com",
            "code_url": "https://api.example/source",
        }
        mock_claim.side_effect = [source, None]
        store = mock_store_factory.return_value
        store.list_allocated_aliases_for_code_url.return_value = set(
            generate_gmail_dual_domain_aliases(source["email"])
        )

        with self.assertRaises(GmailApiUrlBatchError):
            create_registration_batch(1, aliases_per_email=1)

        mock_release.assert_called_once_with(
            source["email"],
            "exhausted",
            "Record đã dùng hết alias Gmail khả dụng",
        )

    @patch("core.gmail_api_url_client._batch_store")
    @patch("core.db.release_gmail_api_url_email")
    @patch("core.db.claim_next_gmail_api_url_email")
    def test_pending_alias_does_not_terminalize_raw_source(
        self, mock_claim, mock_release, mock_store_factory
    ):
        source = {
            "email": "source@gmail.com",
            "code_url": "https://api.example/source",
            "_claimed_from_available": True,
        }
        mock_claim.side_effect = [source, None]
        store = mock_store_factory.return_value
        store.list_allocated_aliases_for_code_url.return_value = set(
            generate_gmail_dual_domain_aliases(source["email"])
        )
        store.list_globally_unavailable_aliases.return_value = set()
        store.has_pending_alias_for_code_url.return_value = True

        with self.assertRaises(GmailApiUrlBatchError):
            create_registration_batch(1, aliases_per_email=1)

        mock_release.assert_not_called()
        store.has_pending_alias_for_code_url.assert_called_once_with(source["code_url"])

    def test_active_last_alias_does_not_mark_raw_source_exhausted(self):
        """A temporarily owned final alias must remain usable after the lock drains."""
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_json = root / "gmail-pool.json"
            raw_txt = root / "gmail-pool.txt"
            state = root / "gmail-state.sqlite3"
            source_email = "activesource123@gmail.com"
            code_url = "https://api.example/active-source"
            aliases = generate_gmail_dual_domain_aliases(source_email)
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", raw_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", raw_txt),
                patch.object(db, "_SQLITE_PATH", state),
                patch.object(db, "_DEFAULT_SQLITE_PATH", state),
                patch.object(db, "_SQLITE_READY", False),
            ):
                db.import_gmail_api_url_emails([
                    {"email": source_email, "code_url": code_url},
                ])
                store = GmailApiUrlBatchStore(state)
                batch_id = store.create_batch_multi([
                    {
                        "source_email": source_email,
                        "code_url": code_url,
                        "aliases": aliases,
                    },
                ])
                for index in range(len(aliases) - 1):
                    assignment = store.claim(batch_id, f"completed-{index}")
                    self.assertTrue(store.complete(assignment.assignment_id))
                active = store.claim(batch_id, "active-last")
                self.assertIsNotNone(active)

                with patch(
                    "core.gmail_api_url_client._batch_store",
                    return_value=store,
                ), self.assertRaises(GmailApiUrlBatchError):
                    create_registration_batch(1, aliases_per_email=1)

                raw_row = db.get_gmail_api_url_email_by_email(source_email)
                self.assertEqual(raw_row["status"], "used")
                self.assertFalse(db.is_gmail_api_url_source_blocked(source_email))

    def test_unavailable_aliases_for_code_url_are_scoped_to_source_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GmailApiUrlBatchStore(f"{temp_dir}/batch.db")
            batch_id = store.create_batch_multi([
                {
                    "source_email": "source-a@gmail.com",
                    "code_url": "https://api.example/source-a",
                    "aliases": ["a.one@gmail.com"],
                },
            ])
            store.create_batch_multi([
                {
                    "source_email": "source-b@gmail.com",
                    "code_url": "https://api.example/source-b",
                    "aliases": ["b.one@gmail.com"],
                },
            ])
            store.claim(batch_id, "job")

            self.assertEqual(
                store.list_unavailable_aliases_for_code_url("https://api.example/source-a"),
                {"a.one@gmail.com"},
            )

    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch("core.gmail_api_url_client._batch_store")
    def test_get_email_from_batch_waits_for_temporary_code_url_lock(
        self, mock_store_factory, mock_sleep
    ):
        assignment = Assignment(
            "assignment-1",
            "batch-1",
            "alias@gmail.com----https://api.example/code",
            "job-2",
            "active",
        )
        store = mock_store_factory.return_value
        store.list_active_assignments.return_value = []
        store.list_waiting_jobs.return_value = []
        store.claim_waiting.side_effect = [None, assignment]
        store.batch_status.return_value = {
            "exhausted_batch": False,
            "pending": 1,
            "active_assignments": 1,
            "waiting_jobs": 1,
            "available_code_urls": 0,
        }

        account = get_email_from_batch(
            "batch-1",
            "job-2",
            wait_timeout=1,
            poll_interval=0,
        )

        self.assertEqual(account.email, "alias@gmail.com")
        self.assertEqual(store.claim_waiting.call_count, 2)
        mock_sleep.assert_called_once_with(0)

    @patch("core.registration_service.is_stop_requested", return_value=True)
    @patch("core.gmail_api_url_client._batch_store")
    def test_get_email_from_batch_stops_queued_job_after_lane_quarantine(
        self, mock_store_factory, _mock_is_stop_requested
    ):
        store = mock_store_factory.return_value

        with self.assertRaises(registration_service.StopRequested):
            get_email_from_batch("batch-1", "7", wait_timeout=30, poll_interval=0)

        store.claim_waiting.assert_not_called()
        store.cancel_waiter.assert_called_once_with(
            "batch-1", "7", "job stopped by email lane quarantine"
        )

    @patch("core.db.get_account_by_email", return_value={"id": 91})
    @patch("core.db.get_job")
    def test_reconcile_completes_terminal_assignment_and_cancels_waiter(
        self, mock_get_job, _mock_get_account
    ):
        store = Mock()
        store.list_active_assignments.return_value = [
            Assignment(
                "assignment-1",
                "batch-1",
                "alias@gmail.com----https://api.example/code",
                "7",
                "active",
            )
        ]
        store.list_waiting_jobs.return_value = ["8"]
        store.list_reusable_assignments.return_value = []
        mock_get_job.side_effect = [
            {"id": 7, "status": "failed", "account_id": 91},
            {"id": 8, "status": "cancelled"},
        ]

        _reconcile_batch_queue(store, "batch-1")

        store.complete.assert_called_once_with("assignment-1")
        store.release.assert_not_called()
        store.cancel_waiter.assert_called_once_with(
            "batch-1", "8", "terminal job reconciliation"
        )

    @patch("core.db.get_account_by_email", return_value=None)
    @patch("core.db.get_job", return_value={"id": 9, "status": "failed", "account_id": None})
    def test_reconcile_discards_released_alias_from_failed_job(
        self, _mock_get_job, _mock_get_account
    ):
        store = Mock()
        store.list_active_assignments.return_value = []
        store.list_reusable_assignments.return_value = [
            Assignment(
                "assignment-old",
                "batch-1",
                "old@gmail.com----https://api.example/code",
                "9",
                "released",
            )
        ]
        store.list_waiting_jobs.return_value = []

        _reconcile_batch_queue(store, "batch-1")

        store.discard.assert_called_once_with(
            "assignment-old", reason="failed job reconciliation"
        )
        store.release.assert_not_called()

    @patch("core.registration_service.is_job_active", return_value=False)
    @patch("core.db.update_job")
    @patch("core.db.get_account_by_email", return_value=None)
    @patch("core.db.get_job", return_value={"id": 15, "status": "running", "account_id": None})
    def test_reconcile_discards_running_assignment_without_live_worker(
        self,
        _mock_get_job,
        _mock_get_account,
        mock_update_job,
        _mock_is_job_active,
    ):
        store = Mock()
        store.list_active_assignments.return_value = [
            Assignment(
                "assignment-orphan",
                "batch-1",
                "orphan@gmail.com----https://api.example/code",
                "15",
                "active",
            )
        ]
        store.list_reusable_assignments.return_value = []
        store.list_waiting_jobs.return_value = []

        _reconcile_batch_queue(store, "batch-1")

        mock_update_job.assert_called_once()
        self.assertEqual(mock_update_job.call_args.kwargs["status"], "failed")
        store.discard.assert_called_once_with(
            "assignment-orphan", reason="orphaned running job reconciliation"
        )
        store.release.assert_not_called()

    @patch("core.registration_service.is_job_active", return_value=True)
    @patch("core.db.update_job")
    @patch("core.db.get_job", return_value={"id": 16, "status": "running", "account_id": None})
    def test_reconcile_preserves_assignment_owned_by_live_worker(
        self, _mock_get_job, mock_update_job, _mock_is_job_active
    ):
        store = Mock()
        store.list_active_assignments.return_value = [
            Assignment(
                "assignment-live",
                "batch-1",
                "live@gmail.com----https://api.example/code",
                "16",
                "active",
            )
        ]
        store.list_reusable_assignments.return_value = []
        store.list_waiting_jobs.return_value = []

        _reconcile_batch_queue(store, "batch-1")

        mock_update_job.assert_not_called()
        store.discard.assert_not_called()
        store.release.assert_not_called()
        store.complete.assert_not_called()

    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    def test_poll_returns_otp_on_code_0_success(self, _sleep, mock_get):
        """code=0 时返回 OTP。"""
        mock_get.return_value = _Response({"code": 0, "data": {"code": "123456"}})
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        code = poll_verification_code(account, max_wait=5, poll_interval=1)

        self.assertEqual(code, "123456")
        self.assertEqual(mock_get.call_count, 1)

    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.db.is_gmail_api_url_account_blocked", return_value=True)
    @patch("core.db.is_gmail_api_url_code_url_failed", return_value=False)
    @patch("core.gmail_api_url_client._batch_store")
    def test_poll_rejects_blocked_source_before_provider_request(
        self,
        mock_batch_store,
        _mock_failed_url,
        _mock_account_blocked,
        mock_get,
    ):
        mock_batch_store.return_value.path = "runtime/turb.sqlite3"
        account = GmailApiUrlAccount("blocked+alias@gmail.com", "https://api.example/blocked")

        with self.assertRaisesRegex(GmailApiUrlError, "disabled or terminally retired"):
            poll_verification_code(account, max_wait=5, poll_interval=0)

        mock_get.assert_not_called()

    @patch("core.gmail_api_url_client.time.time", side_effect=[0.0, 0.0, 2.0])
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_ignores_malformed_otp_payload(self, mock_get, _sleep, _time):
        mock_get.return_value = _Response({"code": 0, "data": {"code": "12ab"}})
        account = GmailApiUrlAccount("test@gmail.com", "http://example.com/otp")

        with self.assertRaises(GmailApiUrlError):
            poll_verification_code(account, max_wait=1, poll_interval=0)

    @patch("core.gmail_api_url_client._record_latest_otp")
    @patch("core.gmail_api_url_client._get_latest_otp", return_value="111111")
    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_uses_persisted_latest_otp_as_baseline(self, mock_get, get_latest, record_latest):
        mock_get.return_value = _Response({"code": 0, "data": {"code": "222222"}})
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        code = poll_verification_code(account, max_wait=5, poll_interval=1, after_ts=123.0)

        self.assertEqual(code, "222222")
        get_latest.assert_called_once_with(account)
        record_latest.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    @patch("core.gmail_api_url_client._record_latest_otp")
    @patch("core.gmail_api_url_client._get_latest_otp", return_value="111111")
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_waits_only_until_persisted_baseline_changes(
        self, mock_get, _sleep, get_latest, record_latest
    ):
        mock_get.side_effect = [
            _Response({"code": 0, "data": {"code": "111111"}}),
            _Response({"code": 0, "data": {"code": "333333"}}),
        ]
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        code = poll_verification_code(account, max_wait=5, poll_interval=1, after_ts=123.0)

        self.assertEqual(code, "333333")
        get_latest.assert_called_once_with(account)
        record_latest.assert_not_called()
        self.assertEqual(mock_get.call_count, 2)

    @patch("core.gmail_api_url_client._record_latest_otp")
    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_accepts_first_code_visible_after_otp_request(self, mock_get, record_latest):
        """Lần poll đầu tiên diễn ra sau khi UI đã gửi OTP, nên mã hiện tại phải được nhận."""
        mock_get.return_value = _Response({"code": 0, "data": {"code": "123456"}})
        account = GmailApiUrlAccount("test@gmail.com", "http://example.com/otp")

        code = poll_verification_code(account, max_wait=5, poll_interval=1, after_ts=123.0)

        self.assertEqual(code, "123456")
        record_latest.assert_not_called()
        self.assertEqual(mock_get.call_count, 1)

    @patch("core.gmail_api_url_client._record_latest_otp")
    def test_acknowledge_verification_code_persists_only_after_validation(self, record_latest):
        account = GmailApiUrlAccount("test@gmail.com", "http://example.com/otp")

        acknowledge_verification_code(account, "123456")

        record_latest.assert_called_once_with(account, "123456")

    @patch("core.gmail_api_url_client._record_latest_otp")
    def test_acknowledge_verification_code_rejects_malformed_code(self, record_latest):
        account = GmailApiUrlAccount("test@gmail.com", "http://example.com/otp")

        with self.assertRaises(ValueError):
            acknowledge_verification_code(account, "12ab56")

        record_latest.assert_not_called()

    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    def test_poll_continues_on_code_601_waiting(self, _sleep, mock_get):
        """code=601 时继续轮询，直到拿到 code=0。"""
        mock_get.side_effect = [
            _Response({"code": 601}),
            _Response({"code": 601}),
            _Response({"code": 0, "data": {"code": "789012"}}),
        ]
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        code = poll_verification_code(account, max_wait=10, poll_interval=1)

        self.assertEqual(code, "789012")
        self.assertEqual(mock_get.call_count, 3)

    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_raises_refund_error_on_code_602(self, mock_get):
        """code=602 时抛出错误，包含退款提示。"""
        mock_get.return_value = _Response({"code": 602, "message": "Provider error"})
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*refund"):
            poll_verification_code(account, max_wait=5, poll_interval=1)

        self.assertEqual(mock_get.call_count, 1)

    @patch("core.gmail_api_url_client.requests.get")
    def test_poll_602_quarantines_raw_and_canonical_siblings(self, mock_get):
        """A terminal provider response retires every owner of the URL."""
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_json = root / "gmail-pool.json"
            raw_txt = root / "gmail-pool.txt"
            store = GmailApiUrlBatchStore(root / "gmail-state.sqlite3")
            code_url = "https://api.example/terminal"

            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", raw_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", raw_txt),
            ):
                self.assertEqual(
                    db.import_gmail_api_url_emails([
                        {"email": "source-one@gmail.com", "code_url": code_url},
                        {"email": "source-two@gmail.com", "code_url": code_url},
                    ]),
                    (2, 0),
                )
                batch_id = store.create_batch_multi([
                    {
                        "source_email": "source-one@gmail.com",
                        "code_url": code_url,
                        "aliases": ["alias-one@gmail.com", "alias-two@gmail.com"],
                    },
                ])
                assignment = store.claim(batch_id, "job-terminal")
                mock_get.return_value = _Response({"code": 602, "message": "expired"})

                with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*expired"):
                    poll_verification_code(
                        GmailApiUrlAccount("alias-one@gmail.com", code_url),
                        max_wait=5,
                        poll_interval=0,
                        sqlite_path=store.path,
                    )

                rows = db.list_gmail_api_url_email_pool(limit=10)
                self.assertEqual({row["status"] for row in rows}, {"failed"})
                self.assertEqual(store.get_assignment(assignment.assignment_id).state, "failed")
                self.assertEqual(store.batch_status(batch_id)["pending"], 0)

                mock_get.reset_mock()
                with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*quarantined"):
                    poll_verification_code(
                        GmailApiUrlAccount("alias-two@gmail.com", code_url),
                        max_wait=5,
                        poll_interval=0,
                        sqlite_path=store.path,
                    )
                mock_get.assert_not_called()

    @patch("core.gmail_api_url_client.requests.get")
    def test_string_provider_code_602_is_terminal(self, mock_get):
        mock_get.return_value = _Response({"code": "602", "message": "expired"})

        with self.assertRaisesRegex(GmailApiUrlError, r"code=602.*expired"):
            _fetch_code_once("https://api.example/code")

    @patch("core.gmail_api_url_client.requests.get")
    def test_http_status_602_is_terminal(self, mock_get):
        mock_get.return_value = _Response({}, status_code=602)

        with self.assertRaisesRegex(GmailApiUrlError, r"code=602"):
            _fetch_code_once("https://api.example/code")

    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    def test_poll_timeout_raises_error(self, _sleep, mock_get):
        """轮询超时时抛出超时错误。"""
        mock_get.return_value = _Response({"code": 601})
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        with self.assertRaisesRegex(GmailApiUrlError, "Timeout"):
            poll_verification_code(account, max_wait=2, poll_interval=1)

        self.assertGreaterEqual(mock_get.call_count, 1)

    @patch("core.gmail_api_url_client.requests.get")
    @patch("core.gmail_api_url_client.time.sleep", return_value=None)
    def test_poll_retries_on_http_error(self, _sleep, mock_get):
        """HTTP 错误时重试，直到成功或超时。"""
        mock_get.side_effect = [
            _Response({"error": "server error"}, status_code=500),
            _Response({"code": 0, "data": {"code": "456789"}}),
        ]
        account = GmailApiUrlAccount(
            email="test@gmail.com",
            code_url="http://example.com/otp",
        )

        code = poll_verification_code(account, max_wait=10, poll_interval=1)

        self.assertEqual(code, "456789")
        self.assertEqual(mock_get.call_count, 2)

    @patch("core.db.claim_next_gmail_api_url_email")
    @patch("core.db.import_gmail_api_url_emails")
    def test_pick_account_returns_claimed_email(self, mock_import, mock_claim):
        """pick_account 返回从池中领取的账号。"""
        mock_import.return_value = (0, 0)
        mock_claim.return_value = {
            "id": 1,
            "email": "claimed@gmail.com",
            "code_url": "http://example.com/otp/claimed",
            "status": "used",
        }

        account = pick_account()

        self.assertEqual(account.email, "claimed@gmail.com")
        self.assertEqual(account.code_url, "http://example.com/otp/claimed")
        mock_claim.assert_called_once()

    @patch("core.db.claim_next_gmail_api_url_email", return_value=None)
    @patch("core.db.import_gmail_api_url_emails")
    @patch("core.db.gmail_api_url_email_pool_summary")
    def test_pick_account_raises_when_pool_empty(self, mock_summary, mock_import, mock_claim):
        """邮箱池为空时抛出错误。"""
        mock_import.return_value = (0, 0)
        mock_summary.return_value = {"total": 0, "available": 0}

        with self.assertRaisesRegex(GmailApiUrlError, "pool empty"):
            pick_account()

    @patch("core.db.get_gmail_api_url_email_by_email")
    def test_get_account_context_returns_account_from_db(self, mock_get):
        """get_account_context 从 DB 获取账号上下文。"""
        mock_get.return_value = {
            "email": "context@gmail.com",
            "code_url": "http://example.com/otp/context",
        }

        account = get_account_context("context@gmail.com")

        self.assertIsNotNone(account)
        self.assertEqual(account.email, "context@gmail.com")
        mock_get.assert_called_once_with("context@gmail.com")

    @patch("core.db.get_gmail_api_url_email_by_email", return_value=None)
    def test_get_account_context_returns_none_when_not_found(self, mock_get):
        """账号不存在时返回 None。"""
        account = get_account_context("missing@gmail.com")

        self.assertIsNone(account)
        mock_get.assert_called_once_with("missing@gmail.com")

    @patch("core.db.release_gmail_api_url_email")
    def test_release_account_delegates_to_db(self, mock_release):
        """release_account 委托给 DB 层释放账号。"""
        release_account("released@gmail.com", status="available", note="test note")

        mock_release.assert_called_once_with("released@gmail.com", "available", "test note")


if __name__ == "__main__":
    unittest.main()
