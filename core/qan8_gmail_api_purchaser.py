"""Purchase one Gmail API source and materialize it in the canonical ledger."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_aliases import GmailAliasError, generate_gmail_dual_domain_aliases
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8GmailApiClient,
    Qan8Order,
    Qan8OrderUnknownError,
)

logger = logging.getLogger(__name__)


class Qan8GmailApiPurchaser:
    """Use QAN8 as a purchase adapter for the Gmail API URL provider."""

    def __init__(
        self,
        *,
        client: Qan8GmailApiClient | None = None,
        poll_interval: float = 2.0,
        order_timeout: float | None = None,
    ) -> None:
        self.client = client or Qan8GmailApiClient()
        self.poll_interval = max(0.0, float(poll_interval))
        if order_timeout is None:
            try:
                from config import email as email_config

                order_timeout = getattr(email_config, "QAN8_ORDER_TIMEOUT", 120)
            except (ImportError, AttributeError):
                order_timeout = 120
        self.order_timeout = max(1.0, float(order_timeout))

    def purchase_source(
        self,
        batch_id: str,
        *,
        aliases_per_source: int = 12,
        store: GmailApiUrlBatchStore | None = None,
        stop_check: Callable[[], None] | None = None,
    ) -> bool:
        """Purchase one source and append its aliases to ``batch_id``.

        The caller owns the canonical provision lease.  This method therefore
        never creates a QAN8 batch, lane, source, or alias assignment.
        """
        batch = str(batch_id or "").strip()
        if not batch:
            raise ValueError("Gmail API URL batch ID is required")
        limit = max(1, min(12, int(aliases_per_source or 12)))
        target_store = store or GmailApiUrlBatchStore(APP_STATE_DB_PATH)
        if stop_check is not None:
            stop_check()

        orders = target_store.list_purchase_orders(batch)
        unresolved = [
            row
            for row in orders
            if str(row.get("status") or "").strip().lower()
            in {
                "pending",
                "unknown",
                "processing",
                "materializing",
                "materialization_failed",
            }
        ]
        if unresolved:
            order_row = unresolved[-1]
        else:
            order_no = f"gapi-{batch[:16]}-{len(orders) + 1}"
            order_row = target_store.create_purchase_order(
                batch,
                order_no,
                str(getattr(self.client, "sku_id", "") or ""),
            )

        with self._request_route():
            order = self._obtain_order(
                target_store,
                order_row,
                stop_check=stop_check,
            )
            if order.status == "failed":
                raise RuntimeError(
                    f"QAN8 order failed: {str(order.message or '')[:160]}"
                )
            if order.status != "completed":
                raise RuntimeError(f"QAN8 order did not complete: {order.status}")
            if stop_check is not None:
                stop_check()

            order_no = str(order_row["out_order_no"])
            target_store.update_purchase_order(
                batch,
                order_no,
                status="materializing",
                message=order.message,
            )
            try:
                records = self.client.parse_delivery(order.delivery)
            except Qan8DeliveryError as exc:
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="delivery_unparsed",
                    message=str(exc),
                )
                raise RuntimeError(f"QAN8 delivery rejected: {exc}") from exc
            if len(records) != 1:
                message = "quantity=1 delivery must contain exactly one Gmail source"
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="delivery_unparsed",
                    message=message,
                )
                raise RuntimeError(f"QAN8 delivery rejected: {message}")

            source = records[0]
            from core import db

            if db.is_gmail_api_url_code_url_failed(
                source.code_url,
                sqlite_path=target_store.path,
            ):
                message = "Gmail API URL source is quarantined after provider error code=602"
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="source_failed",
                    message=message,
                )
                raise RuntimeError(message)

            try:
                aliases = generate_gmail_dual_domain_aliases(
                    source.email,
                    limit=limit,
                )
            except GmailAliasError as exc:
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="source_invalid",
                    message=str(exc),
                )
                raise RuntimeError(f"QAN8 source email is invalid: {exc}") from exc

            unavailable = target_store.list_globally_unavailable_aliases()
            unavailable.update(
                target_store.list_unavailable_aliases_for_code_url(source.code_url)
            )
            aliases = [
                alias
                for alias in aliases
                if alias.casefold() not in unavailable
                and not target_store.has_alias_for_other_code_url(
                    alias,
                    source.code_url,
                )
            ]
            if not aliases:
                message = "all Gmail aliases are already consumed, failed, or reserved"
                db.record_gmail_api_url_email(
                    source.email,
                    source.code_url,
                    status="exhausted",
                    note="Purchased source has no canonical alias capacity",
                    sqlite_path=target_store.path,
                )
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="source_exhausted",
                    message=message,
                )
                raise RuntimeError(message)

            try:
                db.record_gmail_api_url_email(
                    source.email,
                    source.code_url,
                    status="used",
                    note="QAN8 purchased Gmail API source",
                    sqlite_path=target_store.path,
                )
                target_store.append_source_group(
                    batch,
                    source.email,
                    source.code_url,
                    aliases,
                    exclusive_code_url=True,
                )
            except Exception as exc:
                try:
                    db.fail_gmail_api_url_sources_for_code_url(
                        source.code_url,
                        note=f"Canonical source materialization failed: {exc}",
                        sqlite_path=target_store.path,
                    )
                except Exception:
                    logger.exception(
                        "Could not retire source after canonical materialization failure: %s",
                        source.code_url,
                    )
                target_store.update_purchase_order(
                    batch,
                    order_no,
                    status="materialization_failed",
                    message=str(exc),
                )
                raise RuntimeError(
                    f"QAN8 source materialization failed: {exc}"
                ) from exc

            target_store.update_purchase_order(
                batch,
                order_no,
                status="completed",
                message=order.message,
                delivery_summary="one Gmail source",
                source_email=source.email,
                code_url=source.code_url,
            )
            logger.info(
                "Materialized one QAN8 Gmail API source into batch %s with %d aliases",
                batch,
                len(aliases),
            )
            return True

    def _obtain_order(
        self,
        store: GmailApiUrlBatchStore,
        order_row: dict[str, object],
        *,
        stop_check: Callable[[], None] | None = None,
    ) -> Qan8Order:
        batch = str(order_row["batch_id"])
        order_no = str(order_row["out_order_no"])
        status = str(order_row.get("status") or "pending").strip().lower()
        if status == "pending":
            if stop_check is not None:
                stop_check()
            try:
                order = self.client.create_order(order_no, quantity=1)
            except Qan8OrderUnknownError:
                store.update_purchase_order(
                    batch,
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
                store.update_purchase_order(
                    batch,
                    order_no,
                    status="failed",
                    message=str(exc),
                )
                raise
            store.update_purchase_order(
                batch,
                order_no,
                status=order.status,
                message=order.message,
            )
        else:
            try:
                order = self.client.get_order(order_no)
            except Exception as exc:
                raise Qan8OrderUnknownError(
                    f"QAN8 order {order_no} lookup failed"
                ) from exc
            store.update_purchase_order(
                batch,
                order_no,
                status=order.status,
                message=order.message,
            )

        deadline = time.monotonic() + self.order_timeout
        while str(order.status or "").strip().lower() == "processing":
            if stop_check is not None:
                stop_check()
            if time.monotonic() >= deadline:
                raise RuntimeError("QAN8 order polling timed out")
            time.sleep(self.poll_interval)
            order = self.client.get_order(order_no)
            store.update_purchase_order(
                batch,
                order_no,
                status=order.status,
                message=order.message,
            )
        return order

    @contextmanager
    def _request_route(self):
        """Hold one configured route for the full purchase/poll cycle."""
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
