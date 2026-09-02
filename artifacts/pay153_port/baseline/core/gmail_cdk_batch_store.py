# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .gmail_batch_store_base import (
    GmailBatchStoreBase,
    GmailBatchError,
    GmailBatchConflict,
    Assignment,
)


# Backward compatibility aliases
GmailCdkBatchError = GmailBatchError
GmailCdkBatchConflict = GmailBatchConflict
GmailCdkAssignment = Assignment


@dataclass(frozen=True)
class GmailCdkBatchItem:
    batch_id: str
    inventory_id: str
    position: int
    state: str
    completed_count: int
    capacity: int


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gmail_cdk_batches (
    batch_id TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 12),
    routed_domains TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS gmail_cdk_batch_items (
    batch_id TEXT NOT NULL REFERENCES gmail_cdk_batches(batch_id) ON DELETE CASCADE,
    inventory_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'failed', 'exhausted')),
    completed_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    PRIMARY KEY (batch_id, inventory_id),
    UNIQUE(batch_id, position)
);
CREATE TABLE IF NOT EXISTS gmail_cdk_assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    inventory_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('active', 'completed', 'failed', 'exhausted', 'released')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id, inventory_id)
        REFERENCES gmail_cdk_batch_items(batch_id, inventory_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS gmail_cdk_active_inventory_assignment_idx
ON gmail_cdk_assignments(batch_id, inventory_id) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS gmail_cdk_active_job_assignment_idx
ON gmail_cdk_assignments(batch_id, job_id) WHERE state = 'active';
"""


class GmailCdkBatchStore(GmailBatchStoreBase):
    """Gmail CDK batch store with CDK-specific polling."""

    def _table_prefix(self) -> str:
        return "gmail_cdk"

    def _get_schema_sql(self) -> str:
        return _SCHEMA_SQL

    def _connect(self) -> sqlite3.Connection:
        """Override to add WAL check."""
        connection = super()._connect()
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            connection.close()
            raise GmailCdkBatchError("WAL is disabled for Gmail CDK batch state")
        return connection

    def poll_otp(
        self,
        assignment: Assignment,
        *,
        after_ts: Optional[float] = None,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        """Poll OTP via Gmail CDK client."""
        from . import gmail_cdk_client
        return gmail_cdk_client.poll_otp(
            assignment.inventory_id,
            after_ts=after_ts,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def create_batch(
        self,
        inventory_ids: list[str],
        *,
        capacity: int,
        routed_domains=(),
    ) -> str:
        """Create new Gmail CDK batch."""
        ids = list(dict.fromkeys(
            str(inventory_id or "").strip()
            for inventory_id in inventory_ids
            if str(inventory_id or "").strip()
        ))
        limit = int(capacity)
        if not ids:
            raise GmailCdkBatchError("Batch Gmail cần ít nhất một inventory ID")
        if not 1 <= limit <= 12:
            raise GmailCdkBatchError("Capacity Gmail CDK phải từ 1 đến 12")
        batch_id = uuid.uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO gmail_cdk_batches(batch_id, capacity, routed_domains) VALUES (?, ?, ?)",
                (batch_id, limit, json.dumps(list(routed_domains), ensure_ascii=False)),
            )
            connection.executemany(
                "INSERT INTO gmail_cdk_batch_items(batch_id, inventory_id, position) VALUES (?, ?, ?)",
                [(batch_id, inventory_id, position) for position, inventory_id in enumerate(ids)],
            )
        return batch_id

    def exhaust(self, assignment_id: str, reason: str = "") -> bool:
        """Mark assignment and item as exhausted (CDK-specific)."""
        return self._finish(assignment_id, "exhausted", item_state="exhausted", reason=reason)

    def get_assignment(self, assignment_id: str) -> Optional[GmailCdkAssignment]:
        """Get assignment by ID (CDK-specific return type)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gmail_cdk_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
        return self._assignment(row) if row else None

    def get_item(self, batch_id: str, inventory_id: str) -> Optional[GmailCdkBatchItem]:
        """Get batch item details."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT i.*, b.capacity FROM gmail_cdk_batch_items i "
                "JOIN gmail_cdk_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.inventory_id = ?",
                (str(batch_id), str(inventory_id)),
            ).fetchone()
        return self._item(row) if row else None

    @staticmethod
    def _item(row: sqlite3.Row) -> GmailCdkBatchItem:
        """Convert database row to GmailCdkBatchItem."""
        return GmailCdkBatchItem(
            row["batch_id"], row["inventory_id"], int(row["position"]), row["state"],
            int(row["completed_count"]), int(row["capacity"]),
        )
