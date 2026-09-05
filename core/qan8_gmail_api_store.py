"""Durable SQLite state for QAN8 lazy source lanes."""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Iterable
from contextlib import closing, contextmanager
from pathlib import Path

from core.app_state_db import APP_STATE_DB_PATH, connect, ensure_schema
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore, GmailBatchConflict

logger = logging.getLogger(__name__)

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
    state TEXT NOT NULL DEFAULT 'active',
    failure_reason TEXT NOT NULL DEFAULT '',
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
    gmail_batch_id TEXT,
    gmail_inventory_id TEXT,
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

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        initialize_schema: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else APP_STATE_DB_PATH
        if initialize_schema:
            with closing(self._connection()) as connection:
                connection.executescript(_SCHEMA)
            self._migrate_legacy_aliases_if_idle()

    def _migrate_legacy_aliases_if_idle(self) -> None:
        """Complete the one-time whole-store link before accepting new claims."""
        with closing(self._connection()) as connection:
            unlinked = connection.execute(
                "SELECT COUNT(*) FROM qan8_aliases "
                "WHERE (gmail_batch_id IS NULL OR gmail_inventory_id IS NULL) "
                "AND state != 'failed'"
            ).fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(*) FROM qan8_assignments WHERE state = 'active'"
            ).fetchone()[0]
        if not unlinked or active:
            return
        try:
            self.migrate_all_aliases_to_gmail_ledger()
        except RuntimeError as exc:
            logger.info("Deferring QAN8 Gmail ledger migration: %s", exc)

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
        # A worker lane owns one source at a time.  Do not create more
        # provider lanes than the requested jobs can fill with one source;
        # otherwise a small batch (for example six jobs with twelve aliases
        # per source) would purchase multiple sources before using capacity.
        source_count = (target + capacity - 1) // capacity
        effective_workers = min(workers, source_count)
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
        items: list[dict] = []
        for row in rows:
            item = self._row(row)
            if item is not None:
                items.append(item)
        return items

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
            self._require_active_lane(connection, batch_id, lane_id)
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
            self._require_active_lane(connection, batch_id, lane_id)
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
        items: list[dict] = []
        for row in rows:
            item = self._row(row)
            if item is not None:
                items.append(item)
        return items

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
        *,
        gmail_alias_refs: dict[str, tuple[str, str]] | None = None,
        gmail_batch_id: str | None = None,
    ) -> dict:
        email = str(source_email or "").strip().lower()
        url = str(code_url or "").strip()
        alias_values = [str(alias or "").strip().lower() for alias in aliases if str(alias or "").strip()]
        if not email or not url or not alias_values:
            raise ValueError("QAN8 source group requires email, code_url, and aliases")
        if len(set(alias_values)) != len(alias_values):
            raise ValueError("QAN8 source group aliases must be unique")
        refs = {
            str(alias or "").strip().lower(): (str(values[0]), str(values[1]))
            for alias, values in (gmail_alias_refs or {}).items()
            if str(alias or "").strip() and len(values) == 2
        }
        now = time.time()
        source_group_id = uuid.uuid4().hex
        try:
            with self._transaction() as connection:
                self._require_active_lane(connection, batch_id, lane_id)
                unresolved_aliases = [alias for alias in alias_values if alias not in refs]
                if unresolved_aliases:
                    gmail_store = GmailApiUrlBatchStore(self.path)
                    if gmail_batch_id:
                        refs.update(
                            gmail_store.append_source_group_in_connection(
                                connection,
                                str(gmail_batch_id),
                                email,
                                url,
                                unresolved_aliases,
                            )
                        )
                    else:
                        refs.update(
                            gmail_store.ensure_alias_items_in_connection(
                                connection,
                                url,
                                unresolved_aliases,
                            )
                        )
                duplicate_alias = connection.execute(
                    "SELECT 1 FROM qan8_aliases WHERE lower(alias) IN ("
                    + ",".join("?" for _ in alias_values)
                    + ") LIMIT 1",
                    tuple(alias_values),
                ).fetchone()
                if duplicate_alias is not None:
                    raise ValueError("QAN8 alias is already registered in the shared ledger")
                gmail_states: dict[str, str] = {}
                for alias in alias_values:
                    gmail_batch_id, gmail_inventory_id = refs[alias]
                    gmail_item = connection.execute(
                        "SELECT i.email, i.code_url, i.state, i.completed_count, "
                        "i.failure_reason, b.capacity "
                        "FROM gmail_api_url_batch_items i "
                        "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                        "WHERE i.batch_id = ? AND i.inventory_id = ?",
                        (gmail_batch_id, gmail_inventory_id),
                    ).fetchone()
                    if gmail_item is None:
                        raise RuntimeError("Gmail API alias reference does not exist")
                    # A caller-supplied reference is a durable ownership link,
                    # not merely an existence hint.  Validate both sides of
                    # that link before writing QAN8 provenance, otherwise a
                    # stale ref can route an alias to another mailbox/URL.
                    if (
                        str(gmail_item["email"] or "").strip().casefold() != alias
                        or str(gmail_item["code_url"] or "").strip() != url
                    ):
                        raise ValueError(
                            "QAN8 Gmail alias reference does not match alias/code_url"
                        )
                    is_failed = (
                        str(gmail_item["state"] or "") == "failed"
                        or (
                            str(gmail_item["state"] or "") == "exhausted"
                            and bool(str(gmail_item["failure_reason"] or "").strip())
                        )
                    )
                    is_consumed = (
                        str(gmail_item["state"] or "") == "exhausted"
                        or int(gmail_item["completed_count"] or 0)
                        >= int(gmail_item["capacity"] or 1)
                    )
                    gmail_states[alias] = (
                        "failed" if is_failed else "consumed" if is_consumed else "available"
                    )
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
                    "INSERT INTO qan8_aliases("
                    "alias_id, source_group_id, alias, ordinal, gmail_batch_id, "
                    "gmail_inventory_id, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            uuid.uuid4().hex,
                            source_group_id,
                            alias,
                            ordinal,
                            refs.get(alias, (None, None))[0],
                            refs.get(alias, (None, None))[1],
                            gmail_states[alias],
                            now,
                        )
                        for ordinal, alias in enumerate(alias_values)
                    ),
                )
                available_count = connection.execute(
                    "SELECT COUNT(*) FROM qan8_aliases WHERE source_group_id = ? "
                    "AND state = 'available'",
                    (source_group_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE qan8_sources SET completed_count = ("
                    "SELECT COUNT(*) FROM qan8_aliases WHERE source_group_id = ? "
                    "AND state = 'consumed'), state = CASE WHEN ? = 0 "
                    "THEN 'exhausted' ELSE 'active' END, retired_at = CASE WHEN ? = 0 "
                    "THEN ? ELSE NULL END WHERE source_group_id = ?",
                    (
                        source_group_id,
                        int(available_count),
                        int(available_count),
                        now,
                        source_group_id,
                    ),
                )
                connection.execute(
                    "UPDATE qan8_lanes SET current_source_group_id = ?, active_job_id = NULL, "
                    "failure_reason = '' WHERE batch_id = ? AND lane_id = ?",
                    (
                        source_group_id if available_count else None,
                        str(batch_id),
                        int(lane_id),
                    ),
                )
        except GmailBatchConflict as exc:
            raise ValueError("QAN8 alias is already registered in the shared ledger") from exc
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
                "WHERE l.batch_id = ? AND l.lane_id = ? AND l.state = 'active' "
                "AND s.state = 'active'",
                (str(batch_id), int(lane_id)),
            ).fetchone()
        return self._row(row)

    def list_source_aliases(self, source_group_id: str) -> list[dict]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM qan8_aliases WHERE source_group_id = ? ORDER BY ordinal",
                (str(source_group_id),),
            ).fetchall()
        items: list[dict] = []
        for row in rows:
            item = self._row(row)
            if item is not None:
                items.append(item)
        return items

    def alias_usage_for_source(self, source_email: str, code_url: str) -> dict | None:
        """Return alias state counts for one QAN8 source without exposing its code URL."""
        email = str(source_email or "").strip().lower()
        url = str(code_url or "").strip()
        if not email or not url or not self.path.is_file():
            return None
        try:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT x.state, COUNT(*) AS count FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.source_email = ? AND s.code_url = ? "
                    "GROUP BY x.state",
                    (email, url),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        if not rows:
            return None
        counts = {str(row["state"]): int(row["count"] or 0) for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "available": counts.get("available", 0),
            "used": counts.get("consumed", 0),
            "failed": counts.get("failed", 0),
            "reserved": counts.get("active", 0),
        }

    def alias_state_sets_for_source(
        self,
        source_email: str,
        code_url: str,
    ) -> dict[str, set[str]] | None:
        """Return QAN8 alias names grouped by state for cross-store accounting."""
        email = str(source_email or "").strip().lower()
        url = str(code_url or "").strip()
        if not email or not url or not self.path.is_file():
            return None
        try:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT x.alias, x.state FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "WHERE s.source_email = ? AND s.code_url = ?",
                    (email, url),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        if not rows:
            return None
        states: dict[str, set[str]] = {}
        for row in rows:
            alias = str(row["alias"] or "").strip().casefold()
            state = str(row["state"] or "").strip().lower()
            if alias and state:
                states.setdefault(state, set()).add(alias)
        return states

    def migrate_all_aliases_to_gmail_ledger(self) -> dict[str, int]:
        """Link every historical QAN8 alias to the canonical Gmail API ledger.

        This is deliberately a whole-store migration rather than a claim-time
        fallback. It refuses to run while a QAN8 alias or an overlapping Gmail
        alias belongs to a running registration job, so the transaction never
        overwrites live ownership.
        """
        migration_reason = "QAN8 Gmail ledger migration: historical terminal alias"
        failed_reason = "QAN8 Gmail ledger migration: historical failed alias"
        with self._transaction() as connection:
            active_qan8 = connection.execute(
                "SELECT COUNT(*) FROM qan8_assignments WHERE state = 'active'"
            ).fetchone()[0]
            if active_qan8:
                raise RuntimeError(
                    "Cannot migrate QAN8 aliases while a QAN8 assignment is active"
                )

            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'registration_jobs'"
                ).fetchall()
            }
            # A custom/isolated runtime store may not have registration_jobs,
            # but an active canonical assignment still owns the mailbox. When
            # job state exists, only live/unknown jobs block migration; a
            # completed historical job remains eligible for reconciliation.
            overlap_query = (
                "SELECT DISTINCT a.job_id, r.status AS registration_status "
                "FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "JOIN qan8_aliases x ON lower(x.alias) = lower(i.email) "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "AND s.code_url = i.code_url "
                "LEFT JOIN registration_jobs r ON CAST(r.id AS TEXT) = a.job_id "
                "WHERE a.state = 'active' "
                "AND (x.gmail_batch_id IS NULL OR x.gmail_inventory_id IS NULL) "
                "AND x.state NOT IN ('failed', 'consumed')"
                if tables
                else
                "SELECT DISTINCT a.job_id, NULL AS registration_status "
                "FROM gmail_api_url_assignments a "
                "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                "AND i.inventory_id = a.inventory_id "
                "JOIN qan8_aliases x ON lower(x.alias) = lower(i.email) "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "AND s.code_url = i.code_url "
                "WHERE a.state = 'active' "
                "AND (x.gmail_batch_id IS NULL OR x.gmail_inventory_id IS NULL) "
                "AND x.state NOT IN ('failed', 'consumed')"
            )
            overlap_rows = connection.execute(overlap_query).fetchall()
            live_overlap = [
                row
                for row in overlap_rows
                if not tables
                or str(row["registration_status"] or "").strip().lower()
                in {"", "pending", "running", "stopping"}
            ]
            if live_overlap:
                raise RuntimeError(
                    "Cannot migrate QAN8 aliases while an overlapping Gmail assignment is running"
                )

            rows = connection.execute(
                "SELECT x.alias_id, x.alias, x.state, s.code_url "
                "FROM qan8_aliases x JOIN qan8_sources s "
                "ON s.source_group_id = x.source_group_id "
                "ORDER BY s.created_at, x.ordinal, x.alias_id"
            ).fetchall()
            gmail_store = GmailApiUrlBatchStore(self.path)
            stats = {
                "aliases": len(rows),
                "linked": 0,
                "created_gmail_items": 0,
                "released_stale_assignments": 0,
                "terminalized_assignments": 0,
                "state_updates": 0,
                "exhausted_sources": 0,
                "conflicted_aliases": 0,
            }
            # Old Gmail-only batches could leave assignments active after their
            # registration job had already ended. Reconcile those rows first;
            # QAN8-overlapping rows are handled below with their QAN8 state.
            if tables:
                stale_gmail_assignments = connection.execute(
                    "SELECT a.assignment_id, a.job_id "
                    "FROM gmail_api_url_assignments a "
                    "LEFT JOIN registration_jobs r ON CAST(r.id AS TEXT) = a.job_id "
                "WHERE a.state = 'active' AND COALESCE(r.status, '') NOT IN "
                "('pending', 'running', 'stopping') "
                    "AND NOT EXISTS (SELECT 1 FROM gmail_api_url_batch_items i "
                    "JOIN qan8_aliases x ON lower(x.alias) = lower(i.email) "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "AND s.code_url = i.code_url "
                    "WHERE i.batch_id = a.batch_id AND i.inventory_id = a.inventory_id)"
                ).fetchall()
                for assignment in stale_gmail_assignments:
                    job_status = connection.execute(
                        "SELECT status FROM registration_jobs WHERE CAST(id AS TEXT) = ?",
                        (str(assignment["job_id"]),),
                    ).fetchone()
                    status = str(job_status["status"] if job_status else "").strip().lower()
                    if status == "success":
                        changed = gmail_store.finish_assignment_in_connection(
                            connection,
                            str(assignment["assignment_id"]),
                            "completed",
                            reason="Gmail ledger migration: completed historical job",
                        )
                    elif status == "failed":
                        changed = gmail_store.discard_assignment_in_connection(
                            connection,
                            str(assignment["assignment_id"]),
                            reason=failed_reason,
                        )
                    else:
                        changed = gmail_store.finish_assignment_in_connection(
                            connection,
                            str(assignment["assignment_id"]),
                            "released",
                            reason="Gmail ledger migration: stale assignment",
                        )
                    if changed:
                        if status in {"success", "failed"}:
                            stats["terminalized_assignments"] += 1
                        else:
                            stats["released_stale_assignments"] += 1
            for row in rows:
                alias = str(row["alias"] or "").strip().casefold()
                code_url = str(row["code_url"] or "").strip()
                previous_state = str(row["state"] or "").strip().lower()
                ledger = gmail_store.alias_ledger_in_connection(
                    connection,
                    code_url,
                    alias,
                )
                items = ledger["items"]
                active_assignments = ledger["active_assignments"]
                if not items:
                    stats["created_gmail_items"] += 1

                terminal_items = [
                    item
                    for item in items
                    if str(item["state"] or "") != "active"
                    or int(item["completed_count"] or 0) >= int(item["capacity"] or 1)
                ]
                active_job_statuses: list[str] = []
                if active_assignments and tables:
                    for assignment in active_assignments:
                        job_row = connection.execute(
                            "SELECT status FROM registration_jobs "
                            "WHERE CAST(id AS TEXT) = ?",
                            (str(assignment["job_id"]),),
                        ).fetchone()
                        active_job_statuses.append(
                            str(job_row["status"] if job_row else "").strip().lower()
                        )
                failed_terminal = any(
                    str(item["state"] or "") == "failed"
                    or (
                        str(item["state"] or "") == "exhausted"
                        and bool(str(item["failure_reason"] or "").strip())
                    )
                    for item in terminal_items
                )
                if previous_state == "failed" or failed_terminal:
                    target_state = "failed"
                elif "success" in active_job_statuses or previous_state == "consumed" or terminal_items:
                    target_state = "consumed"
                elif "failed" in active_job_statuses:
                    target_state = "failed"
                else:
                    target_state = "available"

                try:
                    if target_state == "available":
                        for assignment in active_assignments:
                            if gmail_store.finish_assignment_in_connection(
                                connection,
                                str(assignment["assignment_id"]),
                                "released",
                                reason="QAN8 Gmail ledger migration: stale assignment",
                            ):
                                stats["released_stale_assignments"] += 1
                        refs = gmail_store.ensure_alias_items_in_connection(
                            connection,
                            code_url,
                            [alias],
                        )
                    else:
                        for assignment in active_assignments:
                            if target_state == "failed":
                                changed = gmail_store.discard_assignment_in_connection(
                                    connection,
                                    str(assignment["assignment_id"]),
                                    reason=failed_reason,
                                )
                            else:
                                changed = gmail_store.finish_assignment_in_connection(
                                    connection,
                                    str(assignment["assignment_id"]),
                                    "completed",
                                    reason=migration_reason,
                                )
                            if changed:
                                stats["terminalized_assignments"] += 1
                        refs = gmail_store.exhaust_alias_items_in_connection(
                            connection,
                            code_url,
                            alias,
                            failure_reason=failed_reason if target_state == "failed" else "",
                        )
                except GmailBatchConflict as exc:
                    conflict_reason = (
                        "QAN8 Gmail ledger migration: alias conflicts with an existing "
                        "canonical Gmail root owned by another code_url"
                    )
                    logger.warning(
                        "%s for alias %s: %s",
                        conflict_reason,
                        alias,
                        exc,
                    )
                    connection.execute(
                        "UPDATE qan8_aliases SET gmail_batch_id = NULL, "
                        "gmail_inventory_id = NULL, state = 'failed' WHERE alias_id = ?",
                        (row["alias_id"],),
                    )
                    if previous_state != "failed":
                        stats["state_updates"] += 1
                    stats["conflicted_aliases"] += 1
                    continue

                gmail_batch_id, gmail_inventory_id = refs[alias]
                if previous_state != target_state:
                    stats["state_updates"] += 1
                connection.execute(
                    "UPDATE qan8_aliases SET gmail_batch_id = ?, gmail_inventory_id = ?, "
                    "state = ? WHERE alias_id = ?",
                    (gmail_batch_id, gmail_inventory_id, target_state, row["alias_id"]),
                )
                stats["linked"] += 1

            connection.execute(
                "UPDATE qan8_sources SET completed_count = ("
                "SELECT COUNT(*) FROM qan8_aliases x "
                "WHERE x.source_group_id = qan8_sources.source_group_id "
                "AND x.state = 'consumed')"
            )
            exhausted_sources = connection.execute(
                "SELECT source_group_id FROM qan8_sources s WHERE s.state = 'active' "
                "AND NOT EXISTS (SELECT 1 FROM qan8_aliases x "
                "WHERE x.source_group_id = s.source_group_id "
                "AND x.state IN ('available', 'active'))"
            ).fetchall()
            if exhausted_sources:
                now = time.time()
                source_ids = [str(row["source_group_id"]) for row in exhausted_sources]
                placeholders = ",".join("?" for _ in source_ids)
                connection.execute(
                    "UPDATE qan8_sources SET state = 'exhausted', retired_at = ? "
                    f"WHERE source_group_id IN ({placeholders})",
                    (now, *source_ids),
                )
                connection.execute(
                    "UPDATE qan8_lanes SET current_source_group_id = NULL, active_job_id = NULL "
                    f"WHERE current_source_group_id IN ({placeholders})",
                    tuple(source_ids),
                )
                stats["exhausted_sources"] = len(source_ids)

            invalid_refs = connection.execute(
                "SELECT COUNT(*) FROM qan8_aliases x "
                "LEFT JOIN gmail_api_url_batch_items i ON i.batch_id = x.gmail_batch_id "
                "AND i.inventory_id = x.gmail_inventory_id "
                "WHERE (x.gmail_batch_id IS NULL OR x.gmail_inventory_id IS NULL "
                "OR i.inventory_id IS NULL) AND x.state != 'failed'"
            ).fetchone()[0]
            if invalid_refs:
                raise RuntimeError("QAN8 Gmail ledger migration produced invalid alias references")
        return stats

    def claim_alias(self, batch_id: str, lane_id: int, job_id: int | str) -> dict | None:
        job = str(job_id)
        now = time.time()
        from core import db

        with self._runtime_transaction() as connection:
            blocked_roots = db.gmail_api_url_blocked_canonical_roots(sqlite_path=self.path)
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
            blocked_aliases = self._blocked_gmail_api_url_aliases(
                connection,
                batch_id,
                lane_id,
                blocked_roots=blocked_roots,
            )
            row = connection.execute(
                "SELECT x.*, s.code_url FROM qan8_aliases x "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "JOIN qan8_lanes l ON l.batch_id = s.batch_id AND l.lane_id = s.lane_id "
                "WHERE s.batch_id = ? AND s.lane_id = ? AND l.state = 'active' "
                "AND l.current_source_group_id = s.source_group_id "
                "AND s.state = 'active' "
                "AND x.state = 'available' ORDER BY x.ordinal LIMIT 1",
                (str(batch_id), int(lane_id)),
            ).fetchone()
            if row is not None and str(row["alias"] or "").strip().casefold() in blocked_aliases:
                row = None
                available_rows = connection.execute(
                    "SELECT x.*, s.code_url FROM qan8_aliases x "
                    "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                    "JOIN qan8_lanes l ON l.batch_id = s.batch_id AND l.lane_id = s.lane_id "
                    "WHERE s.batch_id = ? AND s.lane_id = ? AND l.state = 'active' "
                    "AND l.current_source_group_id = s.source_group_id "
                    "AND s.state = 'active' AND x.state = 'available' "
                    "ORDER BY x.ordinal",
                    (str(batch_id), int(lane_id)),
                ).fetchall()
                for candidate in available_rows:
                    if str(candidate["alias"] or "").strip().casefold() not in blocked_aliases:
                        row = candidate
                        break
            if row is None:
                current_source = connection.execute(
                    "SELECT current_source_group_id FROM qan8_lanes "
                    "WHERE batch_id = ? AND lane_id = ? AND state = 'active'",
                    (str(batch_id), int(lane_id)),
                ).fetchone()
                source_group_id = (
                    current_source["current_source_group_id"]
                    if current_source is not None
                    else None
                )
                if source_group_id:
                    remaining_rows = connection.execute(
                        "SELECT alias FROM qan8_aliases WHERE source_group_id = ? "
                        "AND state IN ('available', 'active')",
                        (source_group_id,),
                    ).fetchall()
                    remaining_alias = next(
                        (
                            candidate
                            for candidate in remaining_rows
                            if str(candidate["alias"] or "").strip().casefold()
                            not in blocked_aliases
                        ),
                        None,
                    )
                    if remaining_alias is None:
                        connection.execute(
                            "UPDATE qan8_sources SET state = 'exhausted', retired_at = ? "
                            "WHERE source_group_id = ? AND state = 'active'",
                            (now, source_group_id),
                        )
                        connection.execute(
                            "UPDATE qan8_lanes SET current_source_group_id = NULL, active_job_id = NULL "
                            "WHERE batch_id = ? AND lane_id = ? AND current_source_group_id = ?",
                            (str(batch_id), int(lane_id), source_group_id),
                        )
                return None
            gmail_assignment = None
            gmail_batch_id = row["gmail_batch_id"]
            gmail_inventory_id = row["gmail_inventory_id"]
            gmail_store = GmailApiUrlBatchStore(self.path)
            if not gmail_batch_id or not gmail_inventory_id:
                raise RuntimeError("QAN8 alias is not linked to the Gmail API ledger")
            gmail_assignment = gmail_store.claim_alias_in_connection(
                connection,
                str(gmail_batch_id),
                str(gmail_inventory_id),
                job,
            )
            if gmail_assignment is None:
                return None
            assignment_id = (
                gmail_assignment.assignment_id
                if gmail_assignment is not None
                else uuid.uuid4().hex
            )
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

    @staticmethod
    def _blocked_gmail_api_url_aliases(
        connection: sqlite3.Connection,
        batch_id: str,
        lane_id: int,
        *,
        blocked_roots: set[str] | None = None,
    ) -> set[str]:
        """Return Gmail-batch aliases unavailable to this QAN8 lane.

        QAN8 unit databases do not necessarily contain the Gmail batch schema;
        the table check keeps those isolated stores independent while the
        canonical application DB gets one cross-provider ownership rule.
        """
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('gmail_api_url_batch_items', 'gmail_api_url_batches', "
                "'gmail_api_url_assignments')"
            ).fetchall()
        }
        required = {
            "gmail_api_url_batch_items",
            "gmail_api_url_batches",
            "gmail_api_url_assignments",
        }
        blocked: set[str] = set()
        if tables == required:
            shadow_items = GmailApiUrlBatchStore._shadow_aliases_in_connection(
                connection
            )
            rows = connection.execute(
                "SELECT DISTINCT i.email FROM qan8_aliases x "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "JOIN gmail_api_url_batch_items i ON i.code_url = s.code_url "
                "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                "WHERE s.batch_id = ? AND s.lane_id = ? AND ("
                "i.state IN ('exhausted', 'failed') "
                "OR i.completed_count >= b.capacity "
                "OR EXISTS (SELECT 1 FROM gmail_api_url_assignments a "
                "WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id "
                "AND a.state = 'active')"
                ") AND lower(i.email) = lower(x.alias)",
                (str(batch_id), int(lane_id)),
            ).fetchall()
            blocked.update(
                str(row["email"] or "").strip().casefold()
                for row in rows
                if str(row["email"] or "").strip()
            )
            shadow_rows = connection.execute(
                "SELECT x.alias, x.gmail_batch_id, x.gmail_inventory_id "
                "FROM qan8_aliases x "
                "JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE s.batch_id = ? AND s.lane_id = ?",
                (str(batch_id), int(lane_id)),
            ).fetchall()
            blocked.update(
                str(row["alias"] or "").strip().casefold()
                for row in shadow_rows
                if (
                    str(row["alias"] or "").strip()
                    and (
                        str(row["gmail_batch_id"] or ""),
                        str(row["gmail_inventory_id"] or ""),
                    ) in shadow_items
                )
            )
        if blocked_roots:
            aliases = connection.execute(
                "SELECT alias FROM qan8_aliases WHERE source_group_id IN ("
                "SELECT source_group_id FROM qan8_sources WHERE batch_id = ? AND lane_id = ?"
                ")",
                (str(batch_id), int(lane_id)),
            ).fetchall()
            from core.gmail_aliases import GmailAliasError, canonical_gmail

            normalized_roots = {
                str(root or "").strip().casefold()
                for root in blocked_roots
                if str(root or "").strip()
            }
            for alias_row in aliases:
                alias = str(alias_row["alias"] or "").strip()
                if not alias:
                    continue
                try:
                    if canonical_gmail(alias) in normalized_roots:
                        blocked.add(alias.casefold())
                except GmailAliasError:
                    continue
        return blocked

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

    def quarantine_lane(self, batch_id: str, lane_id: int, reason: str = "") -> int:
        """Retire the current source after code 602 while keeping the lane usable."""
        batch = str(batch_id or "").strip()
        lane = int(lane_id)
        message = str(reason or "")[:300]
        now = time.time()
        with self._transaction() as connection:
            lane_row = connection.execute(
                "SELECT current_source_group_id FROM qan8_lanes "
                "WHERE batch_id = ? AND lane_id = ? AND state = 'active'",
                (batch, lane),
            ).fetchone()
            if lane_row is None or not lane_row["current_source_group_id"]:
                return 0
            source_group_id = str(lane_row["current_source_group_id"])
            source_row = connection.execute(
                "SELECT code_url FROM qan8_sources WHERE source_group_id = ?",
                (source_group_id,),
            ).fetchone()
            if source_row is not None:
                GmailApiUrlBatchStore(self.path).quarantine_code_url_in_connection(
                    connection,
                    str(source_row["code_url"]),
                    reason=message,
                )
            code_url = str(source_row["code_url"] if source_row else "")
            source_rows = connection.execute(
                "SELECT source_group_id, batch_id, lane_id FROM qan8_sources "
                "WHERE code_url = ? AND state = 'active'",
                (code_url,),
            ).fetchall()
            assignment_count = 0
            for source in source_rows:
                source_id = str(source["source_group_id"])
                assignment_count += connection.execute(
                    "SELECT COUNT(*) FROM qan8_assignments WHERE state = 'active' "
                    "AND alias_id IN (SELECT alias_id FROM qan8_aliases WHERE source_group_id = ?)",
                    (source_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE qan8_assignments SET state = 'failed', reason = ?, updated_at = ? "
                    "WHERE state = 'active' AND alias_id IN ("
                    "SELECT alias_id FROM qan8_aliases WHERE source_group_id = ?)",
                    (message, now, source_id),
                )
                connection.execute(
                    "UPDATE qan8_aliases SET state = 'failed' WHERE source_group_id = ? "
                    "AND state IN ('available', 'active')",
                    (source_id,),
                )
                connection.execute(
                    "UPDATE qan8_sources SET state = 'retired', retired_at = ? "
                    "WHERE source_group_id = ? AND state = 'active'",
                    (now, source_id),
                )
                connection.execute(
                    "UPDATE qan8_lanes SET current_source_group_id = NULL, active_job_id = NULL, "
                    "failure_reason = ? WHERE batch_id = ? AND lane_id = ?",
                    (message, source["batch_id"], source["lane_id"]),
                )
        return int(assignment_count or 0)

    def get_account_context(self, alias: str, *, job_id: int | str | None = None) -> dict | None:
        with closing(self._connection()) as connection:
            params: tuple[object, ...]
            query = (
                "SELECT x.alias, x.state AS alias_state, s.source_group_id, s.batch_id, "
                "s.lane_id, s.source_email, s.code_url, s.state AS source_state "
                "FROM qan8_aliases x JOIN qan8_sources s ON s.source_group_id = x.source_group_id "
                "WHERE x.alias = ?"
            )
            params = (str(alias or "").strip().lower(),)
            if job_id is not None:
                query += (
                    " AND EXISTS (SELECT 1 FROM qan8_assignments a "
                    "WHERE a.alias_id = x.alias_id AND a.job_id = ? "
                    "AND a.state = 'active')"
                )
                params += (str(job_id),)
            query += " ORDER BY CASE WHEN s.state = 'active' THEN 0 ELSE 1 END, s.created_at DESC LIMIT 1"
            row = connection.execute(query, params).fetchone()
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
                    "remaining_aliases": 0,
                    "pending_aliases": 0,
                    "shared_remaining_aliases": 0,
                    "shared_pending_aliases": 0,
                    "shared_active_assignments": 0,
                }
            canonical_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('gmail_api_url_batches', 'gmail_api_url_batch_items', "
                    "'gmail_api_url_assignments')"
                ).fetchall()
            }
            if canonical_tables == {
                "gmail_api_url_batches",
                "gmail_api_url_batch_items",
                "gmail_api_url_assignments",
            }:
                # QAN8 tables keep purchase provenance only.  Runtime claims
                # update the canonical Gmail rows, so derive the dashboard
                # counts from those rows instead of the stale QAN8 alias state.
                gmail_store = GmailApiUrlBatchStore(self.path)
                blocked_roots = gmail_store.runtime_blocked_canonical_roots()
                terminal_aliases = (
                    gmail_store._globally_terminal_aliases_in_connection(connection)
                )
                unavailable_aliases = (
                    gmail_store._globally_unavailable_aliases_in_connection(connection)
                )
                shadow_items = gmail_store._shadow_aliases_in_connection(connection)
                active_assignment_keys = {
                    (str(row["batch_id"]), str(row["inventory_id"]))
                    for row in connection.execute(
                        "SELECT batch_id, inventory_id FROM gmail_api_url_assignments "
                        "WHERE state = 'active'"
                    ).fetchall()
                }
                active_code_urls = {
                    str(row["code_url"] or "").strip()
                    for row in connection.execute(
                        "SELECT DISTINCT i.code_url "
                        "FROM gmail_api_url_assignments a "
                        "JOIN gmail_api_url_batch_items i ON i.batch_id = a.batch_id "
                        "AND i.inventory_id = a.inventory_id "
                        "WHERE a.state = 'active'"
                    ).fetchall()
                }
                active_source_ids = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT source_group_id FROM qan8_sources "
                        "WHERE batch_id = ? AND state = 'active'",
                        (str(batch_id),),
                    ).fetchall()
                }

                def _usable(row, *, include_reserved: bool) -> bool:
                    key = (str(row["batch_id"]), str(row["inventory_id"]))
                    if key in shadow_items:
                        return False
                    if gmail_store._alias_is_runtime_blocked(
                        str(row["email"] or ""), blocked_roots
                    ):
                        return False
                    alias = str(row["email"] or "").strip().casefold()
                    if alias in terminal_aliases:
                        return False
                    if not include_reserved and (
                        alias in unavailable_aliases
                        or key in active_assignment_keys
                        or str(row["code_url"] or "").strip() in active_code_urls
                    ):
                        return False
                    return (
                        str(row["state"] or "").strip().lower() == "active"
                        and int(row["completed_count"] or 0)
                        < int(row["capacity"] or 1)
                    )

                q8_rows = connection.execute(
                    "SELECT s.source_group_id, i.* , b.capacity "
                    "FROM qan8_sources s "
                    "JOIN qan8_aliases x ON x.source_group_id = s.source_group_id "
                    "JOIN gmail_api_url_batch_items i ON i.batch_id = x.gmail_batch_id "
                    "AND i.inventory_id = x.gmail_inventory_id "
                    "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id "
                    "WHERE s.batch_id = ?",
                    (str(batch_id),),
                ).fetchall()
                active_rows = [
                    row for row in q8_rows
                    if str(row["state"] or "").strip().lower() == "active"
                    and str(row["source_group_id"] or "")
                ]
                active_sources = len({
                    str(row["source_group_id"])
                    for row in active_rows
                    if _usable(row, include_reserved=True)
                    and str(row["source_group_id"]) in active_source_ids
                })
                remaining = sum(
                    1 for row in active_rows if _usable(row, include_reserved=False)
                )
                pending = sum(
                    1 for row in active_rows if _usable(row, include_reserved=True)
                )
                shared_rows = connection.execute(
                    "SELECT i.*, b.capacity FROM gmail_api_url_batch_items i "
                    "JOIN gmail_api_url_batches b ON b.batch_id = i.batch_id"
                ).fetchall()
                shared_remaining = sum(
                    1 for row in shared_rows if _usable(row, include_reserved=False)
                )
                shared_pending = sum(
                    1 for row in shared_rows if _usable(row, include_reserved=True)
                )
                shared_active_sources = len({
                    str(row["code_url"] or "").strip()
                    for row in shared_rows
                    if _usable(row, include_reserved=True)
                    and str(row["code_url"] or "").strip()
                })
                shared_active_assignments = connection.execute(
                    "SELECT COUNT(*) FROM gmail_api_url_assignments "
                    "WHERE state = 'active'"
                ).fetchone()[0]

                # A QAN8 batch may consume an alias already materialized by a
                # different batch.  That claim is intentionally recorded only
                # in the canonical runtime ledger, so there is no local QAN8
                # source row to join above.  Surface the shared ledger as the
                # effective batch capacity instead of reporting a false zero.
                if not active_rows:
                    active_sources = shared_active_sources
                    remaining = shared_remaining
                    pending = shared_pending
            else:
                active_sources = connection.execute(
                    "SELECT COUNT(*) FROM qan8_sources WHERE batch_id = ? AND state = 'active'",
                    (str(batch_id),),
                ).fetchone()[0]
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM qan8_aliases x JOIN qan8_sources s "
                    "ON s.source_group_id = x.source_group_id WHERE s.batch_id = ? "
                    "AND x.state = 'available'",
                    (str(batch_id),),
                ).fetchone()[0]
                pending = remaining
                shared_remaining = remaining
                shared_pending = remaining
                shared_active_assignments = connection.execute(
                    "SELECT COUNT(*) FROM qan8_assignments WHERE state = 'active'"
                ).fetchone()[0]
            orders = connection.execute(
                "SELECT COUNT(*) FROM qan8_orders WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()[0]
            sources = connection.execute(
                "SELECT COUNT(*) FROM qan8_sources WHERE batch_id = ?",
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
            "pending_aliases": int(pending),
            "shared_remaining_aliases": int(shared_remaining),
            "shared_pending_aliases": int(shared_pending),
            "shared_active_assignments": int(shared_active_assignments),
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
                "SELECT a.assignment_id, a.batch_id, a.lane_id, a.alias_id, "
                "x.gmail_batch_id, x.gmail_inventory_id FROM qan8_assignments a "
                "JOIN qan8_aliases x ON x.alias_id = a.alias_id "
                "WHERE a.job_id = ? AND a.state = 'active'",
                (str(job_id),),
            ).fetchone()
            if row is None:
                return False
            if row["gmail_batch_id"] and row["gmail_inventory_id"]:
                gmail_store = GmailApiUrlBatchStore(self.path)
                if alias_state == "consumed":
                    gmail_finished = gmail_store.finish_assignment_in_connection(
                        connection,
                        row["assignment_id"],
                        "completed",
                        reason=reason,
                    )
                elif alias_state == "failed":
                    gmail_finished = gmail_store.discard_assignment_in_connection(
                        connection,
                        row["assignment_id"],
                        reason=reason,
                    )
                else:
                    gmail_finished = gmail_store.finish_assignment_in_connection(
                        connection,
                        row["assignment_id"],
                        "released",
                        reason=reason,
                    )
                if not gmail_finished:
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
            if alias_state == "failed":
                source = connection.execute(
                    "SELECT s.source_group_id FROM qan8_sources s "
                    "WHERE s.source_group_id = (SELECT source_group_id FROM qan8_aliases "
                    "WHERE alias_id = ?) AND s.state = 'active' AND NOT EXISTS "
                    "(SELECT 1 FROM qan8_aliases x WHERE x.source_group_id = s.source_group_id "
                    "AND x.state IN ('available', 'active'))",
                    (row["alias_id"],),
                ).fetchone()
                if source:
                    connection.execute(
                        "UPDATE qan8_sources SET state = 'exhausted', retired_at = ? "
                        "WHERE source_group_id = ?",
                        (time.time(), source["source_group_id"]),
                    )
                    connection.execute(
                        "UPDATE qan8_lanes SET current_source_group_id = NULL "
                        "WHERE batch_id = ? AND lane_id = ? AND current_source_group_id = ?",
                        (row["batch_id"], row["lane_id"], source["source_group_id"]),
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
        self._ensure_lane_columns(connection)
        self._ensure_alias_columns(connection)
        connection.executescript(GmailApiUrlBatchStore(self.path)._get_schema_sql())
        return connection

    def _transaction(self):
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        return _Transaction(connection)

    @contextmanager
    def _runtime_transaction(self):
        """Serialize raw-pool status snapshots with QAN8 canonical claims."""
        from core import db

        with db._LOCK, self._transaction() as connection:
            yield connection

    @staticmethod
    def _require_lane(connection: sqlite3.Connection, batch_id: str, lane_id: int) -> None:
        row = connection.execute(
            "SELECT 1 FROM qan8_lanes WHERE batch_id = ? AND lane_id = ?",
            (str(batch_id), int(lane_id)),
        ).fetchone()
        if row is None:
            raise ValueError("QAN8 lane does not exist")

    @staticmethod
    def _require_active_lane(connection: sqlite3.Connection, batch_id: str, lane_id: int) -> None:
        row = connection.execute(
            "SELECT state FROM qan8_lanes WHERE batch_id = ? AND lane_id = ?",
            (str(batch_id), int(lane_id)),
        ).fetchone()
        if row is None:
            raise ValueError("QAN8 lane does not exist")
        if str(row["state"] or "active") != "active":
            raise RuntimeError("QAN8 lane is quarantined")

    @staticmethod
    def _ensure_lane_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(qan8_lanes)").fetchall()
        }
        if "state" not in columns:
            connection.execute(
                "ALTER TABLE qan8_lanes ADD COLUMN state TEXT NOT NULL DEFAULT 'active'"
            )
        if "failure_reason" not in columns:
            connection.execute(
                "ALTER TABLE qan8_lanes ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _ensure_alias_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(qan8_aliases)").fetchall()
        }
        if "gmail_batch_id" not in columns:
            connection.execute("ALTER TABLE qan8_aliases ADD COLUMN gmail_batch_id TEXT")
        if "gmail_inventory_id" not in columns:
            connection.execute("ALTER TABLE qan8_aliases ADD COLUMN gmail_inventory_id TEXT")

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
