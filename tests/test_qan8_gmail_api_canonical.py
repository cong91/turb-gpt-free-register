import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator
from core.qan8_gmail_api_client import Qan8Order
from core.qan8_gmail_api_store import Qan8GmailApiStore


class _CanonicalClient:
    def __init__(self, delivery=None):
        self.delivery = delivery
        self.created = []
        self.proxy_url = ""

    def create_order(self, out_order_no, *, quantity=1):
        self.created.append((out_order_no, quantity))
        if self.delivery is None:
            raise AssertionError("QAN8 purchase should not run while canonical aliases are available")
        return Qan8Order(
            order_no=out_order_no,
            status="completed",
            delivery=self.delivery,
        )

    def parse_delivery(self, delivery):
        from core.qan8_gmail_api_client import Qan8SourceRecord

        email, code_url = str(delivery).split("----", 1)
        return [Qan8SourceRecord(email=email, code_url=code_url)]


class Qan8CanonicalRuntimeTests(unittest.TestCase):
    def test_provider_acquisition_uses_canonical_batch_for_qan8_source(self):
        from core import email_provider

        allocator = MagicMock()
        allocator.acquire_gmail_api_account.return_value = type(
            "Account", (), {"email": "alias@gmail.com"}
        )()
        with patch(
            "core.email_provider.Qan8GmailApiAllocator",
            return_value=allocator,
        ):
            email = email_provider._pick_from_source(
                "qan8_gmail_api",
                job_id=7,
                gmail_api_url_batch_id="gmail-batch",
                qan8_gmail_api_batch_id="qan8-batch",
                qan8_gmail_api_lane_id=2,
            )

        self.assertEqual(email, "alias@gmail.com")
        allocator.acquire_gmail_api_account.assert_called_once()
        kwargs = allocator.acquire_gmail_api_account.call_args.kwargs
        self.assertEqual(kwargs["batch_id"], "qan8-batch")
        self.assertEqual(kwargs["gmail_batch_id"], "gmail-batch")
        self.assertEqual(kwargs["job_id"], 7)
        self.assertEqual(kwargs["lane_id"], 2)
        self.assertTrue(callable(kwargs["stop_check"]))

    @patch("core.email_provider._current_otp_job_id", return_value=17)
    @patch("core.gmail_api_url_client.get_batch_account_context", return_value=object())
    @patch("core.gmail_api_url_client.release_account", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="qan8_gmail_api")
    def test_qan8_cleanup_uses_job_owned_canonical_assignment(
        self,
        _resolve,
        mock_release,
        mock_context,
        _job_id,
    ):
        from core import email_provider

        self.assertTrue(
            email_provider.release_email_if_unconsumed(
                "alias@gmail.com",
                note="registration failed",
                discard_on_failure=True,
            )
        )
        mock_context.assert_called_once_with("alias@gmail.com", job_id=17)
        mock_release.assert_called_once_with(
            "alias@gmail.com",
            status="failed",
            note="registration failed",
            job_id=17,
        )

    def test_qan8_claims_existing_canonical_alias_without_qan8_assignment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_batch_multi([
                {
                    "source_email": "source@gmail.com",
                    "code_url": "https://mail.example/source",
                    "aliases": ["existing+one@gmail.com", "existing+two@gmail.com"],
                }
            ])
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=2,
            )
            allocator = Qan8GmailApiAllocator(
                client=_CanonicalClient(),
                store=q8_store,
                poll_interval=0,
            )

            account = allocator.acquire_gmail_api_account(
                q8_batch["batch_id"],
                gmail_batch_id,
                "job-1",
                0,
                wait_timeout=0,
            )

            self.assertIn(
                account.email,
                {"existing+one@gmail.com", "existing+two@gmail.com"},
            )
            self.assertEqual(account.code_url, "https://mail.example/source")
            self.assertIsNotNone(gmail_store.find_active_assignment_for_job("job-1"))
            self.assertIsNone(q8_store.get_assignment("job-1"))
            status = q8_store.batch_status(q8_batch["batch_id"])
            self.assertEqual(status["active_sources"], 1)
            self.assertEqual(status["remaining_aliases"], 0)
            self.assertEqual(status["pending_aliases"], 2)

    def test_qan8_claim_rejects_shadowed_legacy_canonical_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_store.create_batch_multi([
                {
                    "source_email": "foobar@gmail.com",
                    "code_url": "https://mail.example/primary",
                    "aliases": ["foobar@gmail.com"],
                }
            ])
            shadow_batch_id = gmail_store.create_empty_batch()
            shadow_inventory_id = "foo.bar@gmail.com----https://mail.example/shadow"
            with gmail_store._transaction() as connection:
                connection.execute(
                    "INSERT INTO gmail_api_url_batch_items "
                    "(batch_id, inventory_id, email, code_url, position) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (
                        shadow_batch_id,
                        shadow_inventory_id,
                        "foo.bar@gmail.com",
                        "https://mail.example/shadow",
                    ),
                )
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=1,
            )
            q8_store.create_source_group(
                q8_batch["batch_id"],
                0,
                "foo.bar@gmail.com",
                "https://mail.example/shadow",
                ["foo.bar@gmail.com"],
                gmail_alias_refs={
                    "foo.bar@gmail.com": (shadow_batch_id, shadow_inventory_id),
                },
            )

            self.assertIsNone(
                q8_store.claim_alias(q8_batch["batch_id"], 0, "shadow-job")
            )
            self.assertIsNone(gmail_store.find_active_assignment_for_job("shadow-job"))
            self.assertEqual(
                q8_store.batch_status(q8_batch["batch_id"])["active_sources"],
                0,
            )

    def test_qan8_materializes_raw_gmail_pool_before_purchase(self):
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pool_json = temp_path / "gmail-pool.json"
            pool_txt = temp_path / "gmail-pool.txt"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "rawsource123@gmail.com",
                        "code_url": "https://mail.example/raw-source",
                    }
                ])
                path = temp_path / "state.sqlite3"
                q8_store = Qan8GmailApiStore(path)
                gmail_store = GmailApiUrlBatchStore(path)
                gmail_batch_id = gmail_store.create_empty_batch()
                q8_batch = q8_store.create_batch(
                    1,
                    requested_workers=1,
                    aliases_per_source=12,
                )
                client = _CanonicalClient()
                allocator = Qan8GmailApiAllocator(
                    client=client,
                    store=q8_store,
                    poll_interval=0,
                )

                account = allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "job-raw-pool",
                    0,
                    wait_timeout=1,
                )

                self.assertEqual(client.created, [])
                self.assertEqual(account.code_url, "https://mail.example/raw-source")
                self.assertIsNotNone(
                    gmail_store.find_active_assignment_for_job("job-raw-pool")
                )
                self.assertEqual(
                    gmail_store.batch_status(gmail_batch_id)["total"],
                    12,
                )

    def test_disabled_raw_source_blocks_canonical_claim_until_reenabled(self):
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pool_json = temp_path / "gmail-pool.json"
            pool_txt = temp_path / "gmail-pool.txt"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "disabledroot123@gmail.com",
                        "code_url": "https://mail.example/disabled",
                    }
                ])
                path = temp_path / "state.sqlite3"
                store = GmailApiUrlBatchStore(path)
                batch_id = store.create_empty_batch()
                from core.gmail_api_url_client import materialize_next_available_source

                self.assertTrue(materialize_next_available_source(batch_id, store=store))
                active = store.claim_any_available("job-active-before-disable")
                self.assertIsNotNone(active)
                db.release_gmail_api_url_email(
                    "disabledroot123@gmail.com",
                    "disabled",
                    "operator disabled source",
                )
                self.assertTrue(store.complete(active.assignment_id))

                self.assertIsNone(store.claim_any_available("job-disabled"))
                self.assertIsNone(store.claim_waiting(batch_id, "job-disabled-waiter"))
                self.assertEqual(store.list_waiting_jobs(batch_id), ["job-disabled-waiter"])

                db.release_gmail_api_url_email(
                    "disabledroot123@gmail.com",
                    "available",
                    "operator re-enabled source",
                )
                assignment = store.claim_any_available("job-reenabled")
                self.assertIsNotNone(assignment)

    def test_qan8_status_excludes_blocked_canonical_aliases(self):
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_empty_batch()
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=1,
            )
            q8_store.create_source_group(
                q8_batch["batch_id"],
                0,
                "blockedstatus123@gmail.com",
                "https://mail.example/blocked-status",
                ["blockedstatus123@gmail.com"],
                gmail_batch_id=gmail_batch_id,
            )

            with patch.object(
                db,
                "gmail_api_url_blocked_canonical_roots",
                return_value={"blockedstatus123@gmail.com"},
            ):
                status = q8_store.batch_status(q8_batch["batch_id"])

            self.assertEqual(status["active_sources"], 0)
            self.assertEqual(status["remaining_aliases"], 0)
            self.assertEqual(status["pending_aliases"], 0)

    def test_disabled_raw_source_blocks_direct_alias_otp_poll(self):
        from core import db, email_provider
        from core.gmail_api_url_client import GmailApiUrlError

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pool_json = temp_path / "gmail-pool.json"
            pool_txt = temp_path / "gmail-pool.txt"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "directdisabled123@gmail.com",
                        "code_url": "https://mail.example/direct-disabled",
                    }
                ])
                db.release_gmail_api_url_email(
                    "directdisabled123@gmail.com",
                    "disabled",
                    "operator disabled source",
                )
                account = {
                    "email": "directdisabled123+alias@gmail.com",
                    "code_url": "https://mail.example/direct-disabled",
                }
                with (
                    patch("config.email.USE_EMAIL_SERVICE", True),
                    patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"),
                    patch("core.gmail_api_url_client._batch_store") as batch_store,
                    patch("core.gmail_api_url_client.get_batch_account_context", return_value=None),
                    patch("core.db.get_gmail_api_url_email_by_email", return_value=account),
                    patch("core.gmail_api_url_client.poll_verification_code") as poll,
                    patch.object(email_provider, "_quarantine_code_url_after_provider_error"),
                    self.assertRaisesRegex(
                        GmailApiUrlError,
                        "disabled or terminally retired",
                    ),
                ):
                    batch_store.return_value.path = temp_path / "state.sqlite3"
                    email_provider.wait_for_otp(
                        "directdisabled123+alias@gmail.com",
                        after_ts=1.0,
                        max_wait=1,
                        poll_interval=0,
                    )
                poll.assert_not_called()

    def test_materializer_quarantines_available_sibling_for_failed_code_url(self):
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pool_json = temp_path / "gmail-pool.json"
            pool_txt = temp_path / "gmail-pool.txt"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "failedroot123@gmail.com",
                        "code_url": "https://mail.example/failed-url",
                    },
                    {
                        "email": "available123@gmail.com",
                        "code_url": "https://mail.example/failed-url",
                    },
                ])
                db.release_gmail_api_url_email(
                    "failedroot123@gmail.com",
                    "failed",
                    "provider error code=602",
                )
                path = temp_path / "state.sqlite3"
                store = GmailApiUrlBatchStore(path)
                batch_id = store.create_empty_batch()
                from core.gmail_api_url_client import materialize_next_available_source

                self.assertFalse(materialize_next_available_source(batch_id, store=store))
                rows = db.list_gmail_api_url_email_pool(limit=10)
                self.assertEqual(
                    {row["status"] for row in rows},
                    {"failed"},
                )

    def test_materializer_does_not_cross_raw_pool_and_canonical_store_scopes(self):
        """A custom canonical DB cannot consume the raw pool from another directory."""
        from core import db
        from core.gmail_api_url_client import materialize_next_available_source

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_root = root / "raw-pool"
            canonical_root = root / "canonical-store"
            raw_root.mkdir()
            canonical_root.mkdir()
            pool_json = raw_root / "gmail-pool.json"
            pool_txt = raw_root / "gmail-pool.txt"
            canonical_path = canonical_root / "state.sqlite3"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {
                        "email": "scopedraw123@gmail.com",
                        "code_url": "https://mail.example/scoped-raw",
                    }
                ])
                store = GmailApiUrlBatchStore(canonical_path)
                batch_id = store.create_empty_batch()

                self.assertFalse(materialize_next_available_source(batch_id, store=store))
                self.assertEqual(
                    db.get_gmail_api_url_email_by_email("scopedraw123@gmail.com")["status"],
                    "available",
                )
                self.assertEqual(store.batch_status(batch_id)["total"], 0)

    def test_qan8_purchase_does_not_cross_raw_pool_scope(self):
        """A failed raw URL in another store must not block a local purchase."""
        from core import db

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_root = root / "raw-pool"
            canonical_root = root / "canonical-store"
            raw_root.mkdir()
            canonical_root.mkdir()
            pool_json = raw_root / "gmail-pool.json"
            pool_txt = raw_root / "gmail-pool.txt"
            canonical_path = canonical_root / "state.sqlite3"
            failed_url = "https://mail.example/scoped-failed"
            with (
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", pool_json),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", pool_txt),
            ):
                db.import_gmail_api_url_emails([
                    {"email": "foreignfailed@gmail.com", "code_url": failed_url},
                ])
                db.release_gmail_api_url_email(
                    "foreignfailed@gmail.com",
                    "failed",
                    "provider error code=602",
                )
                q8_store = Qan8GmailApiStore(canonical_path)
                gmail_store = GmailApiUrlBatchStore(canonical_path)
                gmail_batch_id = gmail_store.create_empty_batch()
                q8_batch = q8_store.create_batch(
                    1,
                    requested_workers=1,
                    aliases_per_source=1,
                )
                allocator = Qan8GmailApiAllocator(
                    client=_CanonicalClient(
                        f"localpurchase@gmail.com----{failed_url}"
                    ),
                    store=q8_store,
                    poll_interval=0,
                )

                account = allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "job-local-scope",
                    0,
                    wait_timeout=1,
                )

                self.assertEqual(account.code_url, failed_url)
                self.assertIsNotNone(
                    gmail_store.find_active_assignment_for_job("job-local-scope")
                )
                self.assertEqual(
                    db.get_gmail_api_url_email_by_email("foreignfailed@gmail.com")["status"],
                    "failed",
                )

    def test_qan8_purchase_appends_to_target_canonical_batch_before_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_empty_batch()
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=2,
            )
            client = _CanonicalClient(
                "purchased@gmail.com----https://mail.example/purchased"
            )
            allocator = Qan8GmailApiAllocator(
                client=client,
                store=q8_store,
                poll_interval=0,
            )
            from core import db

            with (
                patch("core.db.record_gmail_api_url_email"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", Path(temp_dir) / "empty-pool.json"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", Path(temp_dir) / "empty-pool.txt"),
            ):
                account = allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "job-1",
                    0,
                    wait_timeout=1,
                )

            self.assertIn(
                account.email.rsplit("@", 1)[-1].casefold(),
                {"gmail.com", "googlemail.com"},
            )
            self.assertEqual(account.code_url, "https://mail.example/purchased")
            self.assertEqual(client.created[0][1], 1)
            self.assertEqual(
                gmail_store.batch_status(gmail_batch_id)["active_assignments"],
                1,
            )
            self.assertIsNone(q8_store.get_assignment("job-1"))
            q8_source = q8_store.get_current_source(q8_batch["batch_id"], 0)
            self.assertIsNotNone(q8_source)
            refs = q8_store.list_source_aliases(q8_source["source_group_id"])
            self.assertTrue(refs)
            self.assertEqual({row["gmail_batch_id"] for row in refs}, {gmail_batch_id})

            status = q8_store.batch_status(q8_batch["batch_id"])
            self.assertEqual(status["active_sources"], 1)
            self.assertEqual(status["remaining_aliases"], 0)
            self.assertEqual(status["pending_aliases"], len(refs))
            self.assertEqual(status["shared_remaining_aliases"], 0)
            self.assertEqual(status["shared_pending_aliases"], len(refs))
            self.assertEqual(status["shared_active_assignments"], 1)

            assignment = gmail_store.find_active_assignment_for_job("job-1")
            self.assertIsNotNone(assignment)
            self.assertTrue(gmail_store.complete(assignment.assignment_id))
            status = q8_store.batch_status(q8_batch["batch_id"])
            self.assertEqual(status["active_sources"], 1)
            self.assertEqual(status["remaining_aliases"], len(refs) - 1)

            next_assignment = gmail_store.claim_waiting(gmail_batch_id, "job-2")
            self.assertIsNotNone(next_assignment)
            self.assertTrue(gmail_store.complete(next_assignment.assignment_id))
            status = q8_store.batch_status(q8_batch["batch_id"])
            self.assertEqual(status["active_sources"], 0)
            self.assertEqual(status["remaining_aliases"], 0)

    def test_qan8_immediate_purchase_can_claim_at_zero_wait_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_empty_batch()
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=1,
            )
            allocator = Qan8GmailApiAllocator(
                client=_CanonicalClient(
                    "immediate@gmail.com----https://mail.example/immediate"
                ),
                store=q8_store,
                poll_interval=0,
            )

            from core import db

            with (
                patch("core.db.record_gmail_api_url_email"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", Path(temp_dir) / "empty-pool.json"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", Path(temp_dir) / "empty-pool.txt"),
            ):
                account = allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "job-immediate",
                    0,
                    wait_timeout=0,
                )

            self.assertEqual(account.code_url, "https://mail.example/immediate")
            self.assertIsNotNone(
                gmail_store.find_active_assignment_for_job("job-immediate")
            )

    def test_qan8_does_not_purchase_while_shared_provision_lease_is_held(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_batch_multi([
                {
                    "source_email": "shared@gmail.com",
                    "code_url": "https://mail.example/shared",
                    "aliases": ["shared@gmail.com"],
                }
            ])
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=1,
            )
            allocator = Qan8GmailApiAllocator(
                client=_CanonicalClient(
                    "paid@gmail.com----https://mail.example/paid"
                ),
                store=q8_store,
                poll_interval=0,
            )
            self.assertTrue(gmail_store.acquire_provision_lease("other-worker"))
            try:
                with patch.object(allocator.gmail_store, "has_available_item", return_value=False), \
                     patch.object(allocator.gmail_store, "has_pending_item", return_value=False), \
                     patch.object(
                         allocator.gmail_store,
                         "batch_status",
                         return_value={"exhausted_batch": True},
                     ), \
                     self.assertRaisesRegex(RuntimeError, "canonical purchase is busy"):
                    allocator.acquire_gmail_api_account(
                        q8_batch["batch_id"],
                        gmail_batch_id,
                        "job-blocked",
                        0,
                        wait_timeout=0,
                    )
            finally:
                gmail_store.release_provision_lease("other-worker")
            self.assertEqual(allocator.client.created, [])

    def test_qan8_lease_blocks_claim_after_pending_check_before_purchase(self):
        """A shared claim racing the final check must not cause a paid duplicate."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            shared_batch_id = gmail_store.create_batch_multi([
                {
                    "source_email": "shared-race@gmail.com",
                    "code_url": "https://mail.example/shared-race",
                    "aliases": [
                        "shared-race@gmail.com",
                        "shared-race+second@gmail.com",
                    ],
                }
            ])
            gmail_batch_id = gmail_store.create_empty_batch()
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=2,
            )
            client = _CanonicalClient(
                "purchasedrace@gmail.com----https://mail.example/purchased-race"
            )
            allocator = Qan8GmailApiAllocator(
                client=client,
                store=q8_store,
                poll_interval=0,
            )
            real_claim_any = allocator.gmail_store.claim_any_available
            racer_results = []
            pending_checks = 0

            def claim_any(job_id, *args, **kwargs):
                if str(job_id) == "buyer-race":
                    return None
                return real_claim_any(job_id, *args, **kwargs)

            def pending_item(*, exclude_batch_id=None):
                nonlocal pending_checks
                pending_checks += 1
                if pending_checks == 2:
                    racer_results.append(
                        real_claim_any(
                            "racer-during-purchase",
                            exclude_batch_id=exclude_batch_id,
                        )
                    )
                return False

            from core import db

            with (
                patch.object(allocator.gmail_store, "claim_any_available", side_effect=claim_any),
                patch.object(allocator.gmail_store, "has_available_item", return_value=False),
                patch.object(allocator.gmail_store, "has_pending_item", side_effect=pending_item),
                patch.object(allocator, "_materialize_raw_gmail_source", return_value=False),
                patch("core.db.record_gmail_api_url_email"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_JSON", Path(temp_dir) / "empty-pool.json"),
                patch.object(db, "_GMAIL_API_URL_EMAIL_TXT", Path(temp_dir) / "empty-pool.txt"),
            ):
                account = allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "buyer-race",
                    0,
                    wait_timeout=1,
                )

            self.assertEqual(account.code_url, "https://mail.example/purchased-race")
            self.assertEqual(len(client.created), 1)
            self.assertEqual(client.created[0][1], 1)
            self.assertEqual(pending_checks, 2)
            self.assertEqual(racer_results, [None])
            post_race = real_claim_any(
                "after-race",
                exclude_batch_id=gmail_batch_id,
            )
            self.assertIsNotNone(post_race)
            self.assertEqual(post_race.batch_id, shared_batch_id)

    def test_qan8_acquisition_cancels_waiter_when_purchase_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            q8_store = Qan8GmailApiStore(path)
            gmail_store = GmailApiUrlBatchStore(path)
            gmail_batch_id = gmail_store.create_empty_batch()
            q8_batch = q8_store.create_batch(
                1,
                requested_workers=1,
                aliases_per_source=1,
            )
            allocator = Qan8GmailApiAllocator(
                client=_CanonicalClient(),
                store=q8_store,
                poll_interval=0,
            )

            with (
                patch.object(allocator, "_materialize_raw_gmail_source", return_value=False),
                patch.object(
                    allocator,
                    "_purchase_source",
                    side_effect=RuntimeError("purchase failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "purchase failed"),
            ):
                allocator.acquire_gmail_api_account(
                    q8_batch["batch_id"],
                    gmail_batch_id,
                    "job-failed-purchase",
                    0,
                    wait_timeout=0,
                )

            self.assertEqual(gmail_store.list_waiting_jobs(gmail_batch_id), [])


if __name__ == "__main__":
    unittest.main()
