"""
Gmail API URL batch store - manages Gmail accounts accessed via API URL.
Supports multi-alias batches (1-12 aliases per account).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass

from .gmail_batch_store_base import (
    Assignment,
    GmailBatchConflict,
    GmailBatchError,
    GmailBatchStoreBase,
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

CREATE TABLE IF NOT EXISTS gmail_api_url_provision_leases (
    lease_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_WAITER_STALE_SECONDS = 300
_PROVISION_LEASE_KEY = "canonical_purchase"
_PROVISION_LEASE_SECONDS = 300


class GmailApiUrlBatchStore(GmailBatchStoreBase):
    """Gmail API URL batch store with API URL-specific polling."""

    def _table_prefix(self) -> str:
        return "gmail_api_url"

    def _get_schema_sql(self) -> str:
        return _SCHEMA_SQL

    def runtime_blocked_canonical_roots(self) -> set[str]:
        """Snapshot raw-pool roots that must not receive new assignments."""
        from . import db

        return db.gmail_api_url_blocked_canonical_roots(sqlite_path=self.path)

    @staticmethod
    def _runtime_alias_keys(alias: object) -> set[str]:
        """Return exact and canonical keys for one Gmail spelling.

        Historical imports may use dots, plus tags, or ``googlemail.com``.
        Runtime ownership is by the underlying Gmail root; the exact key is
        retained for existing callers and diagnostics.
        """
        from .gmail_aliases import GmailAliasError, canonical_gmail

        value = str(alias or "").strip().casefold()
        if not value:
            return set()
        keys = {value}
        try:
            keys.add(canonical_gmail(value))
        except GmailAliasError:
            pass
        return keys

    @classmethod
    def _runtime_alias_is_unavailable(
        cls,
        alias: object,
        unavailable_aliases: set[str],
    ) -> bool:
        value = str(alias or "").strip().casefold()
        return bool(value and value in unavailable_aliases)

    @classmethod
    def _runtime_alias_key(cls, alias: object) -> str:
        keys = cls._runtime_alias_keys(alias)
        if not keys:
            return ""
        from .gmail_aliases import GmailAliasError, canonical_gmail

        value = str(alias or "").strip().casefold()
        try:
            return canonical_gmail(value)
        except GmailAliasError:
            return value

    @contextmanager
    def _runtime_transaction(self):
        """Serialize raw-pool status snapshots with canonical assignments."""
        from . import db

        with db._LOCK, self._transaction() as connection:
            yield connection

    def acquire_provision_lease(
        self,
        owner: str,
        *,
        lease_seconds: int = _PROVISION_LEASE_SECONDS,
    ) -> bool:
        """Reserve the canonical ledger while deciding whether to buy a source."""
        value = str(owner or "").strip()
        if not value:
            raise ValueError("Gmail API provision lease owner is required")
        now = time.time()
        expires_at = now + max(1, int(lease_seconds))
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM gmail_api_url_provision_leases WHERE expires_at <= ?",
                (now,),
            )
            row = connection.execute(
                "SELECT owner FROM gmail_api_url_provision_leases WHERE lease_key = ?",
                (_PROVISION_LEASE_KEY,),
            ).fetchone()
            if row is not None and str(row["owner"]) != value:
                return False
            connection.execute(
                "INSERT INTO gmail_api_url_provision_leases"
                "(lease_key, owner, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(lease_key) DO UPDATE SET owner = excluded.owner, "
                "expires_at = excluded.expires_at, updated_at = CURRENT_TIMESTAMP",
                (_PROVISION_LEASE_KEY, value, expires_at),
            )
        return True

    def release_provision_lease(self, owner: str) -> bool:
        value = str(owner or "").strip()
        if not value:
            return False
        with self._transaction() as connection:
            result = connection.execute(
                "DELETE FROM gmail_api_url_provision_leases "
                "WHERE lease_key = ? AND owner = ?",
                (_PROVISION_LEASE_KEY, value),
            )
        return result.rowcount > 0

    @staticmethod
    def _provision_claim_allowed(
        connection: sqlite3.Connection,
        provision_owner: str | None,
    ) -> bool:
        """Block new claims while another worker is buying a shared source."""
        row = connection.execute(
            "SELECT owner, expires_at FROM gmail_api_url_provision_leases "
            "WHERE lease_key = ?",
            (_PROVISION_LEASE_KEY,),
        ).fetchone()
        if row is None or float(row["expires_at"] or 0) <= time.time():
            return True
        return bool(provision_owner and str(row["owner"]) == str(provision_owner))

    def _claim_is_blocked_by_provision(self, connection: sqlite3.Connection) -> bool:
        return not self._provision_claim_allowed(connection, None)

    @staticmethod
    def _alias_is_runtime_blocked(alias: str, blocked_roots: set[str]) -> bool:
        if not blocked_roots:
            return False
        from .gmail_aliases import GmailAliasError, canonical_gmail

        try:
            return canonical_gmail(alias) in blocked_roots
        except GmailAliasError:
            return False

    @staticmethod
    def _is_legacy_capacity_slot(inventory_id: object) -> bool:
        """Identify the pre-alias ``email----url::index`` inventory shape."""
        value = str(inventory_id or "")
        suffix = value.rsplit("::", 1)[-1] if "::" in value else ""
        return bool(suffix.isdigit())

    def _first_runtime_eligible_row(
        self,
        rows: list[sqlite3.Row],
        blocked_roots: set[str],
        unavailable_aliases: set[str] | None = None,
        shadow_items: set[tuple[str, str]] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        unavailable = unavailable_aliases or set()
        shadows = shadow_items or set()
        return next(
            (
                row for row in rows
                if not self._runtime_alias_is_unavailable(row["email"], unavailable)
                and (str(row["batch_id"]), str(row["inventory_id"])) not in shadows
                and not self._alias_is_runtime_blocked(str(row["email"] or ""), blocked_roots)
            ),
            None,
        )

    @staticmethod
    def _shadow_aliases_in_connection(
        connection: sqlite3.Connection,
    ) -> set[tuple[str, str]]:
        """Return duplicate rows hidden behind one logical alias owner.

        Old databases may contain duplicate rows from before the shared-ledger
        uniqueness check. The oldest provider URL owns a canonical Gmail root;
        rows for another URL are shadowed, while aliases on the owner URL remain
        usable sequentially. Exact non-legacy duplicates are also shadowed.
        """
        rows = connection.execute(
            "SELECT lower(i.email) AS email, i.code_url, i.batch_id, i.inventory_id, "
            "i.rowid AS row_id, i.state, i.completed_count, b.capacity "
            "FROM gmail_api_url_batch_items i "
            "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
            "ORDER BY i.rowid"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        shadows: set[tuple[str, str]] = set()
        for row in rows:
            # Group by the underlying Gmail root, then only shadow rows tied to
            # a different provider URL. Variants on one URL remain independent
            # sequential slots for that source.
            alias = GmailApiUrlBatchStore._runtime_alias_key(row["email"])
            if not alias:
                continue
            grouped.setdefault(alias, []).append(row)
        for alias_rows in grouped.values():
            code_urls = {
                str(row["code_url"] or "").strip()
                for row in alias_rows
                if str(row["code_url"] or "").strip()
            }
            if not code_urls:
                continue
            # The oldest URL owns a legacy root permanently.  Choosing a newer
            # active row after the old one is exhausted would let historical
            # dotted/undotted duplicates switch provider URLs over time.
            primary = alias_rows[0]
            primary_url = str(primary["code_url"] or "").strip()
            shadows.update(
                (str(row["batch_id"]), str(row["inventory_id"]))
                for row in alias_rows
                if str(row["code_url"] or "").strip() != primary_url
            )
            seen_exact: set[tuple[str, str]] = set()
            legacy_exact: set[tuple[str, str]] = set()
            for row in alias_rows:
                row_url = str(row["code_url"] or "").strip()
                exact_key = (row_url, str(row["email"] or "").strip().casefold())
                is_legacy = GmailApiUrlBatchStore._is_legacy_capacity_slot(
                    row["inventory_id"]
                )
                duplicate_legacy_slot = is_legacy and exact_key in legacy_exact
                if row_url != primary_url or (
                    exact_key in seen_exact and not duplicate_legacy_slot
                ):
                    shadows.add((str(row["batch_id"]), str(row["inventory_id"])))
                seen_exact.add(exact_key)
                if is_legacy:
                    legacy_exact.add(exact_key)
        return shadows

    @staticmethod
    def _globally_terminal_aliases_in_connection(
        connection: sqlite3.Connection,
    ) -> set[str]:
        """Return aliases consumed or failed anywhere in the shared ledger."""
        aliases: set[str] = set()
        q8_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('qan8_aliases', 'qan8_sources')"
            ).fetchall()
        }
        if q8_tables == {"qan8_aliases", "qan8_sources"}:
            rows = connection.execute(
                "SELECT DISTINCT lower(x.alias) AS alias FROM qan8_aliases x "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE x.state IN ('consumed', 'failed')"
            ).fetchall()
            for row in rows:
                if str(row["alias"] or "").strip():
                    aliases.add(str(row["alias"] or "").strip().casefold())
        return aliases

    @classmethod
    def _globally_unavailable_aliases_in_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> set[str]:
        """Return terminal aliases plus aliases currently owned by a job."""
        aliases = cls._globally_terminal_aliases_in_connection(connection)
        q8_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'qan8_aliases'"
            ).fetchall()
        }
        if q8_tables:
            rows = connection.execute(
                "SELECT DISTINCT lower(alias) AS alias FROM qan8_aliases "
                "WHERE state IN ('consumed', 'failed', 'active')"
            ).fetchall()
            for row in rows:
                if str(row["alias"] or "").strip():
                    aliases.add(str(row["alias"] or "").strip().casefold())
        return aliases

    def claim(
        self,
        batch_id: str,
        job_id: str,
        *,
        provision_owner: str | None = None,
    ) -> Assignment:
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
        with self._runtime_transaction() as connection:
            blocked_roots = self.runtime_blocked_canonical_roots()
            unavailable_aliases = self._globally_unavailable_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
            # Check existing active assignment
            existing = connection.execute(
                f"SELECT * FROM {prefix}_assignments "
                "WHERE batch_id = ? AND job_id = ? AND state = 'active'",
                (batch, owner),
            ).fetchone()
            if existing:
                return self._assignment(existing)

            if not self._provision_claim_allowed(connection, provision_owner):
                raise GmailBatchConflict("Gmail API URL provision is busy")
            
            # Find available item with exclusive code_url constraint
            rows = connection.execute(
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
                "WHERE i2.code_url = i.code_url "
                "AND a2.state = 'active') "
                "ORDER BY RANDOM()",
                (batch,),
            ).fetchall()
            row = self._first_runtime_eligible_row(
                rows,
                blocked_roots,
                unavailable_aliases,
                shadow_items,
                connection,
            )
            
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

    def claim_waiting(
        self,
        batch_id: str,
        job_id: str,
        *,
        provision_owner: str | None = None,
    ) -> Assignment | None:
        """Claim through the durable FIFO queue, returning None while waiting.

        A waiting job is persisted before it competes for a code URL. This keeps
        concurrent workers from treating a temporary URL lock as exhaustion and
        gives the next process a recoverable queue after a worker restart. The
        waiter order stays FIFO while the eligible alias is selected randomly.
        """
        batch, owner = self._required(batch_id, job_id)
        prefix = self._table_prefix()

        with self._runtime_transaction() as connection:
            blocked_roots = self.runtime_blocked_canonical_roots()
            unavailable_aliases = self._globally_unavailable_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
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

            if not self._provision_claim_allowed(connection, provision_owner):
                return None

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

            rows = connection.execute(
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
                "WHERE i2.code_url = i.code_url "
                "AND a2.state = 'active') "
                "ORDER BY RANDOM()",
                (batch,),
            ).fetchall()
            row = self._first_runtime_eligible_row(
                rows,
                blocked_roots,
                unavailable_aliases,
                shadow_items,
                connection,
            )
            if row is None:
                connection.execute(
                    "UPDATE gmail_api_url_waiters SET updated_at = CURRENT_TIMESTAMP, "
                    "last_error = ? WHERE batch_id = ? AND job_id = ?",
                    (
                        "source disabled"
                        if blocked_roots and rows
                        else "code_url locked",
                        batch,
                        owner,
                    ),
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

    def claim_any_available(
        self,
        job_id: str,
        *,
        exclude_batch_id: str | None = None,
        provision_owner: str | None = None,
    ) -> Assignment | None:
        """Claim one available alias from the shared Gmail ledger.

        QAN8 jobs own an empty target batch until a purchase is required.  This
        method lets those jobs consume aliases already materialized by another
        Gmail/QAN8 batch while keeping the same code-url exclusivity and one
        assignment owner used by the normal batch queue.
        """
        owner = str(job_id or "").strip()
        if not owner:
            raise GmailBatchError("Job ID cannot be empty")
        excluded = str(exclude_batch_id or "").strip()
        with self._runtime_transaction() as connection:
            blocked_roots = self.runtime_blocked_canonical_roots()
            unavailable_aliases = self._globally_unavailable_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
            existing = connection.execute(
                "SELECT * FROM gmail_api_url_assignments "
                "WHERE job_id = ? AND state = 'active' LIMIT 1",
                (owner,),
            ).fetchone()
            if existing is not None:
                return self._assignment(existing)
            if not self._provision_claim_allowed(connection, provision_owner):
                return None
            clauses = [
                "i.state = 'active'",
                "i.completed_count < b.capacity",
                ("NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active')"),
                ("NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a2 "
                "JOIN gmail_api_url_batch_items i2 ON a2.batch_id = i2.batch_id "
                "AND a2.inventory_id = i2.inventory_id "
                "WHERE i2.code_url = i.code_url AND a2.state = 'active')"),
            ]
            params: list[str] = []
            if excluded:
                clauses.append("i.batch_id != ?")
                params.append(excluded)
            rows = connection.execute(
                "SELECT i.* FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY b.created_at, i.position, i.created_at",
                tuple(params),
            ).fetchall()
            row = self._first_runtime_eligible_row(
                rows,
                blocked_roots,
                unavailable_aliases,
                shadow_items,
                connection,
            )
            if row is None:
                return None
            assignment_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO gmail_api_url_assignments "
                "(assignment_id, batch_id, inventory_id, job_id, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (assignment_id, row["batch_id"], row["inventory_id"], owner),
            )
            created = connection.execute(
                "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            return self._assignment(created)

    def has_available_item(self, *, exclude_batch_id: str | None = None) -> bool:
        """Return whether any unassigned active alias remains in the ledger."""
        excluded = str(exclude_batch_id or "").strip()
        blocked_roots = self.runtime_blocked_canonical_roots()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            unavailable_aliases = self._globally_unavailable_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
            clauses = [
                "i.state = 'active'",
                "i.completed_count < b.capacity",
                ("NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active')"),
                ("NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a2 "
                "JOIN gmail_api_url_batch_items i2 ON a2.batch_id = i2.batch_id "
                "AND a2.inventory_id = i2.inventory_id "
                "WHERE i2.code_url = i.code_url AND a2.state = 'active')"),
            ]
            params: list[str] = []
            if excluded:
                clauses.append("i.batch_id != ?")
                params.append(excluded)
            rows = connection.execute(
                "SELECT i.batch_id, i.inventory_id, i.email, i.code_url FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
            row = next(
                (
                    item for item in rows
                    if not self._runtime_alias_is_unavailable(
                        item["email"], unavailable_aliases
                    )
                    and (str(item["batch_id"]), str(item["inventory_id"])) not in shadow_items
                    if not self._alias_is_runtime_blocked(str(item["email"] or ""), blocked_roots)
                ),
                None,
            )
        return row is not None

    def has_pending_item(self, *, exclude_batch_id: str | None = None) -> bool:
        """Return whether any active alias still has capacity, even if locked.

        A code URL is deliberately exclusive while one job is polling it.  The
        allocator must wait for that URL to become free instead of treating the
        temporary lock as an empty inventory and buying another source.
        """
        excluded = str(exclude_batch_id or "").strip()
        blocked_roots = self.runtime_blocked_canonical_roots()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            terminal_aliases = self._globally_terminal_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
            clauses = [
                "i.state = 'active'",
                "i.completed_count < b.capacity",
            ]
            params: list[str] = []
            if excluded:
                clauses.append("i.batch_id != ?")
                params.append(excluded)
            rows = connection.execute(
                "SELECT i.batch_id, i.inventory_id, i.email, i.code_url FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
            row = next(
                (
                    item for item in rows
                    if not self._runtime_alias_is_unavailable(
                        item["email"], terminal_aliases
                    )
                    and (str(item["batch_id"]), str(item["inventory_id"])) not in shadow_items
                    if not self._alias_is_runtime_blocked(str(item["email"] or ""), blocked_roots)
                ),
                None,
            )
        return row is not None

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

    def ensure_alias_items(
        self,
        code_url: str,
        aliases: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Return one reusable Gmail item for every alias, creating missing items."""
        with self._transaction() as connection:
            return self.ensure_alias_items_in_connection(connection, code_url, aliases)

    def ensure_alias_items_in_connection(
        self,
        connection: sqlite3.Connection,
        code_url: str,
        aliases: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Ensure alias items while the caller owns the SQLite transaction."""
        url = str(code_url or "").strip()
        values: list[str] = []
        seen: set[str] = set()
        for alias in aliases or []:
            value = str(alias or "").strip().lower()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
        if not url or not values:
            raise GmailBatchError("Gmail API alias items require code_url and aliases")

        result: dict[str, tuple[str, str]] = {}
        missing: list[str] = []
        for alias in values:
            if self._alias_owned_by_other_code_url_in_connection(connection, alias, url):
                raise GmailBatchConflict(
                    f"Gmail API alias {alias} is already linked to another code_url"
                )
            rows = connection.execute(
                "SELECT i.batch_id, i.inventory_id, i.code_url, i.state, "
                "i.completed_count, b.capacity FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE lower(i.email) = ? "
                "ORDER BY CASE WHEN i.state = 'active' AND i.completed_count < b.capacity "
                "THEN 0 ELSE 1 END, i.created_at, i.position",
                (alias,),
            ).fetchall()
            conflicting = next(
                (row for row in rows if str(row["code_url"] or "").strip() != url),
                None,
            )
            if conflicting is not None:
                raise GmailBatchConflict(
                    f"Gmail API alias {alias} is already linked to another code_url"
                )
            if rows:
                row = rows[0]
                result[alias] = (str(row["batch_id"]), str(row["inventory_id"]))
                continue

            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources"}:
                q8_rows = connection.execute(
                    "SELECT s.code_url FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE lower(x.alias) = ?",
                    (alias,),
                ).fetchall()
                q8_conflicting = next(
                    (
                        row for row in q8_rows
                        if str(row["code_url"] or "").strip() != url
                    ),
                    None,
                )
                if q8_conflicting is not None:
                    raise GmailBatchConflict(
                        f"Gmail API alias {alias} is already linked to another code_url"
                    )
            missing.append(alias)

        if missing:
            batch_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO gmail_api_url_batches(batch_id, capacity, routed_domains) "
                "VALUES (?, 1, '[]')",
                (batch_id,),
            )
            connection.executemany(
                "INSERT INTO gmail_api_url_batch_items "
                "(batch_id, inventory_id, email, code_url, position) VALUES (?, ?, ?, ?, ?)",
                (
                    (batch_id, f"{alias}----{url}", alias, url, position)
                    for position, alias in enumerate(missing)
                ),
            )
            result.update({
                alias: (batch_id, f"{alias}----{url}") for alias in missing
            })
        return result

    def alias_ledger_in_connection(
        self,
        connection: sqlite3.Connection,
        code_url: str,
        alias: str,
    ) -> dict[str, list[dict[str, object]]]:
        """Read all Gmail ownership rows for one exact API URL and alias."""
        url = str(code_url or "").strip()
        email = str(alias or "").strip().casefold()
        if not url or not email:
            raise GmailBatchError("Gmail API alias ledger requires code_url and alias")
        items = connection.execute(
            "SELECT i.batch_id, i.inventory_id, i.state, i.completed_count, "
            "i.failure_reason, b.capacity FROM gmail_api_url_batch_items i "
            "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
            "WHERE i.code_url = ? AND lower(i.email) = ? "
            "ORDER BY i.created_at, i.position",
            (url, email),
        ).fetchall()
        assignments = connection.execute(
            "SELECT a.assignment_id, a.batch_id, a.inventory_id, a.job_id "
            "FROM gmail_api_url_assignments a "
            "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
            "AND i.inventory_id = a.inventory_id "
            "WHERE a.state = 'active' AND i.code_url = ? AND lower(i.email) = ? "
            "ORDER BY a.created_at, a.assignment_id",
            (url, email),
        ).fetchall()
        return {
            "items": [dict(row) for row in items],
            "active_assignments": [dict(row) for row in assignments],
        }

    def exhaust_alias_items_in_connection(
        self,
        connection: sqlite3.Connection,
        code_url: str,
        alias: str,
        *,
        failure_reason: str = "",
    ) -> dict[str, tuple[str, str]]:
        """Retire every Gmail item for an alias and return its canonical reference."""
        refs = self.ensure_alias_items_in_connection(connection, code_url, [alias])
        email = str(alias or "").strip().casefold()
        message = str(failure_reason or "")[:300]
        connection.execute(
            "UPDATE gmail_api_url_batch_items SET state = 'exhausted', failure_reason = ? "
            "WHERE code_url = ? AND lower(email) = ?",
            (message, str(code_url or "").strip(), email),
        )
        return refs

    def claim_alias_in_connection(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        inventory_id: str,
        job_id: str,
    ) -> Assignment | None:
        """Claim one exact item while the caller owns the SQLite transaction."""
        batch, owner = self._required(batch_id, job_id)
        inventory = str(inventory_id or "").strip()
        if not inventory:
            raise GmailBatchError("Inventory ID cannot be empty")
        existing = connection.execute(
            "SELECT * FROM gmail_api_url_assignments WHERE job_id = ? AND state = 'active' LIMIT 1",
            (owner,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["batch_id"]) == batch
                and str(existing["inventory_id"]) == inventory
            ):
                return self._assignment(existing)
            return None
        if not self._provision_claim_allowed(connection, None):
            return None
        row = connection.execute(
            "SELECT i.*, b.capacity FROM gmail_api_url_batch_items i "
            "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
            "WHERE i.batch_id = ? AND i.inventory_id = ? AND i.state = 'active' "
            "AND i.completed_count < b.capacity "
            "AND NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
            "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
            "AND a.state = 'active') "
            "AND NOT EXISTS (SELECT 1 FROM gmail_api_url_assignments a2 "
            "JOIN gmail_api_url_batch_items i2 ON a2.batch_id = i2.batch_id "
            "AND a2.inventory_id = i2.inventory_id "
            "WHERE a2.state = 'active' AND i2.code_url = i.code_url)",
            (batch, inventory),
        ).fetchone()
        if row is None:
            return None
        assignment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO gmail_api_url_assignments "
            "(assignment_id, batch_id, inventory_id, job_id, state) VALUES (?, ?, ?, ?, 'active')",
            (assignment_id, batch, inventory, owner),
        )
        created = connection.execute(
            "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
        return self._assignment(created)

    def finish_assignment_in_connection(
        self,
        connection: sqlite3.Connection,
        assignment_id: str,
        target: str,
        *,
        reason: str = "",
    ) -> bool:
        """Finish a Gmail assignment inside a caller-owned transaction."""
        return self._finish_in_connection(connection, assignment_id, target, reason=reason)

    def discard_assignment_in_connection(
        self,
        connection: sqlite3.Connection,
        assignment_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """Fail an assignment and permanently retire its Gmail alias."""
        value = str(assignment_id or "").strip()
        message = str(reason or "")[:300]
        row = connection.execute(
            "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
            (value,),
        ).fetchone()
        if row is None:
            return False
        if row["state"] == "failed":
            return True
        if row["state"] != "active":
            return False
        connection.execute(
            "UPDATE gmail_api_url_assignments SET state = 'failed', reason = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE assignment_id = ?",
            (message, value),
        )
        connection.execute(
            "UPDATE gmail_api_url_batch_items SET state = 'exhausted', failure_reason = ? "
            "WHERE batch_id = ? AND inventory_id = ?",
            (message, row["batch_id"], row["inventory_id"]),
        )
        return True

    def quarantine_code_url_in_connection(
        self,
        connection: sqlite3.Connection,
        code_url: str,
        *,
        reason: str = "",
    ) -> int:
        """Quarantine Gmail items for one provider URL in an existing transaction."""
        url = str(code_url or "").strip()
        if not url:
            raise GmailBatchError("Code URL is required")
        message = str(reason or "")[:300]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM gmail_api_url_batch_items "
            "WHERE code_url = ? AND state != 'exhausted'",
            (url,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE gmail_api_url_assignments SET state = 'failed', reason = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE state IN ('active', 'failed', 'released') "
            "AND EXISTS (SELECT 1 FROM gmail_api_url_batch_items i "
            "WHERE i.batch_id = gmail_api_url_assignments.batch_id "
            "AND i.inventory_id = gmail_api_url_assignments.inventory_id AND i.code_url = ?)",
            (message, url),
        )
        connection.execute(
            "UPDATE gmail_api_url_batch_items SET state = 'exhausted', failure_reason = ? "
            "WHERE code_url = ?",
            (message, url),
        )
        return int(item_count or 0)

    def quarantine_code_url(self, code_url: str, *, reason: str = "") -> int:
        """Exhaust every alias backed by a provider URL that returned code 602."""
        url = str(code_url or "").strip()
        if not url:
            raise GmailBatchError("Code URL is required")

        message = str(reason or "")[:300]
        with self._transaction() as connection:
            item_count = self.quarantine_code_url_in_connection(
                connection,
                url,
                reason=message,
            )
            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources', 'qan8_assignments', 'qan8_lanes')"
                ).fetchall()
            }
            q8_item_count = 0
            if q8_tables == {
                "qan8_aliases",
                "qan8_sources",
                "qan8_assignments",
                "qan8_lanes",
            }:
                q8_item_count = connection.execute(
                    "SELECT COUNT(*) FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ? AND x.state IN ('available', 'active')",
                    (url,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE qan8_assignments SET state = 'failed', reason = ?, updated_at = ? "
                    "WHERE state = 'active' AND alias_id IN ("
                    "SELECT x.alias_id FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ?)",
                    (message, time.time(), url),
                )
                connection.execute(
                    "UPDATE qan8_aliases SET state = 'failed' WHERE state IN ('available', 'active') "
                    "AND source_group_id IN (SELECT source_group_id FROM qan8_sources WHERE code_url = ?)",
                    (url,),
                )
                connection.execute(
                    "UPDATE qan8_sources SET state = 'retired', retired_at = ? "
                    "WHERE code_url = ? AND state = 'active'",
                    (time.time(), url),
                )
                connection.execute(
                    "UPDATE qan8_lanes SET current_source_group_id = NULL, active_job_id = NULL, "
                    "failure_reason = ? WHERE current_source_group_id IN ("
                    "SELECT source_group_id FROM qan8_sources WHERE code_url = ?)",
                    (message, url),
                )
        return max(int(item_count or 0), int(q8_item_count or 0))

    def poll_otp(
        self,
        assignment: Assignment,
        *,
        after_ts: float | None = None,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> str | None:
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
            sqlite_path=self.path,
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

    def create_empty_batch(self, *, routed_domains=()) -> str:
        """Create an empty canonical batch that can be filled lazily.

        QAN8 registration creates the canonical Gmail batch before it knows
        whether a purchase is required.  Keeping the batch row without
        inventory lets existing Gmail API aliases and later purchased aliases
        share one queue and one assignment ledger.
        """
        batch_id = uuid.uuid4().hex
        domains = list(routed_domains or ())
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO gmail_api_url_batches(batch_id, capacity, routed_domains) "
                "VALUES (?, 1, ?)",
                (batch_id, json.dumps(domains, ensure_ascii=False)),
            )
        return batch_id

    def append_source_group(
        self,
        batch_id: str,
        source_email: str,
        code_url: str,
        aliases: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Append one purchased source group to an existing canonical batch.

        Alias ownership is checked while holding the same ``BEGIN IMMEDIATE``
        transaction used for insertion.  An alias can only belong to one
        provider URL; a duplicate already in the target batch is treated as an
        idempotent append and returned without creating another item.
        ``source_email`` is retained in the API for purchase provenance but is
        not duplicated in the canonical item schema, where ``email`` is the
        actual alias claimed by workers.
        """
        with self._transaction() as connection:
            return self.append_source_group_in_connection(
                connection,
                batch_id,
                source_email,
                code_url,
                aliases,
            )

    def append_source_group_in_connection(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        source_email: str,
        code_url: str,
        aliases: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Append a source group inside a caller-owned SQLite transaction."""
        batch, url = self._required(batch_id, code_url)
        # Validate the provenance input even though the canonical schema stores
        # aliases rather than the source mailbox itself.
        if not str(source_email or "").strip():
            raise GmailBatchError("Gmail API source group requires source_email")

        values: list[str] = []
        seen: set[str] = set()
        for alias in aliases or []:
            value = str(alias or "").strip().casefold()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
        if not values:
            raise GmailBatchError("Gmail API source group requires aliases")

        target = connection.execute(
            "SELECT 1 FROM gmail_api_url_batches WHERE batch_id = ?",
            (batch,),
        ).fetchone()
        if target is None:
            raise GmailBatchError(f"Gmail API batch does not exist: {batch}")

        # Read all rows for each alias before inserting anything.  The caller's
        # immediate transaction makes this check and the following inserts
        # atomic against concurrent purchasers.
        result: dict[str, tuple[str, str]] = {}
        missing: list[str] = []
        for alias in values:
            if self._alias_owned_by_other_code_url_in_connection(connection, alias, url):
                raise GmailBatchConflict(
                    f"Gmail API alias {alias} is already linked to another code_url"
                )
            rows = connection.execute(
                "SELECT batch_id, inventory_id, code_url FROM gmail_api_url_batch_items "
                "WHERE lower(email) = ? ORDER BY created_at, position",
                (alias,),
            ).fetchall()
            conflicting = next(
                (row for row in rows if str(row["code_url"] or "").strip() != url),
                None,
            )
            if conflicting is not None:
                raise GmailBatchConflict(
                    f"Gmail API alias {alias} is already linked to another code_url"
                )
            in_target = next(
                (row for row in rows if str(row["batch_id"] or "") == batch),
                None,
            )
            if in_target is not None:
                result[alias] = (batch, str(in_target["inventory_id"]))
                continue
            if rows:
                raise GmailBatchConflict(
                    f"Gmail API alias {alias} is already allocated in another batch"
                )

            # QAN8 keeps purchase provenance in a second set of tables.  Check
            # it in this same transaction too, otherwise a concurrent/legacy
            # QAN8 row could bypass canonical alias ownership.
            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources"}:
                q8_rows = connection.execute(
                    "SELECT s.code_url FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE lower(x.alias) = ?",
                    (alias,),
                ).fetchall()
                if q8_rows:
                    q8_conflicting = next(
                        (
                            row for row in q8_rows
                            if str(row["code_url"] or "").strip() != url
                        ),
                        None,
                    )
                    if q8_conflicting is not None:
                        raise GmailBatchConflict(
                            f"Gmail API alias {alias} is already linked to another code_url"
                        )
                    raise GmailBatchConflict(
                        f"Gmail API alias {alias} is already registered in QAN8"
                    )
            missing.append(alias)

        if missing:
            position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 "
                    "FROM gmail_api_url_batch_items WHERE batch_id = ?",
                    (batch,),
                ).fetchone()[0]
                or 0
            )
            connection.executemany(
                "INSERT INTO gmail_api_url_batch_items "
                "(batch_id, inventory_id, email, code_url, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (batch, f"{alias}----{url}", alias, url, position + offset)
                    for offset, alias in enumerate(missing)
                ),
            )
            result.update({
                alias: (batch, f"{alias}----{url}") for alias in missing
            })
        return result

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
        # Build parallel lists: one list per group.  Keep the source mailbox
        # because append_source_group_in_connection performs the shared-ledger
        # ownership check for every insert.
        group_aliases: list[list[tuple[str, str, str]]] = []
        for group in (groups or []):
            source_email = str(group.get("source_email") or "").strip()
            code_url = str(group.get("code_url") or "").strip()
            aliases_for_group: list[tuple[str, str, str]] = []
            seen: set[str] = set()
            for alias in group.get("aliases") or []:
                alias = str(alias or "").strip()
                alias_key = alias.casefold()
                if alias and code_url and source_email and alias_key not in seen:
                    aliases_for_group.append((source_email, alias, code_url))
                    seen.add(alias_key)
            if aliases_for_group:
                group_aliases.append(aliases_for_group)

        # Round-robin interleave: take one alias from each group in rotation
        items: list[tuple[str, str, str]] = []
        max_aliases = max((len(g) for g in group_aliases), default=0)
        for index in range(max_aliases):
            for group_list in group_aliases:
                if index < len(group_list):
                    items.append(group_list[index])

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
            for source_email, alias, code_url in items:
                self.append_source_group_in_connection(
                    connection,
                    batch_id,
                    source_email,
                    code_url,
                    [alias],
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

    def find_item_by_alias_for_job(self, alias: str, job_id: str) -> tuple[str, str] | None:
        """Resolve the mailbox URL through the active assignment owner first."""
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            row = connection.execute(
                "SELECT i.email, i.code_url FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "WHERE a.job_id = ? AND a.state = 'active' "
                "AND lower(i.email) = lower(?) LIMIT 1",
                (str(job_id), str(alias or "").strip()),
            ).fetchone()
        if row is None:
            return None
        return str(row["email"]), str(row["code_url"])

    def list_unavailable_aliases_for_code_url(self, code_url: str) -> set[str]:
        """Return aliases that cannot be assigned to a new registration batch.

        Historical batch rows are an audit trail, not a permanent reservation.
        An alias only remains unavailable after it was consumed, explicitly
        failed, or is still owned by an active assignment. QAN8 uses a second
        set of tables in the same database, so its terminal/live aliases are
        part of the same mailbox ownership check.
        """
        value = str(code_url or "").strip()
        if not value:
            return set()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                f"SELECT DISTINCT i.email FROM {self._table_prefix()}_batch_items i "
                f"JOIN {self._table_prefix()}_batches b ON b.batch_id = i.batch_id "
                "WHERE i.code_url = ? AND ("
                "i.state IN ('exhausted', 'failed') "
                "OR i.completed_count >= b.capacity "
                f"OR EXISTS (SELECT 1 FROM {self._table_prefix()}_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active')"
                ")",
                (value,),
            ).fetchall()
            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            q8_rows = []
            if q8_tables == {'qan8_aliases', 'qan8_sources'}:
                q8_rows = connection.execute(
                    "SELECT DISTINCT x.alias FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ? AND x.state IN ('consumed', 'failed', 'active')",
                    (value,),
                ).fetchall()
        aliases: set[str] = set()
        for row in rows:
            if str(row["email"] or "").strip():
                aliases.add(str(row["email"] or "").strip().casefold())
        for row in q8_rows:
            if str(row["alias"] or "").strip():
                aliases.add(str(row["alias"] or "").strip().casefold())
        return aliases

    def list_globally_unavailable_aliases(self) -> set[str]:
        """Return aliases already owned, consumed, failed, or actively reserved."""
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            return self._globally_unavailable_aliases_in_connection(connection)

    def list_allocated_aliases_for_code_url(self, code_url: str) -> set[str]:
        """Return every alias already owned for one exact provider URL.

        Unlike ``list_unavailable_aliases_for_code_url``, this includes active
        but currently unassigned rows.  A new batch must not create a second
        row for such an alias; workers can reuse the existing row through the
        shared claim path instead.
        """
        value = str(code_url or "").strip()
        if not value:
            return set()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT DISTINCT lower(email) AS email FROM gmail_api_url_batch_items "
                "WHERE code_url = ?",
                (value,),
            ).fetchall()
            aliases = {
                str(row["email"] or "").strip().casefold()
                for row in rows
                if str(row["email"] or "").strip()
            }
            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources"}:
                rows = connection.execute(
                    "SELECT DISTINCT lower(x.alias) AS alias FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ?",
                    (value,),
                ).fetchall()
                aliases.update(
                    str(row["alias"] or "").strip().casefold()
                    for row in rows
                    if str(row["alias"] or "").strip()
                )
        return aliases

    def has_pending_alias_for_code_url(self, code_url: str) -> bool:
        """Return whether an alias for one URL is still reusable or queued.

        A raw source may already be represented by another canonical batch (or
        by QAN8 provenance) while its aliases are waiting for a worker.  That
        is temporary/pending capacity, not source exhaustion; callers must not
        mark the raw root terminal in that state.
        """
        value = str(code_url or "").strip()
        if not value:
            return False
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            row = connection.execute(
                "SELECT 1 FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.code_url = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity LIMIT 1",
                (value,),
            ).fetchone()
            if row is not None:
                return True
            q8_tables = {
                str(item[0])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources"}:
                row = connection.execute(
                    "SELECT 1 FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ? AND s.state = 'active' "
                    "AND x.state IN ('available', 'active') LIMIT 1",
                    (value,),
                ).fetchone()
        return row is not None

    def has_active_code_url_assignment(self, code_url: str) -> bool:
        """Return whether any live job currently owns a mailbox URL."""
        value = str(code_url or "").strip()
        if not value:
            return False
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            row = connection.execute(
                "SELECT 1 FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "WHERE i.code_url = ? AND a.state = 'active' LIMIT 1",
                (value,),
            ).fetchone()
            if row is not None:
                return True
            q8_tables = {
                str(item[0])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources', 'qan8_assignments')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources", "qan8_assignments"}:
                row = connection.execute(
                    "SELECT 1 FROM qan8_assignments a "
                    "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.code_url = ? AND a.state = 'active' LIMIT 1",
                    (value,),
                ).fetchone()
        return row is not None

    def has_alias_for_other_code_url(self, alias: str, code_url: str) -> bool:
        """Return whether an alias mailbox root is tied to another URL."""
        email = str(alias or "").strip().casefold()
        url = str(code_url or "").strip()
        if not email or not url:
            return False
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            return self._alias_owned_by_other_code_url_in_connection(
                connection, email, url
            )

    @staticmethod
    def _alias_owned_by_other_code_url_in_connection(
        connection: sqlite3.Connection,
        alias: str,
        code_url: str,
    ) -> bool:
        """Check exact and canonical Gmail-root ownership in one transaction."""
        from .gmail_aliases import GmailAliasError, canonical_gmail

        email = str(alias or "").strip().casefold()
        url = str(code_url or "").strip()
        if not email or not url:
            return False
        try:
            canonical = canonical_gmail(email)
        except GmailAliasError:
            canonical = ""
        rows = connection.execute(
            "SELECT email, code_url FROM gmail_api_url_batch_items"
        ).fetchall()
        for row in rows:
            owner_url = str(row["code_url"] or "").strip()
            if owner_url == url:
                continue
            owner_email = str(row["email"] or "").strip().casefold()
            if owner_email == email:
                return True
            if canonical:
                try:
                    if canonical_gmail(owner_email) == canonical:
                        return True
                except GmailAliasError:
                    continue

        q8_tables = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('qan8_aliases', 'qan8_sources')"
            ).fetchall()
        }
        if q8_tables != {"qan8_aliases", "qan8_sources"}:
            return False
        rows = connection.execute(
            "SELECT x.alias, s.code_url FROM qan8_aliases x "
            "JOIN qan8_sources s ON s.source_group_id = x.source_group_id"
        ).fetchall()
        for row in rows:
            owner_url = str(row["code_url"] or "").strip()
            if owner_url == url:
                continue
            owner_email = str(row["alias"] or "").strip().casefold()
            if owner_email == email:
                return True
            if canonical:
                try:
                    if canonical_gmail(owner_email) == canonical:
                        return True
                except GmailAliasError:
                    continue
        return False

    def list_batch_ids_for_code_urls(self, code_urls: set[str]) -> list[str]:
        """List batches that still record any of the provider URLs."""
        normalized = sorted({
            str(code_url or "").strip()
            for code_url in (code_urls or set())
            if str(code_url or "").strip()
        })
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT DISTINCT batch_id FROM gmail_api_url_batch_items "
                f"WHERE code_url IN ({placeholders}) ORDER BY batch_id",
                tuple(normalized),
            ).fetchall()
        return [str(row["batch_id"]) for row in rows]

    def alias_usage_for_code_urls(
        self, code_urls: set[str]
    ) -> dict[str, dict[str, set[str]]]:
        """Return allocated, consumed, failed, and reserved aliases by code URL."""
        normalized = {
            str(code_url or "").strip()
            for code_url in (code_urls or set())
            if str(code_url or "").strip()
        }
        usage = {
            code_url: {
                "allocated": set(),
                "consumed": set(),
                "failed": set(),
                "reserved": set(),
            }
            for code_url in normalized
        }
        if not normalized:
            return usage

        normalized_urls = sorted(normalized)
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = []
            for start in range(0, len(normalized_urls), 500):
                url_chunk = normalized_urls[start : start + 500]
                placeholders = ",".join("?" for _ in url_chunk)
                rows.extend(
                    connection.execute(
                        "SELECT i.code_url, i.email, i.state, i.completed_count, "
                        "i.failure_reason, b.capacity, "
                        "EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
                        "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                        "AND a.state = 'active') AS is_reserved "
                        "FROM gmail_api_url_batch_items i "
                        "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                        f"WHERE i.code_url IN ({placeholders})",
                        tuple(url_chunk),
                    ).fetchall()
                )

        for row in rows:
            code_url = str(row["code_url"] or "").strip()
            alias = str(row["email"] or "").strip().casefold()
            if not alias or code_url not in usage:
                continue
            usage[code_url]["allocated"].add(alias)
            failed = (
                str(row["state"] or "") == "failed"
                or (
                    str(row["state"] or "") == "exhausted"
                    and bool(str(row["failure_reason"] or "").strip())
                )
            )
            consumed = (
                str(row["state"] or "") == "exhausted"
                or int(row["completed_count"] or 0) >= int(row["capacity"] or 1)
            )
            if failed:
                usage[code_url]["failed"].add(alias)
            elif consumed:
                usage[code_url]["consumed"].add(alias)
            elif bool(row["is_reserved"]):
                usage[code_url]["reserved"].add(alias)
        return usage

    def alias_root_owners(self) -> dict[str, set[str]]:
        """Return every canonical Gmail root and the URLs that own it.

        The raw pool can contain a dotted/plus source that is a second spelling
        of a root already allocated by another provider URL.  Capacity reports
        must hide that root even when the exact candidate alias is not present
        in the current URL's item rows.
        """
        from .gmail_aliases import GmailAliasError, canonical_gmail

        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            rows = connection.execute(
                "SELECT email, code_url FROM gmail_api_url_batch_items"
            ).fetchall()
            q8_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('qan8_aliases', 'qan8_sources')"
                ).fetchall()
            }
            if q8_tables == {"qan8_aliases", "qan8_sources"}:
                rows.extend(
                    connection.execute(
                        "SELECT x.alias AS email, s.code_url "
                        "FROM qan8_aliases x "
                        "JOIN qan8_sources s ON s.source_group_id = x.source_group_id"
                    ).fetchall()
                )

        owners: dict[str, set[str]] = {}
        for row in rows:
            email = str(row["email"] or "").strip().casefold()
            code_url = str(row["code_url"] or "").strip()
            if not email or not code_url:
                continue
            try:
                root = canonical_gmail(email)
            except GmailAliasError:
                root = email
            owners.setdefault(root, set()).add(code_url)
        return owners

    def reset_unused_aliases_for_code_url(self, code_url: str) -> int:
        """Remove unconsumed aliases that are not owned by a live or queued job."""
        value = str(code_url or "").strip()
        if not value:
            return 0

        with self._transaction() as connection:
            active_assignments = connection.execute(
                "SELECT COUNT(*) FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.code_url = ? AND i.state = 'active' "
                "AND i.completed_count < b.capacity AND a.state = 'active'",
                (value,),
            ).fetchone()[0]
            if active_assignments:
                raise GmailBatchConflict(
                    "Gmail API URL alias đang được job chạy sử dụng"
                )

            waiting_jobs = connection.execute(
                "SELECT COUNT(*) FROM gmail_api_url_waiters w "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = w.batch_id "
                "WHERE i.code_url = ? AND w.state = 'waiting'",
                (value,),
            ).fetchone()[0]
            if waiting_jobs:
                raise GmailBatchConflict(
                    "Gmail API URL alias đang có job chờ trong hàng đợi"
                )

            cursor = connection.execute(
                "DELETE FROM gmail_api_url_batch_items "
                "WHERE code_url = ? AND state = 'active' AND completed_count = 0 "
                "AND NOT EXISTS ("
                "SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = gmail_api_url_batch_items.batch_id "
                "AND a.inventory_id = gmail_api_url_batch_items.inventory_id "
                "AND a.state = 'active'"
                ")",
                (value,),
            )
            return max(0, int(cursor.rowcount or 0))

    def has_pending_items(self, batch_id: str) -> bool:
        """Return whether the batch still has an alias that is not consumed."""
        return bool(self.batch_status(batch_id)["pending"])

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
        blocked_roots = self.runtime_blocked_canonical_roots()
        with closing(self._connect()) as connection:
            connection.executescript(self._get_schema_sql())
            item_rows = connection.execute(
                "SELECT i.*, b.capacity, EXISTS ("
                "SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active'"
                ") AS is_reserved FROM gmail_api_url_batch_items i "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE i.batch_id = ?",
                (batch,),
            ).fetchall()
            active_urls = {
                str(item["code_url"] or "").strip()
                for item in connection.execute(
                    "SELECT DISTINCT i.code_url "
                    "FROM gmail_api_url_assignments a "
                    "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                    "AND i.inventory_id = a.inventory_id "
                    "WHERE a.state = 'active'"
                ).fetchall()
            }
            unavailable_aliases = self._globally_unavailable_aliases_in_connection(connection)
            shadow_items = self._shadow_aliases_in_connection(connection)
            total = pending = completed = exhausted = 0
            available_urls: set[str] = set()
            for item in item_rows:
                item_key = (str(item["batch_id"]), str(item["inventory_id"]))
                if item_key in shadow_items:
                    continue
                capacity = int(item["capacity"] or 1)
                used = int(item["completed_count"] or 0)
                remaining = max(0, capacity - used)
                total += capacity
                completed += used
                state = str(item["state"] or "").strip().lower()
                if state in {"failed", "exhausted"} or used >= capacity:
                    exhausted += remaining if state == "failed" else capacity
                    continue
                if state != "active" or not remaining:
                    continue
                blocked = (
                    self._runtime_alias_is_unavailable(
                        item["email"], unavailable_aliases
                    )
                    or self._alias_is_runtime_blocked(
                        str(item["email"] or ""), blocked_roots
                    )
                )
                if blocked and not bool(item["is_reserved"]):
                    exhausted += remaining
                    continue
                pending += remaining
                code_url = str(item["code_url"] or "").strip()
                if code_url and code_url not in active_urls:
                    available_urls.add(code_url)
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
            available_code_urls = len(available_urls)
        return {
            "total": total,
            "pending": pending,
            "completed": completed,
            "exhausted": exhausted,
            "active_assignments": int(active_assignments or 0),
            "waiting_jobs": int(waiting_jobs or 0),
            "available_code_urls": int(available_code_urls or 0),
            "exhausted_batch": pending == 0,
        }

    def exhaust(self, assignment_id: str, reason: str = "") -> bool:
        """Mark assignment and item as exhausted (API URL-specific)."""
        return self._finish(assignment_id, "exhausted", item_state="exhausted", reason=reason)

    def get_assignment(self, assignment_id: str) -> Assignment | None:
        """Get assignment by ID."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM gmail_api_url_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
        return self._assignment(row) if row else None

    def get_item(self, batch_id: str, inventory_id: str) -> GmailApiUrlBatchItem | None:
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
