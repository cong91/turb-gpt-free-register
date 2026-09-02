"""Base class for Gmail batch stores with shared SQLite lifecycle."""
from __future__ import annotations

import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from core import app_state_db


class GmailBatchError(RuntimeError):
    """Base error for Gmail batch operations."""


class GmailBatchConflict(GmailBatchError):
    """No available Gmail account in batch for current job."""


@dataclass(frozen=True)
class Assignment:
    """Represents a claimed Gmail account assignment."""
    assignment_id: str
    batch_id: str
    inventory_id: str
    job_id: str
    state: str


class GmailBatchStoreBase(ABC):
    """Abstract base class for Gmail batch stores.
    
    Handles common SQLite operations (claim/complete/fail/release).
    Subclasses implement poll_otp() with provider-specific logic.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 3000):
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        """Create and configure SQLite connection."""
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
        if app_state_db.is_app_state_path(self.path):
            app_state_db.ensure_schema(connection)
        return connection

    @abstractmethod
    def _get_schema_sql(self) -> str:
        """Return provider-specific schema SQL."""

    @abstractmethod
    def poll_otp(
        self,
        assignment: Assignment,
        *,
        after_ts: float | None = None,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> str | None:
        """Poll for OTP code using provider-specific mechanism.
        
        Args:
            assignment: The claimed assignment
            after_ts: Only return OTP received after this timestamp
            timeout: Maximum wait time in seconds
            poll_interval: Polling interval in seconds
            
        Returns:
            OTP code if found, None if timeout
        """

    def claim(self, batch_id: str, job_id: str) -> Assignment:
        """Claim an available Gmail account from batch."""
        batch, owner = self._required(batch_id, job_id)
        with self._transaction() as connection:
            # Check existing active assignment
            existing = connection.execute(
                f"SELECT * FROM {self._table_prefix()}_assignments "
                "WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (batch, owner),
            ).fetchone()
            if existing:
                return self._assignment(existing)
            
            # Find available item
            row = connection.execute(
                f"SELECT i.*, b.capacity FROM {self._table_prefix()}_batch_items i "
                f"JOIN {self._table_prefix()}_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity "
                f"AND NOT EXISTS (SELECT 1 FROM {self._table_prefix()}_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id AND a.state = 'active') "
                "ORDER BY i.position LIMIT 1",
                (batch,),
            ).fetchone()
            
            if row is None:
                raise GmailBatchConflict("No available Gmail account in batch")
            
            # Create assignment
            assignment_id = uuid.uuid4().hex
            connection.execute(
                f"INSERT INTO {self._table_prefix()}_assignments "
                "(assignment_id, batch_id, inventory_id, job_id, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (assignment_id, batch, row["inventory_id"], owner),
            )
            
            created = connection.execute(
                f"SELECT * FROM {self._table_prefix()}_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return self._assignment(created)

    def complete(self, assignment_id: str) -> bool:
        """Mark assignment as completed and increment usage count."""
        return self._finish(assignment_id, "completed", item_state=None)

    def fail(self, assignment_id: str, reason: str = "") -> bool:
        """Mark assignment and item as failed."""
        return self._finish(assignment_id, "failed", item_state="failed", reason=reason)

    def release(self, assignment_id: str, reason: str = "") -> bool:
        """Release assignment without changing item state."""
        return self._finish(assignment_id, "released", item_state=None, reason=reason)

    def _finish(
        self,
        assignment_id: str,
        target: str,
        *,
        item_state: str | None,
        reason: str = "",
    ) -> bool:
        """Internal method to transition assignment state."""
        value = str(assignment_id or "").strip()
        if not value:
            raise GmailBatchError("Assignment ID cannot be empty")
        
        prefix = self._table_prefix()
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM {prefix}_assignments WHERE assignment_id = ?",
                (value,),
            ).fetchone()
            
            if row is None:
                return False
            if row["state"] == target:
                return True
            if row["state"] != "active":
                return False
            
            # Update assignment state
            connection.execute(
                f"UPDATE {prefix}_assignments SET state = ?, reason = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE assignment_id = ?",
                (target, str(reason or "")[:300], value),
            )
            
            # Handle completed: increment counter and check exhaustion
            if target == "completed":
                connection.execute(
                    f"UPDATE {prefix}_batch_items SET completed_count = completed_count + 1 "
                    "WHERE batch_id = ? AND inventory_id = ?",
                    (row["batch_id"], row["inventory_id"]),
                )
                connection.execute(
                    f"UPDATE {prefix}_batch_items SET state = 'exhausted' "
                    "WHERE batch_id = ? AND inventory_id = ? "
                    f"AND completed_count >= (SELECT capacity FROM {prefix}_batches WHERE batch_id = ?)",
                    (row["batch_id"], row["inventory_id"], row["batch_id"]),
                )
            # Handle failed: do NOT update item state, allow retry
            # (item_state parameter is ignored for failed assignments)
            
            return True

    def find_active_assignment(
        self, batch_id: str, job_id: str
    ) -> Assignment | None:
        """Find active assignment for batch+job combination."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT * FROM {self._table_prefix()}_assignments "
                "WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (str(batch_id), str(job_id)),
            ).fetchone()
        return self._assignment(row) if row else None

    def find_active_assignment_for_alias(self, alias: str) -> Assignment | None:
        """Find active assignment for a given alias email.
        
        Used during release_account() to finalize orphaned assignments
        when a job fails without explicitly completing its assignment.
        """
        prefix = self._table_prefix()
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT a.* FROM {prefix}_assignments a "
                f"INNER JOIN {prefix}_batch_items i ON a.batch_id = i.batch_id "
                f"AND a.inventory_id = i.inventory_id "
                f"WHERE i.email = ? AND a.state = 'active' LIMIT 1",
                (str(alias or "").strip(),),
            ).fetchone()
        return self._assignment(row) if row else None

    @abstractmethod
    def _table_prefix(self) -> str:
        """Return table prefix for SQL queries (e.g., 'gmail_cdk' or 'gmail_api_url')."""

    @staticmethod
    def _required(*values: str) -> tuple[str, ...]:
        """Validate and normalize required string values."""
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value for value in normalized):
            raise GmailBatchError("Missing required batch data")
        return normalized

    @staticmethod
    def _assignment(row: sqlite3.Row) -> Assignment:
        """Convert database row to Assignment."""
        return Assignment(
            row["assignment_id"],
            row["batch_id"],
            row["inventory_id"],
            row["job_id"],
            row["state"],
        )

    class _Transaction:
        """Transaction context manager."""
        def __init__(self, store: GmailBatchStoreBase):
            self.store = store
            self.connection: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.connection = self.store._connect()
            self.connection.executescript(self.store._get_schema_sql())
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

    def _transaction(self) -> _Transaction:
        """Create transaction context manager."""
        return self._Transaction(self)
