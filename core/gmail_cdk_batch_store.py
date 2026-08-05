# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class GmailCdkBatchError(RuntimeError):
    """Batch Gmail CDK không thể claim hoặc chuyển trạng thái."""


class GmailCdkBatchConflict(GmailCdkBatchError):
    """Không còn Gmail CDK khả dụng cho job hiện tại."""


@dataclass(frozen=True)
class GmailCdkBatchItem:
    batch_id: str
    inventory_id: str
    position: int
    state: str
    completed_count: int
    capacity: int


@dataclass(frozen=True)
class GmailCdkAssignment:
    assignment_id: str
    batch_id: str
    inventory_id: str
    job_id: str
    state: str


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


class GmailCdkBatchStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 3000):
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            connection.close()
            raise GmailCdkBatchError("WAL is disabled for Gmail CDK batch state")
        connection.executescript(_SCHEMA_SQL)
        return connection

    def create_batch(
        self,
        inventory_ids: list[str],
        *,
        capacity: int,
        routed_domains=(),
    ) -> str:
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

    def claim(self, batch_id: str, job_id: str) -> GmailCdkAssignment:
        batch, owner = self._required(batch_id, job_id)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM gmail_cdk_assignments WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (batch, owner),
            ).fetchone()
            if existing:
                return self._assignment(existing)
            row = connection.execute(
                "SELECT i.*, b.capacity FROM gmail_cdk_batch_items i "
                "JOIN gmail_cdk_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity "
                "AND NOT EXISTS (SELECT 1 FROM gmail_cdk_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id AND a.state = 'active') "
                "ORDER BY i.position LIMIT 1",
                (batch,),
            ).fetchone()
            if row is None:
                raise GmailCdkBatchConflict("Không còn Gmail CDK rảnh trong batch")
            assignment_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO gmail_cdk_assignments "
                "(assignment_id, batch_id, inventory_id, job_id, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (assignment_id, batch, row["inventory_id"], owner),
            )
            created = connection.execute(
                "SELECT * FROM gmail_cdk_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return self._assignment(created)

    def complete(self, assignment_id: str) -> bool:
        return self._finish(assignment_id, "completed", item_state=None)

    def fail(self, assignment_id: str, reason: str = "") -> bool:
        return self._finish(assignment_id, "failed", item_state="failed", reason=reason)

    def exhaust(self, assignment_id: str, reason: str = "") -> bool:
        return self._finish(assignment_id, "exhausted", item_state="exhausted", reason=reason)

    def release(self, assignment_id: str, reason: str = "") -> bool:
        return self._finish(assignment_id, "released", item_state=None, reason=reason)

    def _finish(
        self,
        assignment_id: str,
        target: str,
        *,
        item_state: str | None,
        reason: str = "",
    ) -> bool:
        value = str(assignment_id or "").strip()
        if not value:
            raise GmailCdkBatchError("Assignment ID không được để trống")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM gmail_cdk_assignments WHERE assignment_id = ?",
                (value,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] == target:
                return True
            if row["state"] != "active":
                return False
            connection.execute(
                "UPDATE gmail_cdk_assignments SET state = ?, reason = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE assignment_id = ?",
                (target, str(reason or "")[:300], value),
            )
            if target == "completed":
                connection.execute(
                    "UPDATE gmail_cdk_batch_items SET completed_count = completed_count + 1 "
                    "WHERE batch_id = ? AND inventory_id = ?",
                    (row["batch_id"], row["inventory_id"]),
                )
                connection.execute(
                    "UPDATE gmail_cdk_batch_items SET state = 'exhausted' "
                    "WHERE batch_id = ? AND inventory_id = ? "
                    "AND completed_count >= (SELECT capacity FROM gmail_cdk_batches WHERE batch_id = ?)",
                    (row["batch_id"], row["inventory_id"], row["batch_id"]),
                )
            elif item_state:
                connection.execute(
                    "UPDATE gmail_cdk_batch_items SET state = ?, failure_reason = ? "
                    "WHERE batch_id = ? AND inventory_id = ?",
                    (item_state, str(reason or "")[:300], row["batch_id"], row["inventory_id"]),
                )
            return True

    def get_assignment(self, assignment_id: str) -> GmailCdkAssignment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gmail_cdk_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
        return self._assignment(row) if row else None

    def find_active_assignment(self, inventory_id: str, job_id: str) -> GmailCdkAssignment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gmail_cdk_assignments "
                "WHERE inventory_id = ? AND job_id = ? AND state = 'active'",
                (str(inventory_id), str(job_id)),
            ).fetchone()
        return self._assignment(row) if row else None

    def get_item(self, batch_id: str, inventory_id: str) -> GmailCdkBatchItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT i.*, b.capacity FROM gmail_cdk_batch_items i "
                "JOIN gmail_cdk_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.inventory_id = ?",
                (str(batch_id), str(inventory_id)),
            ).fetchone()
        return self._item(row) if row else None

    @staticmethod
    def _required(*values: str) -> tuple[str, ...]:
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value for value in normalized):
            raise GmailCdkBatchError("Thiếu dữ liệu batch Gmail CDK")
        return normalized

    @staticmethod
    def _assignment(row: sqlite3.Row) -> GmailCdkAssignment:
        return GmailCdkAssignment(
            row["assignment_id"], row["batch_id"], row["inventory_id"], row["job_id"], row["state"]
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> GmailCdkBatchItem:
        return GmailCdkBatchItem(
            row["batch_id"], row["inventory_id"], int(row["position"]), row["state"],
            int(row["completed_count"]), int(row["capacity"]),
        )

    class _Transaction:
        def __init__(self, store: "GmailCdkBatchStore"):
            self.store = store
            self.connection: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.connection = self.store._connect()
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection

        def __exit__(self, exc_type, exc, tb) -> None:
            if self.connection is None:
                return
            try:
                if exc_type is None:
                    self.connection.commit()
                else:
                    self.connection.rollback()
            finally:
                self.connection.close()

    def _transaction(self) -> "GmailCdkBatchStore._Transaction":
        return self._Transaction(self)
