"""Lazy QAN8 source allocation with one exclusive source lane per worker."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from core.gmail_aliases import generate_gmail_dual_domain_aliases
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8GmailApiClient,
    Qan8Order,
    Qan8OrderUnknownError,
)
from core.qan8_gmail_api_store import Qan8GmailApiStore

logger = logging.getLogger(__name__)


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
        self.poll_interval = max(0.0, float(poll_interval))
        if order_timeout is None:
            try:
                from config import email as email_config

                order_timeout = getattr(email_config, "QAN8_ORDER_TIMEOUT", 120)
            except (ImportError, AttributeError):
                order_timeout = 120
        self.order_timeout = max(1.0, float(order_timeout))

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
        timeout = self.order_timeout if wait_timeout is None else max(0.0, float(wait_timeout))
        deadline = time.monotonic() + timeout
        while True:
            if stop_check is not None:
                stop_check()
            current = self.store.get_current_source(batch_id, lane)
            if current is not None:
                assignment = self.store.claim_alias(batch_id, lane, job_id)
                if assignment is not None:
                    return Qan8GmailApiAccount(
                        email=str(assignment["alias"]),
                        code_url=str(assignment["code_url"]),
                        batch_id=str(batch_id),
                        lane_id=lane,
                        job_id=str(job_id),
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError("QAN8 lane is busy")
                time.sleep(self.poll_interval)
                continue

            account = self._purchase_source(batch, lane, deadline)
            assignment = self.store.claim_alias(batch_id, lane, job_id)
            if assignment is not None:
                return Qan8GmailApiAccount(
                    email=str(assignment["alias"]),
                    code_url=str(assignment["code_url"]),
                    batch_id=str(batch_id),
                    lane_id=lane,
                    job_id=str(job_id),
                )
            if account is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("QAN8 lane has no available alias")
                time.sleep(self.poll_interval)

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
        if normalized == "used":
            return self.store.complete_assignment(job_id)
        if normalized in {"available", "released"}:
            return self.store.release_assignment(job_id, reason=reason)
        changed = self.store.fail_assignment(job_id, reason=reason)
        if changed and normalized in {"failed", "disabled"} and "code=602" in reason:
            self.store.retire_source(assignment["source_group_id"], reason=reason)
        return changed

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
        """Disable a QAN8 lane and retire all source aliases assigned to it."""
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

    def _purchase_source(self, batch: dict, lane_id: int, deadline: float | None) -> object | None:
        with self._request_route():
            return self._purchase_source_on_route(batch, lane_id, deadline)

    def _purchase_source_on_route(self, batch: dict, lane_id: int, deadline: float | None) -> object | None:
        batch_id = str(batch["batch_id"])
        owner = uuid.uuid4().hex
        if not self.store.acquire_lane_lease(batch_id, lane_id, owner):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("QAN8 lane purchase is busy")
            time.sleep(self.poll_interval)
            return None
        try:
            current = self.store.get_current_source(batch_id, lane_id)
            if current is not None:
                return current
            orders = [row for row in self.store.list_orders(batch_id) if int(row["lane_id"]) == lane_id]
            unresolved = [
                row for row in orders
                if str(row.get("status") or "").lower() in {"pending", "unknown", "processing"}
            ]
            if unresolved:
                order_row = unresolved[-1]
            else:
                order_no = f"qan8-{batch_id[:16]}-{lane_id}-{len(orders) + 1}"
                order_row = self.store.create_order_intent(
                    batch_id, lane_id, order_no, self._sku_id()
                )
            order_no = str(order_row["out_order_no"])
            order = self._obtain_order(order_row, deadline)
            if order.status == "failed":
                self.store.update_order(batch_id, order_no, status="failed", message=order.message)
                raise RuntimeError(f"QAN8 order failed: {order.message[:160]}")
            if order.status != "completed":
                raise RuntimeError(f"QAN8 order did not complete: {order.status}")
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
            aliases = generate_gmail_dual_domain_aliases(
                source.email,
                limit=int(batch["aliases_per_source"]),
            )
            if not aliases:
                self.store.update_order(
                    batch_id, order_no, status="delivery_unparsed", message="no aliases generated"
                )
                raise RuntimeError("QAN8 source produced no Gmail aliases")
            source_group = self.store.create_source_group(
                batch_id,
                lane_id,
                source.email,
                source.code_url,
                aliases,
            )
            from core import db

            db.record_gmail_api_url_email(
                source.email,
                source.code_url,
                status="used",
                note="QAN8 purchased source",
            )
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

    def _obtain_order(self, order_row: dict, deadline: float | None) -> Qan8Order:
        batch_id = str(order_row["batch_id"])
        order_no = str(order_row["out_order_no"])
        status = str(order_row["status"] or "pending")
        if status == "pending":
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
            self.store.update_order(batch_id, order_no, status=order.status, message=order.message)
        elif status == "unknown":
            try:
                order = self.client.get_order(order_no)
            except Exception as exc:
                raise Qan8OrderUnknownError(
                    f"QAN8 order {order_no} remains unknown; lookup failed"
                ) from exc
            self.store.update_order(batch_id, order_no, status=order.status, message=order.message)
        else:
            order = Qan8Order(
                order_no=order_no,
                status=status,
                delivery=None,
                message=str(order_row["message"] or ""),
            )
        while order.status == "processing":
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
