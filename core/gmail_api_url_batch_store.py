# -*- coding: utf-8 -*-
"""
Gmail API URL batch store - manages Gmail accounts accessed via API URL.
Supports multi-alias batches (1-12 aliases per account).
"""
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
GmailApiUrlBatchError = GmailBatchError
GmailApiUrlBatchConflict = GmailBatchConflict


@dataclass(frozen=True)
class GmailApiUrlBatchItem:
    batch_id: str
    inventory_id: str
    position: int
    state: str
    completed_count: int
    capacity: int
    email: str
    code_url: str


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gmail_api_url_batches (
    batch_id TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL DEFAULT 1,
    routed_domains TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gmail_api_url_batch_items (
    batch_id TEXT NOT NULL,
    inventory_id TEXT NOT NULL,
    email TEXT NOT NULL,
    code_url TEXT NOT NULL,
    position INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    completed_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, inventory_id),
    FOREIGN KEY (batch_id) REFERENCES gmail_api_url_batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gmail_api_url_batch_items_state 
    ON gmail_api_url_batch_items(batch_id, state, position);

CREATE TABLE IF NOT EXISTS gmail_api_url_assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    inventory_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id, inventory_id) 
        REFERENCES gmail_api_url_batch_items(batch_id, inventory_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gmail_api_url_assignments_active 
    ON gmail_api_url_assignments(batch_id, inventory_id, state);
CREATE INDEX IF NOT EXISTS idx_gmail_api_url_assignments_job 
    ON gmail_api_url_assignments(job_id, state);

CREATE TABLE IF NOT EXISTS gmail_api_url_waiters (
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting',
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, job_id),
    FOREIGN KEY (batch_id) REFERENCES gmail_api_url_batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gmail_api_url_waiters_queue
    ON gmail_api_url_waiters(batch_id, state, requested_at);
"""

_WAITER_STALE_SECONDS = 300


class GmailApiUrlBatchStore(GmailBatchStoreBase):
    """Gmail API URL batch store with API URL-specific polling."""

    def _table_prefix(self) -> str:
        return "gmail_api_url"

    def _get_schema_sql(self) -> str:
        return _SCHEMA_SQL

    def claim(self, batch_id: str, job_id: str) -> Assignment:
        """Claim an available Gmail account from batch.
        
        Gmail API URL override: Ensures only ONE active assignment per code_url
        (API mailbox) at any time to prevent OTP retrieval conflicts.
        
        If 2 API records and 3 workers:
        - Worker 1 → API 1 (OK)
        - Worker 2 → API 2 (OK)
        - Worker 3 → No available API (raises GmailBatchConflict)
        """
        batch, owner = self._required(batch_id, job_id)
        prefix = self._table_prefix()
        
        with self._transaction() as connection:
            # Check existing active assignment
            existing = connection.execute(
                f"SELECT * FROM {prefix}_assignments "
                "WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (batch, owner),
            ).fetchone()
            if existing:
                return self._assignment(existing)
            
            # Find available item with exclusive code_url constraint
            row = connection.execute(
                f"SELECT i.*, b.capacity FROM {prefix}_batch_items i "
                f"JOIN {prefix}_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity "
                # Exclude if this inventory_id already has active assignment
                f"AND NOT EXISTS (SELECT 1 FROM {prefix}_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active') "
                # NEW: Exclude if this code_url already has active assignment
                f"AND NOT EXISTS (SELECT 1 FROM {prefix}_assignments a2 "
                f"JOIN {prefix}_batch_items i2 ON a2.batch_id = i2.batch_id "
                "AND a2.inventory_id = i2.inventory_id "
                "WHERE a2.batch_id = i.batch_id AND i2.code_url = i.code_url "
                "AND a2.state = 'active') "
                "ORDER BY i.position LIMIT 1",
                (batch,),
            ).fetchone()
            
            if row is None:
                raise GmailBatchConflict("No available Gmail account in batch")
            
            # Create assignment
            assignment_id = uuid.uuid4().hex
            connection.execute(
                f"INSERT INTO {prefix}_assignments "
                "(assignment_id, batch_id, inventory_id, job_id, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (assignment_id, batch, row["inventory_id"], owner),
            )
            
            created = connection.execute(
                f"SELECT * FROM {prefix}_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return self._assignment(created)

    def claim_waiting(self, batch_id: str, job_id: str) -> Optional[Assignment]:
        """Claim through the durable FIFO queue, returning None while waiting.

        A waiting job is persisted before it competes for a code URL. This keeps
        concurrent workers from treating a temporary URL lock as exhaustion and
        gives the next process a recoverable queue after a worker restart.
        """
        batch, owner = self._required(batch_id, job_id)
        prefix = self._table_prefix()

        with self._transaction() as connection:
            existing = connection.execute(
                f"SELECT * FROM {prefix}_assignments "
                "WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (batch, owner),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE gmail_api_url_waiters SET state = 'assigned', "
                    "updated_at = CURRENT_TIMESTAMP WHERE batch_id = ? AND job_id = ?",
                    (batch, owner),
                )
                return self._assignment(existing)

            connection.execute(
                "UPDATE gmail_api_url_waiters SET state = 'expired', "
                "updated_at = CURRENT_TIMESTAMP, last_error = 'stale waiter' "
                "WHERE batch_id = ? AND state = 'waiting' "
                "AND updated_at < datetime('now', ?)",
                (batch, f"-{_WAITER_STALE_SECONDS} seconds"),
            )
            connection.execute(
                "INSERT INTO gmail_api_url_waiters(batch_id, job_id) VALUES (?, ?) "
                "ON CONFLICT(batch_id, job_id) DO UPDATE SET "
                "state = CASE WHEN gmail_api_url_waiters.state = 'assigned' "
                "THEN 'assigned' ELSE 'waiting' END, "
                "requested_at = CASE WHEN gmail_api_url_waiters.state IN ('expired', 'cancelled') "
                "THEN CURRENT_TIMESTAMP ELSE gmail_api_url_waiters.requested_at END, "
                "updated_at = CURRENT_TIMESTAMP, attempts = gmail_api_url_waiters.attempts + 1",
                (batch, owner),
            )

            waiter = connection.execute(
                "SELECT requested_at FROM gmail_api_url_waiters "
                "WHERE batch_id = ? AND job_id = ? AND state = 'waiting'",
                (batch, owner),
            ).fetchone()
            if waiter is None:
                return None
            head = connection.execute(
                "SELECT job_id FROM gmail_api_url_waiters "
                "WHERE batch_id = ? AND state = 'waiting' "
                "ORDER BY requested_at, rowid LIMIT 1",
                (batch,),
            ).fetchone()
            if head is None or head["job_id"] != owner:
                return None

            row = connection.execute(
                f"SELECT i.*, b.capacity FROM {prefix}_batch_items i "
                f"JOIN {prefix}_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity "
                f"AND NOT EXISTS (SELECT 1 FROM {prefix}_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active') "
                f"AND NOT EXISTS (SELECT 1 FROM {prefix}_assignments a2 "
                f"JOIN {prefix}_batch_items i2 ON a2.batch_id = i2.batch_id "
                "AND a2.inventory_id = i2.inventory_id "
                "WHERE a2.batch_id = i.batch_id AND i2.code_url = i.code_url "
                "AND a2.state = 'active') "
                "ORDER BY i.position LIMIT 1",
                (batch,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "UPDATE gmail_api_url_waiters SET updated_at = CURRENT_TIMESTAMP, "
                    "last_error = 'code_url locked' WHERE batch_id = ? AND job_id = ?",
                    (batch, owner),
                )
                return None

            assignment_id = uuid.uuid4().hex
            connection.execute(
                f"INSERT INTO {prefix}_assignments "
                "(assignment_id, batch_id, inventory_id, job_id, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (assignment_id, batch, row["inventory_id"], owner),
            )
            connection.execute(
                "UPDATE gmail_api_url_waiters SET state = 'assigned', "
                "updated_at = CURRENT_TIMESTAMP, last_error = '' "
                "WHERE batch_id = ? AND job_id = ?",
                (batch, owner),
            )
            created = connection.execute(
                f"SELECT * FROM {prefix}_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return self._assignment(created)

    def discard(self, assignment_id: str, reason: str = "") -> bool:
        """Retire one failed alias so later jobs claim the next inventory item."""
        value = str(assignment_id or "").strip()
        if not value:
            raise GmailBatchError("Assignment ID cannot be empty")

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
                (value,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] not in {"active", "failed", "released"}:
                return False
            connection.execute(
                "UPDATE gmail_api_url_assignments SET state = 'failed', reason = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE assignment_id = ?",
                (str(reason or "")[:300], value),
            )
            connection.execute(
                "UPDATE gmail_api_url_batch_items SET state = 'exhausted', "
                "failure_reason = ? WHERE batch_id = ? AND inventory_id = ?",
                (
                    str(reason or "")[:300],
                    row["batch_id"],
                    row["inventory_id"],
                ),
            )
        return True

    def poll_otp(
        self,
        assignment: Assignment,
        *,
        after_ts: Optional[float] = None,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        """Poll OTP via Gmail API URL client."""
        from . import gmail_api_url_client
        
        # Parse both legacy "email----url::N" and current
        # create_batch_multi format "alias----url".
        try:
            if "::" in assignment.inventory_id:
                base_part, alias_index = assignment.inventory_id.rsplit("::", 1)
                int(alias_index)
            else:
                base_part = assignment.inventory_id
            email, code_url = base_part.split("----", 1)
        except (ValueError, IndexError):
            raise GmailBatchError(f"Invalid inventory_id format: {assignment.inventory_id}")

        return gmail_api_url_client.poll_verification_code(
            gmail_api_url_client.GmailApiUrlAccount(email=email, code_url=code_url),
            after_ts=after_ts,
            max_wait=timeout,
            poll_interval=poll_interval,
        )

    def create_batch(
        self,
        email_url_pairs: list[tuple[str, str]],
        *,
        capacity: int,
        routed_domains=(),
    ) -> str:
        """Create new Gmail API URL batch with multi-alias support.
        
        Args:
            email_url_pairs: List of (email, code_url) tuples
            capacity: Number of aliases per email (1-12)
            routed_domains: Optional domain routing hints
            
        Returns:
            batch_id
        """
        pairs = [
            (str(email or "").strip(), str(url or "").strip())
            for email, url in email_url_pairs
            if str(email or "").strip() and str(url or "").strip()
        ]
        limit = int(capacity)
        
        if not pairs:
            raise GmailBatchError("Batch needs at least one email----url pair")
        if not 1 <= limit <= 12:
            raise GmailBatchError("Capacity must be between 1 and 12")
        
        batch_id = uuid.uuid4().hex
        
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO gmail_api_url_batches(batch_id, capacity, routed_domains) VALUES (?, ?, ?)",
                (batch_id, limit, json.dumps(list(routed_domains), ensure_ascii=False)),
            )
            
            # For each email, create N items (one per alias)
            items = []
            position = 0
            for email, code_url in pairs:
                for alias_index in range(limit):
                    inventory_id = f"{email}----{code_url}::{alias_index}"
                    items.append((batch_id, inventory_id, email, code_url, position))
                    position += 1
            
            connection.executemany(
                "INSERT INTO gmail_api_url_batch_items"
                "(batch_id, inventory_id, email, code_url, position) "
                "VALUES (?, ?, ?, ?, ?)",
                items,
            )
        
        return batch_id

    def create_batch_multi(self, groups: list[dict]) -> str:
        """Create batch from pre-generated alias groups (multi-alias multi-source).

        Each group: {"source_email": str, "code_url": str, "aliases": list[str]}
        inventory_id per item: "{alias}----{code_url}"
        capacity per item = 1 (each alias is used exactly once).

        Interleaves aliases across groups for round-robin claim order, enabling
        parallel processing across different source emails.

        Args:
            groups: List of source-email groups, each carrying pre-generated aliases.

        Returns:
            batch_id

        Raises:
            GmailBatchError: groups empty or contain no valid aliases.
        """
        # Build parallel lists: one list per group
        group_aliases: list[list[tuple[str, str]]] = []
        for group in (groups or []):
            code_url = str(group.get("code_url") or "").strip()
            aliases_for_group: list[tuple[str, str]] = []
            for alias in group.get("aliases") or []:
                alias = str(alias or "").strip()
                if alias and code_url:
                    aliases_for_group.append((alias, code_url))
            if aliases_for_group:
                group_aliases.append(aliases_for_group)

        # Round-robin interleave: take one alias from each group in rotation
        items: list[tuple] = []
        position = 0
        max_aliases = max((len(g) for g in group_aliases), default=0)
        for index in range(max_aliases):
            for group_list in group_aliases:
                if index < len(group_list):
                    alias, code_url = group_list[index]
                    inventory_id = f"{alias}----{code_url}"
                    items.append((inventory_id, alias, code_url, position))
                    position += 1

        if not items:
            raise GmailBatchError("Batch cần ít nhất một alias hợp lệ")

        batch_id = uuid.uuid4().hex
        prefix = self._table_prefix()
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO {prefix}_batches(batch_id, capacity, routed_domains)"
                " VALUES (?, ?, ?)",
                (batch_id, 1, "[]"),
            )
            connection.executemany(
                f"INSERT INTO {prefix}_batch_items"
                "(batch_id, inventory_id, email, code_url, position)"
                " VALUES (?, ?, ?, ?, ?)",
                [(batch_id, inv_id, alias, code_url, pos)
                 for inv_id, alias, code_url, pos in items],
            )
        return batch_id

    def find_item_by_alias(self, alias: str) -> tuple[str, str] | None:
        """Return (alias, code_url) for the given alias email, or None if not found.

        Searches batch items whose email column equals *alias* (set by
        create_batch_multi). Parses the inventory_id to recover code_url.
        """
        from contextlib import closing
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT inventory_id FROM {self._table_prefix()}_batch_items"
                " WHERE email = ? LIMIT 1",
                (str(alias or "").strip(),),
            ).fetchone()
        if not row:
            return None
        try:
            found_alias, code_url = row["inventory_id"].split("----", 1)
            return found_alias, code_url
        except ValueError:
            return None

    def list_aliases_for_code_url(self, code_url: str) -> set[str]:
        """Return aliases already allocated for one source mailbox record."""
        value = str(code_url or "").strip()
        if not value:
            return set()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                f"SELECT DISTINCT email FROM {self._table_prefix()}_batch_items "
                "WHERE code_url = ?",
                (value,),
            ).fetchall()
        return {
            str(row["email"] or "").strip().casefold()
            for row in rows
            if str(row["email"] or "").strip()
        }

    def has_pending_items(self, batch_id: str) -> bool:
        """Return whether the batch still has an alias that is not consumed."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity LIMIT 1",
                (str(batch_id or "").strip(),),
            ).fetchone()
        return row is not None

    def cancel_waiter(self, batch_id: str, job_id: str, reason: str = "") -> bool:
        """Mark a queued job as cancelled without changing batch inventory."""
        batch, owner = self._required(batch_id, job_id)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE gmail_api_url_waiters SET state = 'cancelled', "
                "updated_at = CURRENT_TIMESTAMP, last_error = ? "
                "WHERE batch_id = ? AND job_id = ? AND state = 'waiting'",
                (str(reason or "")[:300], batch, owner),
            ).rowcount
        return bool(changed)

    def list_active_assignments(self, batch_id: str) -> list[Assignment]:
        """List active assignments for startup reconciliation."""
        batch = str(batch_id or "").strip()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT * FROM gmail_api_url_assignments "
                "WHERE batch_id = ? AND state = 'active' ORDER BY created_at, assignment_id",
                (batch,),
            ).fetchall()
        return [self._assignment(row) for row in rows]

    def list_reusable_assignments(self, batch_id: str) -> list[Assignment]:
        """List old failed/released assignments whose alias is still reusable."""
        batch = str(batch_id or "").strip()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT a.* FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "WHERE a.batch_id = ? AND a.state IN ('failed', 'released') "
                "AND i.state = 'active' ORDER BY a.updated_at, a.assignment_id",
                (batch,),
            ).fetchall()
        return [self._assignment(row) for row in rows]

    def list_waiting_jobs(self, batch_id: str) -> list[str]:
        """List persisted waiters for terminal-job reconciliation."""
        batch = str(batch_id or "").strip()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT job_id FROM gmail_api_url_waiters "
                "WHERE batch_id = ? AND state = 'waiting' ORDER BY requested_at, rowid",
                (batch,),
            ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def batch_status(self, batch_id: str) -> dict[str, int | bool]:
        """Return durable counts used to distinguish queued work from exhaustion."""
        batch = str(batch_id or "").strip()
        if not batch:
            return {
                "total": 0,
                "pending": 0,
                "completed": 0,
                "exhausted": 0,
                "active_assignments": 0,
                "waiting_jobs": 0,
                "available_code_urls": 0,
                "exhausted_batch": True,
            }
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            row = connection.execute(
                "SELECT SUM(b.capacity) AS total, "
                "SUM(CASE WHEN i.state = 'active' AND i.completed_count < b.capacity "
                "THEN b.capacity - i.completed_count ELSE 0 END) AS pending, "
                "SUM(completed_count) AS completed, "
                "SUM(CASE WHEN i.state = 'exhausted' THEN b.capacity ELSE 0 END) AS exhausted "
                "FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ?",
                (batch,),
            ).fetchone()
            active_assignments = connection.execute(
                "SELECT COUNT(*) FROM gmail_api_url_assignments "
                "WHERE batch_id = ? AND state = 'active'",
                (batch,),
            ).fetchone()[0]
            waiting_jobs = connection.execute(
                "SELECT COUNT(*) FROM gmail_api_url_waiters "
                "WHERE batch_id = ? AND state = 'waiting'",
                (batch,),
            ).fetchone()[0]
            available_code_urls = connection.execute(
                "SELECT COUNT(DISTINCT i.code_url) FROM gmail_api_url_batch_items i "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < (SELECT capacity FROM gmail_api_url_batches WHERE batch_id = ?) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i2 ON a.batch_id = i2.batch_id "
                "AND a.inventory_id = i2.inventory_id "
                "WHERE a.batch_id = i.batch_id AND i2.code_url = i.code_url AND a.state = 'active'"
                ")",
                (batch, batch),
            ).fetchone()[0]
        total = int(row["total"] or 0)
        pending = int(row["pending"] or 0)
        return {
            "total": total,
            "pending": pending,
            "completed": int(row["completed"] or 0),
            "exhausted": int(row["exhausted"] or 0),
            "active_assignments": int(active_assignments or 0),
            "waiting_jobs": int(waiting_jobs or 0),
            "available_code_urls": int(available_code_urls or 0),
            "exhausted_batch": pending == 0,
        }

    def exhaust(self, assignment_id: str, reason: str = "") -> bool:
        """Mark assignment and item as exhausted (API URL-specific)."""
        return self._finish(assignment_id, "exhausted", item_state="exhausted", reason=reason)

    def get_assignment(self, assignment_id: str) -> Optional[Assignment]:
        """Get assignment by ID."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
        return self._assignment(row) if row else None

    def get_item(self, batch_id: str, inventory_id: str) -> Optional[GmailApiUrlBatchItem]:
        """Get batch item details."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT i.*, b.capacity FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.inventory_id = ?",
                (str(batch_id), str(inventory_id)),
            ).fetchone()
        return self._item(row) if row else None

    @staticmethod
    def _item(row: sqlite3.Row) -> GmailApiUrlBatchItem:
        """Convert database row to GmailApiUrlBatchItem."""
        return GmailApiUrlBatchItem(
            row["batch_id"], row["inventory_id"], int(row["position"]), row["state"],
            int(row["completed_count"]), int(row["capacity"]), row["email"], row["code_url"],
        )
