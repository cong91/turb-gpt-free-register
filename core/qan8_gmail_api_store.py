"""Durable SQLite state for QAN8 lazy source lanes."""
from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from core.app_state_db import APP_STATE_DB_PATH, connect, ensure_schema

_LEASE_SECONDS = 120
_MAX_ALIASES_PER_SOURCE = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qan8_batches (
    batch_id TEXT PRIMARY KEY,
    target_count INTEGER NOT NULL,
    effective_workers INTEGER NOT NULL,
    aliases_per_source INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qan8_lanes (
    batch_id TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    current_source_group_id TEXT,
    next_sequence INTEGER NOT NULL DEFAULT 0,
    active_job_id TEXT,
    PRIMARY KEY (batch_id, lane_id),
    FOREIGN KEY (batch_id) REFERENCES qan8_batches(batch_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS qan8_orders (
    order_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    out_order_no TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    delivery_summary TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    source_group_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (batch_id, out_order_no),
    FOREIGN KEY (batch_id, lane_id) REFERENCES qan8_lanes(batch_id, lane_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS qan8_sources (
    source_group_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    source_email TEXT NOT NULL,
    code_url TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    completed_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    retired_at REAL,
    UNIQUE (batch_id, source_group_id),
    FOREIGN KEY (batch_id, lane_id) REFERENCES qan8_lanes(batch_id, lane_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qan8_active_code_url
    ON qan8_sources(batch_id, code_url) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_qan8_active_source_email
    ON qan8_sources(batch_id, source_email) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS qan8_aliases (
    alias_id TEXT PRIMARY KEY,
    source_group_id TEXT NOT NULL,
    alias TEXT NOT NULL UNIQUE,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'available',
    created_at REAL NOT NULL,
    FOREIGN KEY (source_group_id) REFERENCES qan8_sources(source_group_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS qan8_assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    job_id TEXT NOT NULL UNIQUE,
    alias_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (batch_id, lane_id) REFERENCES qan8_lanes(batch_id, lane_id) ON DELETE CASCADE,
    FOREIGN KEY (alias_id) REFERENCES qan8_aliases(alias_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qan8_active_lane_assignment
    ON qan8_assignments(batch_id, lane_id) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS qan8_leases (
    batch_id TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    lease_kind TEXT NOT NULL,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (batch_id, lane_id, lease_kind),
    FOREIGN KEY (batch_id, lane_id) REFERENCES qan8_lanes(batch_id, lane_id) ON DELETE CASCADE
);
"""


class Qan8GmailApiStore:
    """Own QAN8 state transitions while leaving HTTP to the client module."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else APP_STATE_DB_PATH
        with closing(self._connection()) as connection:
            connection.executescript(_SCHEMA)

    def create_batch(
        self,
        target_count: int,
        *,
        requested_workers: int,
        aliases_per_source: int,
    ) -> dict:
        target = int(target_count)
        workers = int(requested_workers)
        capacity = int(aliases_per_source)
        if target < 1:
            raise ValueError("QAN8 target_count must be positive")
        if workers < 1:
            raise ValueError("QAN8 requested_workers must be positive")
        if not 1 <= capacity <= _MAX_ALIASES_PER_SOURCE:
            raise ValueError("QAN8 aliases_per_source must be between 1 and 12")
        effective_workers = min(target, workers)
        batch_id = uuid.uuid4().hex
        now = time.time()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO qan8_batches "
                "(batch_id, target_count, effective_workers, aliases_per_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, target, effective_workers, capacity, now, now),
            )
            connection.executemany(
                "INSERT INTO qan8_lanes(batch_id, lane_id) VALUES (?, ?)",
                ((batch_id, lane_id) for lane_id in range(effective_workers)),
            )
        return self.get_batch(batch_id)  # type: ignore[return-value]

    def get_batch(self, batch_id: str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT * FROM qan8_batches WHERE batch_id = ?", (str(batch_id),)
            ).fetchone()
        return self._row(row)

    def list_lanes(self, batch_id: str) -> list[dict]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM qan8_lanes WHERE batch_id = ? ORDER BY lane_id",
                (str(batch_id),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_lane(self, batch_id: str, lane_id: int) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT * FROM qan8_lanes WHERE batch_id = ? AND lane_id = ?",
                (str(batch_id), int(lane_id)),
            ).fetchone()
        return self._row(row)

    def acquire_lane_lease(
        self,
        batch_id: str,
        lane_id: int,
        owner: str,
        *,
        lease_kind: str = "purchase",
        lease_seconds: int = _LEASE_SECONDS,
    ) -> bool:
        value = str(owner or "").strip()
        if not value:
            raise ValueError("QAN8 lease owner is required")
        now = time.time()
        expires = now + max(1, int(lease_seconds))
        with self._transaction() as connection:
            self._require_lane(connection, batch_id, lane_id)
            row = connection.execute(
                "SELECT owner, expires_at FROM qan8_leases "
                "WHERE batch_id = ? AND lane_id = ? AND lease_kind = ?",
                (str(batch_id), int(lane_id), str(lease_kind)),
            ).fetchone()
            if row and float(row["expires_at"]) > now and row["owner"] != value:
                return False
            connection.execute(
                "INSERT INTO qan8_leases(batch_id, lane_id, lease_kind, owner, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_id, lane_id, lease_kind) DO UPDATE SET "
                "owner = excluded.owner, expires_at = excluded.expires_at",
                (str(batch_id), int(lane_id), str(lease_kind), value, expires),
            )
        return True

    def release_lane_lease(
        self, batch_id: str, lane_id: int, owner: str, *, lease_kind: str = "purchase"
    ) -> bool:
        with self._transaction() as connection:
            result = connection.execute(
                "DELETE FROM qan8_leases WHERE batch_id = ? AND lane_id = ? "
                "AND lease_kind = ? AND owner = ?",
                (str(batch_id), int(lane_id), str(lease_kind), str(owner)),
            )
        return result.rowcount > 0

    def create_order_intent(
        self, batch_id: str, lane_id: int, out_order_no: str, sku_id: int | str
    ) -> dict:
        order_no = str(out_order_no or "").strip()
        if not order_no:
            raise ValueError("QAN8 out_order_no is required")
        now = time.time()
        order_id = uuid.uuid4().hex
        with self._transaction() as connection:
            self._require_lane(connection, batch_id, lane_id)
            connection.execute(
                "INSERT INTO qan8_orders "
                "(order_id, batch_id, lane_id, out_order_no, sku_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_id, out_order_no) DO NOTHING",
                (
                    order_id,
                    str(batch_id),
                    int(lane_id),
                    order_no,
                    str(sku_id),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM qan8_orders WHERE batch_id = ? AND out_order_no = ?",
                (str(batch_id), order_no),
            ).fetchone()
        return self._row(row)  # type: ignore[return-value]

    def get_order(self, batch_id: str, out_order_no: str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT * FROM qan8_orders WHERE batch_id = ? AND out_order_no = ?",
                (str(batch_id), str(out_order_no)),
            ).fetchone()
        return self._row(row)

    def list_orders(self, batch_id: str) -> list[dict]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM qan8_orders WHERE batch_id = ? ORDER BY created_at, order_id",
                (str(batch_id),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def update_order(
        self,
        batch_id: str,
        out_order_no: str,
        *,
        status: str,
        message: str = "",
        delivery_summary: str = "",
        source_group_id: str | None = None,
    ) -> dict | None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE qan8_orders SET status = ?, message = ?, delivery_summary = ?, "
                "source_group_id = COALESCE(?, source_group_id), updated_at = ? "
                "WHERE batch_id = ? AND out_order_no = ?",
                (
                    str(status),
                    str(message or "")[:200],
                    str(delivery_summary or "")[:200],
                    source_group_id,
                    time.time(),
                    str(batch_id),
                    str(out_order_no),
                ),
            )
            row = connection.execute(
                "SELECT * FROM qan8_orders WHERE batch_id = ? AND out_order_no = ?",
                (str(batch_id), str(out_order_no)),
            ).fetchone()
        return self._row(row)

    def create_source_group(
        self,
        batch_id: str,
        lane_id: int,
        source_email: str,
        code_url: str,
        aliases: Iterable[str],
    ) -> dict:
        email = str(source_email or "").strip().lower()
        url = str(code_url or "").strip()
        alias_values = [str(alias or "").strip().lower() for alias in aliases if str(alias or "").strip()]
        if not email or not url or not alias_values:
            raise ValueError("QAN8 source group requires email, code_url, and aliases")
        if len(set(alias_values)) != len(alias_values):
            raise ValueError("QAN8 source group aliases must be unique")
        now = time.time()
        source_group_id = uuid.uuid4().hex
        try:
            with self._transaction() as connection:
                self._require_lane(connection, batch_id, lane_id)
                connection.execute(
                    "INSERT INTO qan8_sources "
                    "(source_group_id, batch_id, lane_id, source_email, code_url, capacity, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        source_group_id,
                        str(batch_id),
                        int(lane_id),
                        email,
                        url,
                        len(alias_values),
                        now,
                    ),
                )
                connection.executemany(
                    "INSERT INTO qan8_aliases(alias_id, source_group_id, alias, ordinal, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        (uuid.uuid4().hex, source_group_id, alias, ordinal, now)
                        for ordinal, alias in enumerate(alias_values)
                    ),
                )
                connection.execute(
                    "UPDATE qan8_lanes SET current_source_group_id = ? WHERE batch_id = ? AND lane_id = ?",
                    (source_group_id, str(batch_id), int(lane_id)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("QAN8 source code_url is already active in this batch") from exc
        return self.get_source_group(source_group_id)  # type: ignore[return-value]

    def get_source_group(self, source_group_id: str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT * FROM qan8_sources WHERE source_group_id = ?",
                (str(source_group_id),),
            ).fetchone()
        return self._row(row)

    def get_current_source(self, batch_id: str, lane_id: int) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT s.* FROM qan8_lanes l JOIN qan8_sources s "
                "ON s.source_group_id = l.current_source_group_id "
                "WHERE l.batch_id = ? AND l.lane_id = ? AND s.state = 'active'",
                (str(batch_id), int(lane_id)),
            ).fetchone()
        return self._row(row)

    def list_source_aliases(self, source_group_id: str) -> list[dict]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM qan8_aliases WHERE source_group_id = ? ORDER BY ordinal",
                (str(source_group_id),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def claim_alias(self, batch_id: str, lane_id: int, job_id: int | str) -> dict | None:
        job = str(job_id)
        now = time.time()
        with self._transaction() as connection:
            self._require_lane(connection, batch_id, lane_id)
            existing = connection.execute(
                "SELECT a.*, x.alias, x.source_group_id FROM qan8_assignments a "
                "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                "WHERE a.batch_id = ? AND a.job_id = ? AND a.state = 'active'",
                (str(batch_id), job),
            ).fetchone()
            if existing:
                return self._row(existing)
            active_lane = connection.execute(
                "SELECT 1 FROM qan8_assignments WHERE batch_id = ? AND lane_id = ? AND state = 'active'",
                (str(batch_id), int(lane_id)),
            ).fetchone()
            if active_lane:
                return None
            row = connection.execute(
                "SELECT x.*, s.code_url FROM qan8_aliases x "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE s.batch_id = ? AND s.lane_id = ? AND s.state = 'active' "
                "AND x.state = 'available' ORDER BY x.ordinal LIMIT 1",
                (str(batch_id), int(lane_id)),
            ).fetchone()
            if row is None:
                return None
            assignment_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO qan8_assignments "
                "(assignment_id, batch_id, lane_id, job_id, alias_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (assignment_id, str(batch_id), int(lane_id), job, row["alias_id"], now, now),
            )
            connection.execute(
                "UPDATE qan8_aliases SET state = 'active' WHERE alias_id = ?",
                (row["alias_id"],),
            )
            connection.execute(
                "UPDATE qan8_lanes SET active_job_id = ? WHERE batch_id = ? AND lane_id = ?",
                (job, str(batch_id), int(lane_id)),
            )
            created = connection.execute(
                "SELECT a.*, x.alias, x.source_group_id, s.code_url FROM qan8_assignments a "
                "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE a.assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        return self._row(created)

    def complete_assignment(self, job_id: int | str) -> bool:
        return self._finish_assignment(job_id, "completed", alias_state="consumed")

    def get_assignment(self, job_id: int | str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT a.*, x.alias, x.source_group_id, s.code_url "
                "FROM qan8_assignments a "
                "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE a.job_id = ? ORDER BY a.updated_at DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
        return self._row(row)

    def get_active_assignment_for_alias(self, alias: str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT a.*, x.alias, x.source_group_id, s.code_url "
                "FROM qan8_assignments a "
                "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE x.alias = ? AND a.state = 'active' "
                "ORDER BY a.updated_at DESC LIMIT 1",
                (str(alias or "").strip().lower(),),
            ).fetchone()
        return self._row(row)

    def release_assignment(self, job_id: int | str, reason: str = "") -> bool:
        return self._finish_assignment(job_id, "released", alias_state="available", reason=reason)

    def fail_assignment(self, job_id: int | str, reason: str = "") -> bool:
        return self._finish_assignment(job_id, "failed", alias_state="failed", reason=reason)

    def retire_source(self, source_group_id: str, reason: str = "") -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT batch_id, lane_id, state FROM qan8_sources WHERE source_group_id = ?",
                (str(source_group_id),),
            ).fetchone()
            if row is None or row["state"] != "active":
                return False
            connection.execute(
                "UPDATE qan8_sources SET state = 'retired', retired_at = ? WHERE source_group_id = ?",
                (time.time(), str(source_group_id)),
            )
            connection.execute(
                "UPDATE qan8_lanes SET current_source_group_id = NULL WHERE batch_id = ? "
                "AND lane_id = ? AND current_source_group_id = ?",
                (row["batch_id"], row["lane_id"], str(source_group_id)),
            )
        return True

    def get_account_context(self, alias: str) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT x.alias, x.state AS alias_state, s.source_group_id, s.batch_id, "
                "s.lane_id, s.source_email, s.code_url, s.state AS source_state "
                "FROM qan8_aliases x JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE x.alias = ?",
                (str(alias or "").strip().lower(),),
            ).fetchone()
        return self._row(row)

    def batch_status(self, batch_id: str) -> dict:
        with closing(self._connection()) as connection:
            batch = connection.execute(
                "SELECT * FROM qan8_batches WHERE batch_id = ?", (str(batch_id),)
            ).fetchone()
            if batch is None:
                return {
                    "batch_id": str(batch_id),
                    "target_count": 0,
                    "effective_workers": 0,
                    "active_sources": 0,
                    "orders_placed": 0,
                    "lifetime_sources_purchased": 0,
                }
            active_sources = connection.execute(
                "SELECT COUNT(*) FROM qan8_sources WHERE batch_id = ? AND state = 'active'",
                (str(batch_id),),
            ).fetchone()[0]
            orders = connection.execute(
                "SELECT COUNT(*) FROM qan8_orders WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()[0]
            sources = connection.execute(
                "SELECT COUNT(*) FROM qan8_sources WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()[0]
            remaining = connection.execute(
                "SELECT COUNT(*) FROM qan8_aliases x JOIN qan8_sources s "
                "ON s.source_group_id = x.source_group_id WHERE s.batch_id = ? AND x.state = 'available'",
                (str(batch_id),),
            ).fetchone()[0]
        return {
            "batch_id": str(batch_id),
            "target_count": int(batch["target_count"]),
            "effective_workers": int(batch["effective_workers"]),
            "aliases_per_source": int(batch["aliases_per_source"]),
            "active_sources": int(active_sources),
            "orders_placed": int(orders),
            "lifetime_sources_purchased": int(sources),
            "remaining_aliases": int(remaining),
        }

    def reconcile(self) -> int:
        now = time.time()
        with self._transaction() as connection:
            result = connection.execute(
                "DELETE FROM qan8_leases WHERE expires_at <= ?", (now,)
            )
        return result.rowcount

    def _finish_assignment(
        self,
        job_id: int | str,
        state: str,
        *,
        alias_state: str,
        reason: str = "",
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT assignment_id, batch_id, lane_id, alias_id FROM qan8_assignments "
                "WHERE job_id = ? AND state = 'active'",
                (str(job_id),),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE qan8_assignments SET state = ?, reason = ?, updated_at = ? "
                "WHERE assignment_id = ?",
                (state, str(reason or "")[:300], time.time(), row["assignment_id"]),
            )
            connection.execute(
                "UPDATE qan8_aliases SET state = ? WHERE alias_id = ?",
                (alias_state, row["alias_id"]),
            )
            connection.execute(
                "UPDATE qan8_lanes SET active_job_id = NULL WHERE batch_id = ? AND lane_id = ? "
                "AND active_job_id = ?",
                (row["batch_id"], row["lane_id"], str(job_id)),
            )
            if state == "completed":
                connection.execute(
                    "UPDATE qan8_sources SET completed_count = completed_count + 1 "
                    "WHERE source_group_id = (SELECT source_group_id FROM qan8_aliases WHERE alias_id = ?)",
                    (row["alias_id"],),
                )
                source = connection.execute(
                    "SELECT s.source_group_id FROM qan8_sources s WHERE s.source_group_id = "
                    "(SELECT source_group_id FROM qan8_aliases WHERE alias_id = ?) "
                    "AND s.state = 'active' AND NOT EXISTS "
                    "(SELECT 1 FROM qan8_aliases x WHERE x.source_group_id = s.source_group_id "
                    "AND x.state IN ('available', 'active'))",
                    (row["alias_id"],),
                ).fetchone()
                if source:
                    connection.execute(
                        "UPDATE qan8_sources SET state = 'exhausted', retired_at = ? WHERE source_group_id = ?",
                        (time.time(), source["source_group_id"]),
                    )
                    connection.execute(
                        "UPDATE qan8_lanes SET current_source_group_id = NULL "
                        "WHERE batch_id = ? AND lane_id = ? AND current_source_group_id = ?",
                        (row["batch_id"], row["lane_id"], source["source_group_id"]),
                    )
        return True

    def _connection(self) -> sqlite3.Connection:
        connection = connect(self.path)
        ensure_schema(connection)
        connection.executescript(_SCHEMA)
        return connection

    def _transaction(self):
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        return _Transaction(connection)

    @staticmethod
    def _require_lane(connection: sqlite3.Connection, batch_id: str, lane_id: int) -> None:
        row = connection.execute(
            "SELECT 1 FROM qan8_lanes WHERE batch_id = ? AND lane_id = ?",
            (str(batch_id), int(lane_id)),
        ).fetchone()
        if row is None:
            raise ValueError("QAN8 lane does not exist")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
