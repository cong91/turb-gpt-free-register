"""Lazy QAN8 source allocation with one exclusive source lane per worker."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from core.gmail_aliases import generate_gmail_dual_domain_aliases
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.gmail_api_url_client import _is_provider_code_602
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8GmailApiClient,
    Qan8Order,
    Qan8OrderUnknownError,
)
from core.qan8_gmail_api_store import Qan8GmailApiStore

logger = logging.getLogger(__name__)
_MAX_UNUSABLE_SOURCE_ATTEMPTS = 3
_DEFAULT_PROVISION_LEASE_SECONDS = 300
_PROVISION_LEASE_GRACE_SECONDS = 60


@dataclass(frozen=True)
class Qan8GmailApiAccount:
    email: str
    code_url: str
    batch_id: str
    lane_id: int
    job_id: str


class Qan8GmailApiAllocator:
    """Coordinate QAN8 orders and aliases without a shared source queue."""

    def __init__(
        self,
        *,
        client: Qan8GmailApiClient | None = None,
        store: Qan8GmailApiStore | None = None,
        poll_interval: float = 2.0,
        order_timeout: float | None = None,
    ) -> None:
        self.client = client or Qan8GmailApiClient()
        self.store = store or Qan8GmailApiStore()
        self.gmail_store = GmailApiUrlBatchStore(self.store.path)
        self.poll_interval = max(0.0, float(poll_interval))
        if order_timeout is None:
            try:
                from config import email as email_config

                order_timeout = getattr(email_config, "QAN8_ORDER_TIMEOUT", 120)
            except (ImportError, AttributeError):
                order_timeout = 120
        self.order_timeout = max(1.0, float(order_timeout))

    def _provision_lease_seconds(self) -> int:
        """Keep the shared purchase lock through the provider polling window."""
        return max(
            _DEFAULT_PROVISION_LEASE_SECONDS,
            int(self.order_timeout) + _PROVISION_LEASE_GRACE_SECONDS,
        )

    def create_batch(
        self,
        target_count: int,
        *,
        requested_workers: int,
        aliases_per_source: int,
    ) -> dict:
        return self.store.create_batch(
            target_count,
            requested_workers=requested_workers,
            aliases_per_source=aliases_per_source,
        )

    @staticmethod
    def lane_for_position(position: int, effective_workers: int) -> int:
        workers = int(effective_workers)
        if workers < 1:
            raise ValueError("effective_workers must be positive")
        value = int(position)
        if value < 0:
            raise ValueError("position must not be negative")
        return value % workers

    def acquire_account(
        self,
        batch_id: str,
        job_id: int | str,
        lane_id: int,
        *,
        wait_timeout: float | None = None,
        stop_check: Callable[[], None] | None = None,
    ) -> Qan8GmailApiAccount:
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise ValueError("QAN8 batch does not exist")
        lane = int(lane_id)
        lane_record = self.store.get_lane(batch_id, lane)
        if lane_record is None:
            raise ValueError("QAN8 lane does not exist")
        if str(lane_record.get("state") or "active") != "active":
            raise RuntimeError("QAN8 lane is quarantined")
        # Order timeout bounds provider polling; lane contention waits unless
        # the caller explicitly supplies a wait timeout.
        deadline = (
            None
            if wait_timeout is None
            else time.monotonic() + max(0.0, float(wait_timeout))
        )
        unusable_source_attempts = 0
        while True:
            if stop_check is not None:
                stop_check()
            current = self.store.get_current_source(batch_id, lane)
            if current is not None:
                from core import db

                if db.is_gmail_api_url_code_url_failed(
                    str(current["code_url"]),
                    sqlite_path=self.store.path,
                ):
                    self.store.quarantine_lane(
                        batch_id,
                        lane,
                        reason="Gmail API URL source is quarantined after provider error code=602",
                    )
                    continue
                assignment = self.store.claim_alias(batch_id, lane, job_id)
                if assignment is not None:
                    return Qan8GmailApiAccount(
                        email=str(assignment["alias"]),
                        code_url=str(assignment["code_url"]),
                        batch_id=str(batch_id),
                        lane_id=lane,
                        job_id=str(job_id),
                    )
                if self.store.get_current_source(batch_id, lane) is None:
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    raise RuntimeError("QAN8 lane is busy")
                time.sleep(self.poll_interval)
                continue
            order_count_before = len(self.store.list_orders(batch_id))
            account = self._purchase_source(
                batch,
                lane,
                deadline,
                stop_check=stop_check,
            )
            if stop_check is not None:
                # A paid order can finish while the caller is being stopped.
                # Do not attach the newly materialized alias to a job that no
                # longer owns work; the next caller can reuse this source.
                stop_check()
            assignment = self.store.claim_alias(batch_id, lane, job_id)
            if assignment is not None:
                return Qan8GmailApiAccount(
                    email=str(assignment["alias"]),
                    code_url=str(assignment["code_url"]),
                    batch_id=str(batch_id),
                    lane_id=lane,
                    job_id=str(job_id),
                )
            if account is not None:
                source_group_id = str(account.get("source_group_id") or "") if isinstance(account, dict) else ""
                if source_group_id:
                    self.store.retire_source(
                        source_group_id,
                        reason="Gmail alias became unavailable before QAN8 claim",
                    )
                unusable_source_attempts += 1
                if unusable_source_attempts >= _MAX_UNUSABLE_SOURCE_ATTEMPTS:
                    raise RuntimeError(
                        "QAN8 không còn nguồn Gmail alias khả dụng sau "
                        f"{unusable_source_attempts} lần thử"
                    )
            if account is None:
                order_count_after = len(self.store.list_orders(batch_id))
                if order_count_after > order_count_before:
                    unusable_source_attempts += 1
                if unusable_source_attempts >= _MAX_UNUSABLE_SOURCE_ATTEMPTS:
                    raise RuntimeError(
                        "QAN8 không còn nguồn Gmail alias khả dụng sau "
                        f"{unusable_source_attempts} lần thử"
                    )
                if deadline is not None and time.monotonic() >= deadline:
                    raise RuntimeError("QAN8 lane has no available alias")
                time.sleep(self.poll_interval)

    def acquire_gmail_api_account(
        self,
        batch_id: str,
        gmail_batch_id: str,
        job_id: int | str,
        lane_id: int,
        *,
        wait_timeout: float | None = None,
        stop_check: Callable[[], None] | None = None,
    ):
        """Claim a QAN8-backed alias from the canonical Gmail API ledger.

        QAN8 only coordinates lazy purchases here.  The returned assignment is
        always a ``gmail_api_url_assignments`` row; no runtime QAN8 assignment
        is created, so OTP, release, retry, and completion all observe one
        ownership ledger.
        """
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise ValueError("QAN8 batch does not exist")
        lane = int(lane_id)
        lane_record = self.store.get_lane(batch_id, lane)
        if lane_record is None:
            raise ValueError("QAN8 lane does not exist")
        if str(lane_record.get("state") or "active") != "active":
            raise RuntimeError("QAN8 lane is quarantined")
        canonical_batch = str(gmail_batch_id or "").strip()
        if not canonical_batch:
            raise ValueError("QAN8 Gmail API requires a canonical Gmail batch_id")
        owner = str(job_id or "").strip()
        if not owner:
            raise ValueError("QAN8 Gmail API requires a job_id")

        deadline = (
            None
            if wait_timeout is None
            else time.monotonic() + max(0.0, float(wait_timeout))
        )
        purchase_owner = uuid.uuid4().hex
        waiter_registered = False
        unusable_source_attempts = 0
        try:
            while True:
                if stop_check is not None:
                    stop_check()

                assignment = self.gmail_store.claim_waiting(canonical_batch, owner)
                waiter_registered = True
                if assignment is None:
                    # Existing Gmail/QAN8 source batches are part of the same
                    # canonical ledger.  Reuse one before paying for another
                    # source, while keeping this job's target batch for new buys.
                    assignment = self.gmail_store.claim_any_available(
                        owner,
                        exclude_batch_id=canonical_batch,
                    )
                if assignment is not None:
                    if assignment.batch_id != canonical_batch:
                        self.gmail_store.cancel_waiter(
                            canonical_batch,
                            owner,
                            "claimed from shared Gmail API ledger",
                        )
                    return self._gmail_account_from_assignment(assignment)

                if self.gmail_store.has_available_item(exclude_batch_id=canonical_batch):
                    # A different batch became available between the two claims;
                    # retry immediately instead of purchasing a duplicate source.
                    continue

                status = self.gmail_store.batch_status(canonical_batch)
                if not status["exhausted_batch"]:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError("Gmail API URL batch đang bận")
                    time.sleep(self.poll_interval)
                    continue

                # A canonical alias can be temporarily unavailable only because
                # another worker owns its mailbox URL.  Wait for that assignment
                # instead of buying a new source while pending inventory exists.
                if self.gmail_store.has_pending_item(exclude_batch_id=canonical_batch):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError("Gmail API URL batch đang bận")
                    time.sleep(self.poll_interval)
                    continue

                # Serialize both raw-pool materialization and paid replenishment
                # for this QAN8 batch.  Lane 0 is the durable provision lock.
                if not self.store.acquire_lane_lease(
                    batch_id,
                    0,
                    purchase_owner,
                    lease_kind="canonical_purchase",
                ):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError("QAN8 canonical purchase is busy")
                    time.sleep(self.poll_interval)
                    continue
                if not self.gmail_store.acquire_provision_lease(
                    purchase_owner,
                    lease_seconds=self._provision_lease_seconds(),
                ):
                    self.store.release_lane_lease(
                        batch_id,
                        0,
                        purchase_owner,
                        lease_kind="canonical_purchase",
                    )
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError("QAN8 canonical purchase is busy")
                    time.sleep(self.poll_interval)
                    continue
                try:
                    if stop_check is not None:
                        stop_check()
                    # Re-check after taking the durable lock.  Another worker may
                    # have appended aliases while this caller was waiting.
                    assignment = self.gmail_store.claim_waiting(
                        canonical_batch,
                        owner,
                        provision_owner=purchase_owner,
                    )
                    if assignment is not None:
                        return self._gmail_account_from_assignment(assignment)
                    assignment = self.gmail_store.claim_any_available(
                        owner,
                        exclude_batch_id=canonical_batch,
                        provision_owner=purchase_owner,
                    )
                    if assignment is not None:
                        self.gmail_store.cancel_waiter(
                            canonical_batch,
                            owner,
                            "claimed from shared Gmail API ledger",
                        )
                        return self._gmail_account_from_assignment(assignment)
                    if self.gmail_store.has_available_item(exclude_batch_id=canonical_batch):
                        continue
                    if self.gmail_store.has_pending_item(exclude_batch_id=canonical_batch):
                        continue
                    if self._materialize_raw_gmail_source(
                        canonical_batch,
                        aliases_per_source=int(batch["aliases_per_source"]),
                    ):
                        continue
                    orders_before = self.store.list_orders(batch_id)
                    purchased = self._purchase_source(
                        batch,
                        lane,
                        deadline,
                        gmail_batch_id=canonical_batch,
                        stop_check=stop_check,
                    )
                    if purchased is not None:
                        unusable_source_attempts = 0
                    else:
                        # ``None`` is normally a race (another worker just
                        # materialized a shared alias), but terminal order
                        # outcomes also use it.  Count only durable order
                        # progress so a blocked/exhausted delivery cannot
                        # trigger unbounded paid orders.
                        orders_after = self.store.list_orders(batch_id)
                        if orders_after != orders_before:
                            unusable_source_attempts += 1
                            if unusable_source_attempts >= _MAX_UNUSABLE_SOURCE_ATTEMPTS:
                                raise RuntimeError(
                                    "QAN8 không thể bổ sung Gmail API source sau "
                                    f"{unusable_source_attempts} lần thử không có alias khả dụng"
                                )
                finally:
                    self.gmail_store.release_provision_lease(purchase_owner)
                    self.store.release_lane_lease(
                        batch_id,
                        0,
                        purchase_owner,
                        lease_kind="canonical_purchase",
                    )

                # Re-enter the normal claim path after materialization/purchase.
                # The deadline is checked only when no alias can be claimed, so
                # a source that finishes exactly at the timeout remains usable.
        finally:
            if waiter_registered:
                try:
                    self.gmail_store.cancel_waiter(
                        canonical_batch,
                        owner,
                        "QAN8 canonical acquisition ended",
                    )
                except Exception:
                    logger.warning(
                        "QAN8 canonical waiter cleanup failed: batch=%s job=%s",
                        canonical_batch,
                        owner,
                        exc_info=True,
                    )

    def _materialize_raw_gmail_source(
        self,
        canonical_batch: str,
        *,
        aliases_per_source: int,
    ) -> bool:
        """Bridge one raw Gmail pool source into the canonical ledger."""
        from core.gmail_api_url_client import materialize_next_available_source

        return bool(
            materialize_next_available_source(
                canonical_batch,
                aliases_per_source=aliases_per_source,
                store=self.gmail_store,
            )
        )

    @staticmethod
    def _gmail_account_from_assignment(assignment):
        from core.gmail_api_url_client import GmailApiUrlAccount

        try:
            alias, code_url = str(assignment.inventory_id).split("----", 1)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid canonical Gmail inventory_id: {assignment.inventory_id}"
            ) from exc
        return GmailApiUrlAccount(email=alias, code_url=code_url)

    def complete_account(self, batch_id: str, job_id: int | str) -> bool:
        return self.store.complete_assignment(job_id)

    def release_account_if_unconsumed(
        self, batch_id: str, job_id: int | str, reason: str = ""
    ) -> bool:
        return self.store.release_assignment(job_id, reason=reason)

    def fail_account(self, batch_id: str, job_id: int | str, reason: str = "") -> bool:
        return self.store.fail_assignment(job_id, reason=reason)

    def release_account(self, alias: str, *, status: str = "available", reason: str = "") -> bool:
        assignment = self.store.get_active_assignment_for_alias(alias)
        if assignment is None:
            return False
        job_id = assignment["job_id"]
        normalized = str(status or "available").strip().lower()
        if _is_provider_code_602(reason):
            from core import db
            from core.gmail_api_url_batch_store import GmailApiUrlBatchStore

            code_url = str(assignment.get("code_url") or "").strip()
            quarantined = GmailApiUrlBatchStore(self.store.path).quarantine_code_url(
                code_url,
                reason=reason,
            ) if code_url else 0
            db.fail_gmail_api_url_sources_for_code_url(
                code_url,
                note=reason,
                sqlite_path=self.store.path,
            )
            return bool(quarantined)
        if normalized == "used":
            return self.store.complete_assignment(job_id)
        if normalized in {"available", "released"}:
            return self.store.release_assignment(job_id, reason=reason)
        return self.store.fail_assignment(job_id, reason=reason)

    def get_account_context(self, alias: str) -> Qan8GmailApiAccount | None:
        context = self.store.get_account_context(alias)
        if context is None:
            return None
        return Qan8GmailApiAccount(
            email=str(context["alias"]),
            code_url=str(context["code_url"]),
            batch_id=str(context["batch_id"]),
            lane_id=int(context["lane_id"]),
            job_id="",
        )

    def quarantine_lane(self, batch_id: str, lane_id: int, *, reason: str = "") -> int:
        """Retire the failed source so the lane can purchase a replacement."""
        return self.store.quarantine_lane(batch_id, lane_id, reason=reason)

    def status(self, batch_id: str) -> dict:
        return self.store.batch_status(batch_id)

    @contextmanager
    def _request_route(self):
        """Hold one QAN8 route for the full purchase and order-poll cycle."""
        if str(getattr(self.client, "proxy_url", "") or "").strip():
            yield
            return

        from core.nordvpn_wireguard import proxy_for_qan8_api

        previous_proxy = getattr(self.client, "proxy_url", "")
        with proxy_for_qan8_api(owner_id=f"qan8-api:{uuid.uuid4().hex}") as proxy_url:
            self.client.proxy_url = str(proxy_url or "")
            try:
                yield
            finally:
                self.client.proxy_url = previous_proxy

    def _purchase_source(
        self,
        batch: dict,
        lane_id: int,
        deadline: float | None,
        *,
        gmail_batch_id: str | None = None,
        stop_check: Callable[[], None] | None = None,
    ) -> object | None:
        if stop_check is not None:
            stop_check()
        with self._request_route():
            return self._purchase_source_on_route(
                batch,
                lane_id,
                deadline,
                gmail_batch_id=gmail_batch_id,
                stop_check=stop_check,
            )

    def _purchase_source_on_route(
        self,
        batch: dict,
        lane_id: int,
        deadline: float | None,
        *,
        gmail_batch_id: str | None = None,
        stop_check: Callable[[], None] | None = None,
    ) -> object | None:
        batch_id = str(batch["batch_id"])
        owner = uuid.uuid4().hex
        if not self.store.acquire_lane_lease(batch_id, lane_id, owner):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("QAN8 lane purchase is busy")
            time.sleep(self.poll_interval)
            return None
        try:
            if stop_check is not None:
                stop_check()
            current = (
                self.store.get_current_source(batch_id, lane_id)
                if not gmail_batch_id
                else None
            )
            if current is not None:
                return current
            if not gmail_batch_id:
                reused = self._link_existing_canonical_source(batch_id, lane_id)
                if reused is not None:
                    return reused
            if gmail_batch_id and self.gmail_store.has_available_item(
                exclude_batch_id=gmail_batch_id
            ):
                return None
            orders = [row for row in self.store.list_orders(batch_id) if int(row["lane_id"]) == lane_id]
            unresolved = [
                row for row in orders
                if str(row.get("status") or "").lower()
                in {"pending", "unknown", "processing", "materializing"}
            ]
            if unresolved:
                order_row = unresolved[-1]
            else:
                order_no = f"qan8-{batch_id[:16]}-{lane_id}-{len(orders) + 1}"
                order_row = self.store.create_order_intent(
                    batch_id, lane_id, order_no, self._sku_id()
                )
            order_no = str(order_row["out_order_no"])
            order = self._obtain_order(
                order_row,
                deadline,
                stop_check=stop_check,
            )
            if order.status == "failed":
                self.store.update_order(batch_id, order_no, status="failed", message=order.message)
                raise RuntimeError(f"QAN8 order failed: {order.message[:160]}")
            if order.status != "completed":
                raise RuntimeError(f"QAN8 order did not complete: {order.status}")
            self.store.update_order(
                batch_id,
                order_no,
                status="materializing",
                message=order.message,
            )
            try:
                records = self.client.parse_delivery(order.delivery)
            except Qan8DeliveryError as exc:
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="delivery_unparsed",
                    message=str(exc),
                )
                raise RuntimeError(f"QAN8 delivery rejected: {exc}") from exc
            if len(records) != 1:
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="delivery_unparsed",
                    message="quantity=1 delivery must contain exactly one Gmail source",
                )
                raise RuntimeError("QAN8 delivery must contain exactly one Gmail source")
            source = records[0]
            from core import db

            if db.is_gmail_api_url_code_url_failed(
                source.code_url,
                sqlite_path=self.store.path,
            ):
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="source_failed",
                    message="Gmail API URL source is quarantined after provider error code=602",
                )
                return None
            if db.is_gmail_api_url_source_blocked(
                source.email,
                sqlite_path=self.store.path,
            ):
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="source_blocked",
                    message="Gmail API URL source is disabled or terminally retired",
                )
                return None
            if self.gmail_store.has_active_code_url_assignment(source.code_url):
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="source_busy",
                    message="Gmail API URL is already assigned by another job",
                )
                return None
            aliases = generate_gmail_dual_domain_aliases(
                source.email,
                limit=int(batch["aliases_per_source"]),
            )
            unavailable_aliases = (
                self.gmail_store.list_globally_unavailable_aliases()
                | self.gmail_store.list_unavailable_aliases_for_code_url(
                    source.code_url
                )
                | self.gmail_store.list_allocated_aliases_for_code_url(
                    source.code_url
                )
            )
            aliases = [
                alias
                for alias in aliases
                if alias.strip().casefold() not in unavailable_aliases
                and not self.gmail_store.has_alias_for_other_code_url(alias, source.code_url)
            ]
            if not aliases:
                db.record_gmail_api_url_email(
                    source.email,
                    source.code_url,
                    status="exhausted",
                    note="QAN8 source has no globally available Gmail aliases",
                    sqlite_path=self.store.path,
                )
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="source_exhausted",
                    message="all Gmail aliases are already consumed, failed, or reserved",
                )
                return None
            try:
                # Persist the purchased source before materializing canonical
                # items.  If this JSON/TXT compatibility export fails, no
                # canonical aliases are inserted and the order can be retried
                # without leaving paid inventory orphaned from its source row.
                db.record_gmail_api_url_email(
                    source.email,
                    source.code_url,
                    status="used",
                    note="QAN8 purchased source",
                    sqlite_path=self.store.path,
                )
                if gmail_batch_id:
                    source_group = self.store.create_source_group(
                        batch_id,
                        lane_id,
                        source.email,
                        source.code_url,
                        aliases,
                        gmail_batch_id=gmail_batch_id,
                    )
                else:
                    gmail_alias_refs = self.gmail_store.ensure_alias_items(
                        source.code_url,
                        aliases,
                    )
                    source_group = self.store.create_source_group(
                        batch_id,
                        lane_id,
                        source.email,
                        source.code_url,
                        aliases,
                        gmail_alias_refs=gmail_alias_refs,
                    )
            except Exception as exc:
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="materialization_failed",
                    message=str(exc),
                )
                db.fail_gmail_api_url_sources_for_code_url(
                    source.code_url,
                    note=f"QAN8 source materialization failed: {exc}",
                    sqlite_path=self.store.path,
                )
                raise RuntimeError(
                    f"QAN8 source materialization failed: {exc}"
                ) from exc
            self.store.update_order(
                batch_id,
                order_no,
                status="completed",
                message=order.message,
                delivery_summary="one Gmail source",
                source_group_id=source_group["source_group_id"],
            )
            return source_group
        finally:
            self.store.release_lane_lease(batch_id, lane_id, owner)

    def _link_existing_canonical_source(
        self,
        batch_id: str,
        lane_id: int,
    ) -> dict | None:
        """Link one unused canonical source to the legacy QAN8 lane.

        ``acquire_account`` is retained for older callers, but it must follow
        the same shared Gmail ledger as the canonical acquisition path.  A
        source already present in another Gmail batch is therefore linked into
        QAN8 provenance before any paid order is created.
        """
        with self.gmail_store._runtime_transaction() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('gmail_api_url_batches', 'gmail_api_url_batch_items', "
                    "'gmail_api_url_assignments', 'qan8_aliases')"
                ).fetchall()
            }
            if tables != {
                "gmail_api_url_batches",
                "gmail_api_url_batch_items",
                "gmail_api_url_assignments",
                "qan8_aliases",
            }:
                return None
            rows = connection.execute(
                "SELECT i.*, b.capacity FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.state = 'active' AND i.completed_count < b.capacity "
                "AND NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active') "
                "AND NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a2 "
                "JOIN gmail_api_url_batch_items i2 ON a2.batch_id = i2.batch_id "
                "AND a2.inventory_id = i2.inventory_id "
                "WHERE i2.code_url = i.code_url AND a2.state = 'active') "
                "AND NOT EXISTS (SELECT 1 FROM qan8_aliases x "
                "WHERE lower(x.alias) = lower(i.email)) "
                "ORDER BY b.created_at, i.position, i.created_at"
            ).fetchall()
            if not rows:
                return None
            blocked_roots = self.gmail_store.runtime_blocked_canonical_roots()
            unavailable = self.gmail_store._globally_unavailable_aliases_in_connection(
                connection
            )
            shadows = self.gmail_store._shadow_aliases_in_connection(connection)
            first = self.gmail_store._first_runtime_eligible_row(
                rows,
                blocked_roots,
                unavailable,
                shadows,
                connection,
            )
            if first is None:
                return None
            source_batch = str(first["batch_id"])
            code_url = str(first["code_url"] or "").strip()
            source_rows = [
                row for row in rows
                if str(row["batch_id"]) == source_batch
                and str(row["code_url"] or "").strip() == code_url
                and not self.gmail_store._runtime_alias_is_unavailable(
                    row["email"], unavailable
                )
                and (str(row["batch_id"]), str(row["inventory_id"]))
                not in shadows
                and not self.gmail_store._alias_is_runtime_blocked(
                    str(row["email"] or ""), blocked_roots
                )
            ]
            aliases: list[str] = []
            refs: dict[str, tuple[str, str]] = {}
            for row in source_rows:
                alias = str(row["email"] or "").strip().casefold()
                if not alias or alias in refs:
                    # Legacy capacity batches stored the same source email in
                    # one ``::index`` row per slot.  QAN8 aliases are globally
                    # unique, so retain the first canonical reference instead
                    # of treating duplicate slots as duplicate aliases.
                    continue
                aliases.append(alias)
                refs[alias] = (source_batch, str(row["inventory_id"]))
        if not aliases:
            return None
        try:
            return self.store.create_source_group(
                batch_id,
                lane_id,
                aliases[0],
                code_url,
                aliases,
                gmail_alias_refs=refs,
            )
        except (RuntimeError, ValueError):
            # Another worker may have linked the same canonical rows after the
            # read transaction. The caller will retry before purchasing.
            logger.info(
                "Canonical Gmail source became unavailable while linking: batch=%s lane=%s",
                batch_id,
                lane_id,
            )
            return None

    def _obtain_order(
        self,
        order_row: dict,
        deadline: float | None,
        *,
        stop_check: Callable[[], None] | None = None,
    ) -> Qan8Order:
        batch_id = str(order_row["batch_id"])
        order_no = str(order_row["out_order_no"])
        status = str(order_row["status"] or "pending")
        if status == "pending":
            # Check outside the provider-error handler so cancellation does
            # not turn an unpaid order intent into a terminal provider failure.
            if stop_check is not None:
                stop_check()
            try:
                order = self.client.create_order(order_no, quantity=1)
            except Qan8OrderUnknownError:
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="unknown",
                    message="order request outcome unknown; lookup required",
                )
                try:
                    order = self.client.get_order(order_no)
                except Exception as exc:
                    raise Qan8OrderUnknownError(
                        f"QAN8 order {order_no} remains unknown; lookup failed"
                    ) from exc
            except Exception as exc:
                self.store.update_order(
                    batch_id,
                    order_no,
                    status="failed",
                    message=str(exc)[:300],
                )
                raise
            self.store.update_order(batch_id, order_no, status=order.status, message=order.message)
        elif status in {"unknown", "materializing"}:
            try:
                order = self.client.get_order(order_no)
            except Exception as exc:
                raise Qan8OrderUnknownError(
                    f"QAN8 order {order_no} remains unknown; lookup failed"
                ) from exc
            # Keep a completed provider order in materializing state until the
            # canonical Gmail items and QAN8 provenance are committed.
            persisted_status = "materializing" if status == "materializing" else order.status
            self.store.update_order(
                batch_id,
                order_no,
                status=persisted_status,
                message=order.message,
            )
        else:
            order = Qan8Order(
                order_no=order_no,
                status=status,
                delivery=None,
                message=str(order_row["message"] or ""),
            )
        while order.status == "processing":
            if stop_check is not None:
                stop_check()
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("QAN8 order polling timed out")
            if time.time() - float(order_row["updated_at"] or 0) > self.order_timeout:
                raise RuntimeError("QAN8 order polling timed out")
            time.sleep(self.poll_interval)
            order = self.client.get_order(order_no)
            self.store.update_order(batch_id, order_no, status=order.status, message=order.message)
        return order

    def _sku_id(self) -> str:
        return str(getattr(self.client, "sku_id", "") or "")
