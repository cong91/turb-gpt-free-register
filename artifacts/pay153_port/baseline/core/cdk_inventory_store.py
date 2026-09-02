# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import random
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core import app_state_db


SCHEMA_VERSION = 2
_MAX_CONFIGURED_LIMIT = 6


class CdkInventoryError(RuntimeError):
    """Base error for the local CDK inventory store."""


class CdkInventoryConflict(CdkInventoryError):
    """A state transition conflicts with the current durable state."""


class CdkInventoryBusy(CdkInventoryError):
    """The database stayed busy until the bounded retry deadline."""


class CdkInventorySchemaError(CdkInventoryError):
    """The store schema is missing, corrupt, or newer than this code."""


@dataclass(frozen=True)
class InventoryRecord:
    inventory_id: str
    provider: str
    fingerprint: str
    state: str
    configured_limit: int
    provider_remaining: int | None
    reserved_count: int
    consumed_count: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory_id": self.inventory_id,
            "provider": self.provider,
            "fingerprint": self.fingerprint,
            "state": self.state,
            "configured_limit": self.configured_limit,
            "provider_remaining": self.provider_remaining,
            "reserved_count": self.reserved_count,
            "consumed_count": self.consumed_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, repr=False)
class Reservation:
    reservation_id: str
    inventory_id: str
    email: str
    state: str
    job_id: str
    owner_token: str
    operation_id: str
    alias_phase: str | None = None
    alias_domain: str | None = None

    def __repr__(self) -> str:
        return (
            "Reservation("
            f"reservation_id={self.reservation_id!r}, inventory_id={self.inventory_id!r}, "
            f"email={self.email!r}, state={self.state!r}, job_id={self.job_id!r}, "
            f"operation_id={self.operation_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "inventory_id": self.inventory_id,
            "email": self.email,
            "state": self.state,
            "job_id": self.job_id,
            "operation_id": self.operation_id,
            "alias_phase": self.alias_phase,
            "alias_domain": self.alias_domain,
        }


@dataclass(frozen=True)
class InventoryEvent:
    event_id: str
    inventory_id: str
    event_type: str
    operation_id: str
    actor: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, repr=False)
class CdkLease:
    lease_id: str
    inventory_id: str
    owner_token: str
    fencing_token: str
    state: str
    expires_at: float
    heartbeat_at: float

    def __repr__(self) -> str:
        return (
            "CdkLease("
            f"lease_id={self.lease_id!r}, inventory_id={self.inventory_id!r}, "
            f"state={self.state!r}, expires_at={self.expires_at!r}, "
            f"heartbeat_at={self.heartbeat_at!r})"
        )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cdk_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cdk_inventory (
    inventory_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (length(provider) > 0),
    fingerprint TEXT NOT NULL,
    raw_cdk TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'disabled', 'exhausted', 'needs_review')),
    configured_limit INTEGER NOT NULL CHECK (configured_limit BETWEEN 1 AND 6),
    provider_remaining INTEGER CHECK (provider_remaining IS NULL OR provider_remaining >= 0),
    allocation_phase TEXT NOT NULL DEFAULT 'original'
        CHECK (allocation_phase IN ('original', 'routed')),
    routing_domains TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, fingerprint)
);
CREATE TABLE IF NOT EXISTS cdk_slots (
    slot_id TEXT PRIMARY KEY,
    inventory_id TEXT NOT NULL REFERENCES cdk_inventory(inventory_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    email TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'consumed', 'released')),
    job_id TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    alias_phase TEXT CHECK (alias_phase IS NULL OR alias_phase IN ('original', 'routed')),
    alias_domain TEXT,
    account_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS cdk_slots_live_email_idx
    ON cdk_slots(inventory_id, email) WHERE state != 'released';
CREATE INDEX IF NOT EXISTS cdk_slots_allocation_idx
    ON cdk_slots(inventory_id, state, email);
CREATE INDEX IF NOT EXISTS cdk_slots_job_idx
    ON cdk_slots(job_id, state);
CREATE TABLE IF NOT EXISTS cdk_leases (
    lease_id TEXT PRIMARY KEY,
    inventory_id TEXT NOT NULL REFERENCES cdk_inventory(inventory_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    owner_token TEXT NOT NULL,
    fencing_token TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('active', 'released', 'expired')),
    expires_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS cdk_active_lease_idx
    ON cdk_leases(inventory_id) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS cdk_intents (
    intent_id TEXT PRIMARY KEY,
    inventory_id TEXT NOT NULL REFERENCES cdk_inventory(inventory_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    operation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'external_started', 'succeeded', 'failed', 'uncertain')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cdk_events (
    event_id TEXT PRIMARY KEY,
    inventory_id TEXT NOT NULL REFERENCES cdk_inventory(inventory_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(inventory_id, sequence)
);
CREATE INDEX IF NOT EXISTS cdk_events_inventory_idx
    ON cdk_events(inventory_id, sequence DESC);
CREATE TRIGGER IF NOT EXISTS cdk_events_no_update
BEFORE UPDATE ON cdk_events BEGIN
    SELECT RAISE(ABORT, 'cdk_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS cdk_events_no_delete
BEFORE DELETE ON cdk_events BEGIN
    SELECT RAISE(ABORT, 'cdk_events is append-only');
END;
"""


class CdkInventoryStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 3000):
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            if app_state_db.is_app_state_path(self.path):
                app_state_db.ensure_schema(connection)
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if journal_mode == "wal":
                raise CdkInventorySchemaError(
                    "WAL is disabled for the supported SQLite runtime"
                )
            return connection
        except Exception:
            connection.close()
            raise

    def initialize(self) -> None:
        connection = self._connect()
        try:
            central = app_state_db.is_app_state_path(self.path)
            current = (
                app_state_db.get_component_version(connection, "cdk_inventory")
                if central
                else int(connection.execute("PRAGMA user_version").fetchone()[0])
            )
            if current > SCHEMA_VERSION:
                raise CdkInventorySchemaError(
                    f"Unsupported future CDK inventory schema version: {current}"
                )
            if current == 0:
                connection.executescript(_SCHEMA_SQL)
                connection.execute(
                    "INSERT OR IGNORE INTO cdk_schema_migrations(version, applied_at) "
                    "VALUES (?, CURRENT_TIMESTAMP)",
                    (SCHEMA_VERSION,),
                )
                if central:
                    app_state_db.set_component_version(connection, "cdk_inventory", SCHEMA_VERSION)
                else:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current == 1:
                connection.execute("BEGIN IMMEDIATE")
                inventory_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(cdk_inventory)")
                }
                slot_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(cdk_slots)")
                }
                if "allocation_phase" not in inventory_columns:
                    connection.execute(
                        "ALTER TABLE cdk_inventory ADD COLUMN allocation_phase TEXT "
                        "NOT NULL DEFAULT 'original' CHECK (allocation_phase IN ('original', 'routed'))"
                    )
                if "routing_domains" not in inventory_columns:
                    connection.execute(
                        "ALTER TABLE cdk_inventory ADD COLUMN routing_domains TEXT NOT NULL DEFAULT '[]'"
                    )
                if "alias_phase" not in slot_columns:
                    connection.execute(
                        "ALTER TABLE cdk_slots ADD COLUMN alias_phase TEXT "
                        "CHECK (alias_phase IS NULL OR alias_phase IN ('original', 'routed'))"
                    )
                if "alias_domain" not in slot_columns:
                    connection.execute("ALTER TABLE cdk_slots ADD COLUMN alias_domain TEXT")
                connection.execute(
                    "INSERT OR IGNORE INTO cdk_schema_migrations(version, applied_at) "
                    "VALUES (?, CURRENT_TIMESTAMP)",
                    (SCHEMA_VERSION,),
                )
                if central:
                    app_state_db.set_component_version(connection, "cdk_inventory", SCHEMA_VERSION)
                else:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            elif current == SCHEMA_VERSION:
                connection.executescript(_SCHEMA_SQL)
        except sqlite3.DatabaseError as exc:
            raise CdkInventorySchemaError("Unable to initialize CDK inventory schema") from exc
        finally:
            connection.close()

    def _require_initialized(self, connection: sqlite3.Connection) -> None:
        version = (
            app_state_db.get_component_version(connection, "cdk_inventory")
            if app_state_db.is_app_state_path(self.path)
            else int(connection.execute("PRAGMA user_version").fetchone()[0])
        )
        if version != SCHEMA_VERSION:
            raise CdkInventorySchemaError(f"Unsupported CDK inventory schema version: {version}")

    @staticmethod
    def _canonical(provider: str, raw_cdk: str) -> tuple[str, str, str]:
        provider_name = str(provider or "").strip().lower()
        raw_value = str(raw_cdk or "").strip()
        canonical = raw_value.upper()
        if not provider_name or not canonical:
            raise CdkInventoryError("Provider and CDK are required")
        fingerprint = "sha256:" + hashlib.sha256(
            f"{provider_name}:{canonical}".encode("utf-8")
        ).hexdigest()
        return provider_name, raw_value, fingerprint

    @staticmethod
    def _record(row: sqlite3.Row) -> InventoryRecord:
        return InventoryRecord(
            inventory_id=row["inventory_id"], provider=row["provider"],
            fingerprint=row["fingerprint"], state=row["state"],
            configured_limit=int(row["configured_limit"]),
            provider_remaining=row["provider_remaining"],
            reserved_count=int(row["reserved_count"] or 0),
            consumed_count=int(row["consumed_count"] or 0),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _record_query() -> str:
        return """
        SELECT i.*, SUM(CASE WHEN s.state = 'reserved' THEN 1 ELSE 0 END) AS reserved_count,
               SUM(CASE WHEN s.state = 'consumed' THEN 1 ELSE 0 END) AS consumed_count
        FROM cdk_inventory i
        LEFT JOIN cdk_slots s ON s.inventory_id = i.inventory_id
        """

    def import_cdk(self, provider: str, raw_cdk: str, *, configured_limit: int = 6) -> tuple[InventoryRecord, bool]:
        provider_name, canonical, fingerprint = self._canonical(provider, raw_cdk)
        limit = int(configured_limit)
        if not 1 <= limit <= _MAX_CONFIGURED_LIMIT:
            raise CdkInventoryError("configured_limit must be between 1 and 6")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT inventory_id FROM cdk_inventory WHERE provider = ? AND fingerprint = ?",
                (provider_name, fingerprint),
            ).fetchone()
            created = row is None
            if created:
                inventory_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cdk_inventory "
                    "(inventory_id, provider, fingerprint, raw_cdk, configured_limit) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (inventory_id, provider_name, fingerprint, canonical, limit),
                )
            else:
                inventory_id = row["inventory_id"]
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            row = connection.execute(
                "SELECT inventory_id FROM cdk_inventory WHERE provider = ? AND fingerprint = ?",
                (provider_name, fingerprint),
            ).fetchone()
            if not row:
                raise CdkInventoryConflict("CDK inventory identity conflict")
            inventory_id, created = row["inventory_id"], False
        finally:
            connection.close()
        return self.get_inventory(inventory_id), created

    def get_inventory(self, inventory_id: str) -> InventoryRecord | None:
        self.initialize()
        with closing(self._connect()) as connection:
            self._require_initialized(connection)
            row = connection.execute(
                self._record_query() + " WHERE i.inventory_id = ? GROUP BY i.inventory_id",
                (str(inventory_id),),
            ).fetchone()
            return self._record(row) if row else None

    def resolve_raw_cdk(self, inventory_id: str) -> str:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT raw_cdk FROM cdk_inventory WHERE inventory_id = ?",
                (str(inventory_id),),
            ).fetchone()
        if not row:
            raise CdkInventoryConflict("CDK inventory does not exist")
        return str(row["raw_cdk"])

    def list_inventory(self, *, provider=None, state=None, query="", limit=50, offset=0) -> tuple[list[InventoryRecord], int]:
        self.initialize()
        page_limit = max(1, min(500, int(limit)))
        page_offset = max(0, int(offset))
        clauses, params = [], []
        if provider:
            clauses.append("i.provider = ?")
            params.append(str(provider).strip().lower())
        if state:
            clauses.append("i.state = ?")
            params.append(str(state).strip().lower())
        if query:
            clauses.append("(i.inventory_id LIKE ? OR i.fingerprint LIKE ? OR i.provider LIKE ?)")
            value = f"%{str(query).strip()}%"
            params.extend([value, value, value])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM cdk_inventory i" + where, params
            ).fetchone()[0]
            rows = connection.execute(
                self._record_query() + where + " GROUP BY i.inventory_id "
                "ORDER BY i.updated_at DESC, i.inventory_id LIMIT ? OFFSET ?",
                [*params, page_limit, page_offset],
            ).fetchall()
        return [self._record(row) for row in rows], int(total)

    def update_provider_quota(self, inventory_id: str, remaining: int | None) -> InventoryRecord:
        if remaining is not None and int(remaining) < 0:
            raise CdkInventoryError("provider_remaining cannot be negative")
        self._write(lambda connection: connection.execute(
            "UPDATE cdk_inventory SET provider_remaining = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE inventory_id = ?", (remaining, inventory_id)
        ))
        record = self.get_inventory(inventory_id)
        if record is None:
            raise CdkInventoryConflict("CDK inventory does not exist")
        return record

    def update_configured_limit(self, inventory_id: str, new_limit: int) -> InventoryRecord:
        """Bump configured_limit; chỉ cho phép tăng, không giảm dưới số slot đã dùng."""
        limit = int(new_limit)
        if limit < 1:
            raise CdkInventoryError("configured_limit must be positive")

        def operation(connection: sqlite3.Connection):
            inventory = connection.execute(
                "SELECT * FROM cdk_inventory WHERE inventory_id = ?", (inventory_id,)
            ).fetchone()
            if not inventory:
                raise CdkInventoryConflict("CDK inventory does not exist")
            used = connection.execute(
                "SELECT COUNT(*) FROM cdk_slots WHERE inventory_id = ? AND state IN ('reserved', 'consumed')",
                (inventory_id,),
            ).fetchone()[0]
            if limit < used:
                raise CdkInventoryConflict(
                    f"configured_limit {limit} below used slots {used}"
                )
            if limit <= inventory["configured_limit"]:
                return None
            connection.execute(
                "UPDATE cdk_inventory SET configured_limit = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE inventory_id = ?", (limit, inventory_id)
            )
            self._append_event(
                connection, inventory_id, "configured_limit_bumped", "", "",
                {"configured_limit": limit},
            )
            return None

        self._write(operation)
        record = self.get_inventory(inventory_id)
        if record is None:
            raise CdkInventoryConflict("CDK inventory does not exist")
        return record

    def set_state(self, inventory_id: str, state: str) -> InventoryRecord:
        if state not in {"active", "disabled", "exhausted", "needs_review"}:
            raise CdkInventoryError("Invalid inventory state")
        changed = self._write(lambda connection: connection.execute(
            "UPDATE cdk_inventory SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE inventory_id = ?",
            (state, inventory_id),
        ))
        if changed.rowcount != 1:
            raise CdkInventoryConflict("CDK inventory does not exist")
        record = self.get_inventory(inventory_id)
        return record

    def reserve_slot(self, inventory_id: str, email: str, job_id: str, *, operation_id: str, owner_token: str) -> Reservation:
        email_value, job_value, owner_value, op_value = self._required_strings(email, job_id, owner_token, operation_id)
        self.initialize()

        def operation(connection: sqlite3.Connection) -> Reservation:
            existing = connection.execute(
                "SELECT * FROM cdk_slots WHERE operation_id = ?", (op_value,)
            ).fetchone()
            if existing:
                if (
                    existing["inventory_id"] != inventory_id
                    or existing["email"] != email_value
                    or existing["job_id"] != job_value
                    or existing["owner_token"] != owner_value
                ):
                    raise CdkInventoryConflict(
                        "Operation ID belongs to different reservation work"
                    )
                return self._reservation(existing)
            inventory = connection.execute(
                "SELECT * FROM cdk_inventory WHERE inventory_id = ?", (inventory_id,)
            ).fetchone()
            if not inventory:
                raise CdkInventoryConflict("CDK inventory does not exist")
            if inventory["state"] != "active":
                raise CdkInventoryConflict("CDK inventory is not active")
            used = connection.execute(
                "SELECT COUNT(*) FROM cdk_slots WHERE inventory_id = ? AND state IN ('reserved', 'consumed')",
                (inventory_id,),
            ).fetchone()[0]
            if used >= inventory["configured_limit"]:
                raise CdkInventoryConflict("CDK local quota is exhausted")
            duplicate = connection.execute(
                "SELECT * FROM cdk_slots WHERE inventory_id = ? AND email = ? AND state != 'released'",
                (inventory_id, email_value),
            ).fetchone()
            if duplicate:
                raise CdkInventoryConflict("Email slot is already reserved")
            slot_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO cdk_slots "
                "(slot_id, inventory_id, email, state, job_id, owner_token, operation_id) "
                "VALUES (?, ?, ?, 'reserved', ?, ?, ?)",
                (slot_id, inventory_id, email_value, job_value, owner_value, op_value),
            )
            self._append_event(connection, inventory_id, "slot_reserved", op_value, job_value, {"slot_id": slot_id, "email": email_value})
            row = connection.execute("SELECT * FROM cdk_slots WHERE slot_id = ?", (slot_id,)).fetchone()
            return self._reservation(row)

        return self._write(operation)

    def reserve_first_available_slot(
        self,
        inventory_id: str,
        emails: list[str],
        job_id: str,
        *,
        operation_id: str,
        owner_token: str,
    ) -> Reservation:
        candidates = [str(email or "").strip() for email in emails if str(email or "").strip()]
        if not candidates:
            raise CdkInventoryConflict("CDK local quota is exhausted")
        job_value, owner_value, op_value = self._required_strings(job_id, owner_token, operation_id)
        self.initialize()

        def operation(connection: sqlite3.Connection) -> Reservation:
            existing = connection.execute(
                "SELECT * FROM cdk_slots WHERE operation_id = ?", (op_value,)
            ).fetchone()
            if existing:
                if (
                    existing["inventory_id"] != inventory_id
                    or existing["email"] not in candidates
                    or existing["job_id"] != job_value
                    or existing["owner_token"] != owner_value
                ):
                    raise CdkInventoryConflict(
                        "Operation ID belongs to different reservation work"
                    )
                return self._reservation(existing)
            inventory = connection.execute(
                "SELECT * FROM cdk_inventory WHERE inventory_id = ?", (inventory_id,)
            ).fetchone()
            if not inventory:
                raise CdkInventoryConflict("CDK inventory does not exist")
            if inventory["state"] != "active":
                raise CdkInventoryConflict("CDK inventory is not active")
            used = connection.execute(
                "SELECT COUNT(*) FROM cdk_slots WHERE inventory_id = ? AND state IN ('reserved', 'consumed')",
                (inventory_id,),
            ).fetchone()[0]
            if used >= inventory["configured_limit"]:
                raise CdkInventoryConflict("CDK local quota is exhausted")
            for email_value in candidates:
                duplicate = connection.execute(
                    "SELECT 1 FROM cdk_slots WHERE inventory_id = ? AND email = ? AND state != 'released'",
                    (inventory_id, email_value),
                ).fetchone()
                if duplicate:
                    continue
                slot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cdk_slots "
                    "(slot_id, inventory_id, email, state, job_id, owner_token, operation_id) "
                    "VALUES (?, ?, ?, 'reserved', ?, ?, ?)",
                    (slot_id, inventory_id, email_value, job_value, owner_value, op_value),
                )
                self._append_event(
                    connection,
                    inventory_id,
                    "slot_reserved",
                    op_value,
                    job_value,
                    {"slot_id": slot_id, "email": email_value},
                )
                return self._reservation(
                    connection.execute(
                        "SELECT * FROM cdk_slots WHERE slot_id = ?", (slot_id,)
                    ).fetchone()
                )
            raise CdkInventoryConflict("CDK local quota is exhausted")

        return self._write(operation)

    def reserve_gmail_alias(
        self,
        inventory_id: str,
        candidates,
        job_id: str,
        *,
        operation_id: str,
        owner_token: str,
        routed_domains=(),
    ) -> Reservation:
        candidate_rows = []
        for candidate in candidates:
            email = str(getattr(candidate, "email", "") or "").strip().lower()
            phase = str(getattr(candidate, "phase", "") or "").strip().lower()
            domain = str(getattr(candidate, "domain", "") or "").strip().lower()
            if email and phase in {"original", "routed"} and domain:
                candidate_rows.append((email, phase, domain))
        if not candidate_rows:
            raise CdkInventoryConflict("CDK local quota is exhausted")
        domains = tuple(dict.fromkeys(
            str(domain or "").strip().lower()
            for domain in routed_domains
            if str(domain or "").strip()
        ))
        job_value, owner_value, op_value = self._required_strings(
            job_id, owner_token, operation_id
        )
        self.initialize()

        def operation(connection: sqlite3.Connection) -> Reservation:
            existing = connection.execute(
                "SELECT * FROM cdk_slots WHERE operation_id = ?", (op_value,)
            ).fetchone()
            if existing:
                if (
                    existing["inventory_id"] != inventory_id
                    or existing["job_id"] != job_value
                    or existing["owner_token"] != owner_value
                ):
                    raise CdkInventoryConflict(
                        "Operation ID belongs to different reservation work"
                    )
                return self._reservation(existing)
            inventory = connection.execute(
                "SELECT * FROM cdk_inventory WHERE inventory_id = ?", (inventory_id,)
            ).fetchone()
            if not inventory:
                raise CdkInventoryConflict("CDK inventory does not exist")
            if inventory["provider"] != "gmail" or inventory["state"] != "active":
                raise CdkInventoryConflict("CDK inventory is not active")
            bound_domains = tuple(json.loads(inventory["routing_domains"] or "[]"))
            if domains and bound_domains and domains != bound_domains:
                raise CdkInventoryConflict("Gmail routing domains changed after allocation")
            if domains and not bound_domains:
                connection.execute(
                    "UPDATE cdk_inventory SET routing_domains = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE inventory_id = ?",
                    (json.dumps(domains, ensure_ascii=False), inventory_id),
                )
                bound_domains = domains
            phase = str(inventory["allocation_phase"] or "original")
            configured_limit = int(inventory["configured_limit"])
            has_routed = bool(bound_domains) and any(row[1] == "routed" for row in candidate_rows)
            phase_candidates = [row for row in candidate_rows if row[1] == phase]
            used_phase = connection.execute(
                "SELECT COUNT(*) FROM cdk_slots WHERE inventory_id = ? "
                "AND state IN ('reserved', 'consumed') AND alias_phase = ?",
                (inventory_id, phase),
            ).fetchone()[0]
            if phase == "original" and used_phase >= configured_limit and has_routed:
                phase = "routed"
                connection.execute(
                    "UPDATE cdk_inventory SET allocation_phase = 'routed', "
                    "updated_at = CURRENT_TIMESTAMP WHERE inventory_id = ?",
                    (inventory_id,),
                )
                phase_candidates = [row for row in candidate_rows if row[1] == phase]
                used_phase = connection.execute(
                    "SELECT COUNT(*) FROM cdk_slots WHERE inventory_id = ? "
                    "AND state IN ('reserved', 'consumed') AND alias_phase = 'routed'",
                    (inventory_id,),
                ).fetchone()[0]
            if used_phase >= configured_limit:
                raise CdkInventoryConflict("CDK local quota is exhausted")
            for email_value, alias_phase, alias_domain in phase_candidates:
                duplicate = connection.execute(
                    "SELECT 1 FROM cdk_slots WHERE inventory_id = ? AND email = ? "
                    "AND state != 'released'",
                    (inventory_id, email_value),
                ).fetchone()
                if duplicate:
                    continue
                slot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO cdk_slots "
                    "(slot_id, inventory_id, email, state, job_id, owner_token, "
                    "operation_id, alias_phase, alias_domain) "
                    "VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?)",
                    (
                        slot_id, inventory_id, email_value, job_value, owner_value,
                        op_value, alias_phase, alias_domain,
                    ),
                )
                self._append_event(
                    connection,
                    inventory_id,
                    "slot_reserved",
                    op_value,
                    job_value,
                    {
                        "slot_id": slot_id,
                        "email": email_value,
                        "alias_phase": alias_phase,
                        "alias_domain": alias_domain,
                    },
                )
                return self._reservation(connection.execute(
                    "SELECT * FROM cdk_slots WHERE slot_id = ?", (slot_id,)
                ).fetchone())
            raise CdkInventoryConflict("CDK local quota is exhausted")

        return self._write(operation)

    def consume_reservation(self, reservation_id: str, *, operation_id: str, owner_token: str, account_id: int | None = None) -> bool:
        return self._transition_slot(reservation_id, "consumed", operation_id, owner_token, account_id=account_id)

    def release_reservation(self, reservation_id: str, *, operation_id: str, owner_token: str, reason: str | None = None) -> bool:
        return self._transition_slot(reservation_id, "released", operation_id, owner_token, reason=reason)

    def _transition_slot(self, reservation_id: str, target: str, operation_id: str, owner_token: str, *, account_id=None, reason=None) -> bool:
        op_value, owner_value = self._required_strings(operation_id, owner_token)
        self.initialize()

        def operation(connection: sqlite3.Connection) -> bool:
            prior_event = connection.execute(
                "SELECT event_type, actor, payload FROM cdk_events WHERE operation_id = ?",
                (op_value,),
            ).fetchone()
            if prior_event:
                import json

                payload = json.loads(prior_event["payload"])
                if (
                    prior_event["event_type"] != f"slot_{target}"
                    or payload.get("slot_id") != reservation_id
                ):
                    raise CdkInventoryConflict(
                        "Operation ID belongs to different transition work"
                    )
                return True
            row = connection.execute(
                "SELECT * FROM cdk_slots WHERE slot_id = ?", (reservation_id,)
            ).fetchone()
            if not row or row["owner_token"] != owner_value or row["state"] != "reserved":
                return False
            connection.execute(
                "UPDATE cdk_slots SET state = ?, account_id = COALESCE(?, account_id), updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?",
                (target, account_id, reservation_id),
            )
            self._append_event(connection, row["inventory_id"], f"slot_{target}", op_value, row["job_id"], {"slot_id": reservation_id, "reason": reason} if reason else {"slot_id": reservation_id})
            return True

        return bool(self._write(operation))

    def list_events(self, inventory_id: str, *, limit=50, offset=0) -> tuple[list[InventoryEvent], int]:
        self.initialize()
        with closing(self._connect()) as connection:
            total = connection.execute("SELECT COUNT(*) FROM cdk_events WHERE inventory_id = ?", (inventory_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM cdk_events WHERE inventory_id = ? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (inventory_id, max(1, min(500, int(limit))), max(0, int(offset))),
            ).fetchall()
        import json
        return [InventoryEvent(row["event_id"], row["inventory_id"], row["event_type"], row["operation_id"], row["actor"], json.loads(row["payload"]), row["created_at"]) for row in rows], int(total)

    def acquire_lease(self, inventory_id: str, *, owner_token: str, ttl_seconds: float = 600) -> CdkLease:
        owner_value = self._required_strings(owner_token)[0]
        now = time.time()
        expiry = now + max(1.0, float(ttl_seconds))
        self.initialize()

        def operation(connection: sqlite3.Connection) -> CdkLease:
            connection.execute("UPDATE cdk_leases SET state = 'expired', updated_at = CURRENT_TIMESTAMP WHERE inventory_id = ? AND state = 'active' AND expires_at <= ?", (inventory_id, now))
            current = connection.execute("SELECT * FROM cdk_leases WHERE inventory_id = ? AND state = 'active'", (inventory_id,)).fetchone()
            if current:
                raise CdkInventoryConflict("CDK already has an active lease")
            lease_id, fencing = uuid.uuid4().hex, secrets.token_urlsafe(32)
            connection.execute("INSERT INTO cdk_leases (lease_id, inventory_id, owner_token, fencing_token, state, expires_at, heartbeat_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", (lease_id, inventory_id, owner_value, fencing, expiry, now))
            self._append_event(connection, inventory_id, "lease_acquired", lease_id, owner_value, {"lease_id": lease_id})
            row = connection.execute("SELECT * FROM cdk_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            return self._lease(row)

        return self._write(operation)

    def heartbeat_lease(self, lease_id: str, *, owner_token: str, fencing_token: str, ttl_seconds: float = 600) -> CdkLease:
        return self._update_lease(lease_id, owner_token, fencing_token, max(1.0, float(ttl_seconds)), "heartbeat")

    def assert_active_lease(
        self,
        lease_id: str,
        *,
        owner_token: str,
        fencing_token: str,
    ) -> CdkLease:
        owner_value, fencing_value = self._required_strings(owner_token, fencing_token)
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cdk_leases WHERE lease_id = ? AND owner_token = ? "
                "AND fencing_token = ? AND state = 'active' AND expires_at > ?",
                (lease_id, owner_value, fencing_value, time.time()),
            ).fetchone()
        if not row:
            raise CdkInventoryConflict("Lease fencing token is stale or expired")
        return self._lease(row)

    def release_lease(self, lease_id: str, *, owner_token: str, fencing_token: str) -> bool:
        owner_value, fencing_value = self._required_strings(owner_token, fencing_token)
        self.initialize()

        def operation(connection):
            result = connection.execute("UPDATE cdk_leases SET state = 'released', updated_at = CURRENT_TIMESTAMP WHERE lease_id = ? AND owner_token = ? AND fencing_token = ? AND state = 'active'", (lease_id, owner_value, fencing_value))
            return result.rowcount == 1

        return bool(self._write(operation))

    def _update_lease(self, lease_id, owner_token, fencing_token, ttl_seconds, event_type):
        owner_value, fencing_value = self._required_strings(owner_token, fencing_token)
        now, expiry = time.time(), time.time() + ttl_seconds
        self.initialize()

        def operation(connection):
            row = connection.execute("SELECT * FROM cdk_leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if not row or row["owner_token"] != owner_value or row["fencing_token"] != fencing_value or row["state"] != "active" or row["expires_at"] <= now:
                raise CdkInventoryConflict("Lease fencing token is stale or expired")
            connection.execute("UPDATE cdk_leases SET expires_at = ?, heartbeat_at = ?, updated_at = CURRENT_TIMESTAMP WHERE lease_id = ?", (expiry, now, lease_id))
            self._append_event(connection, row["inventory_id"], f"lease_{event_type}", uuid.uuid4().hex, owner_value, {"lease_id": lease_id})
            return self._lease(connection.execute("SELECT * FROM cdk_leases WHERE lease_id = ?", (lease_id,)).fetchone())

        return self._write(operation)

    @staticmethod
    def _required_strings(*values: str) -> tuple[str, ...]:
        result = tuple(str(value or "").strip() for value in values)
        if any(not value for value in result):
            raise CdkInventoryError("Required inventory operation value is missing")
        return result

    @staticmethod
    def _reservation(row: sqlite3.Row) -> Reservation:
        keys = set(row.keys())
        return Reservation(
            row["slot_id"],
            row["inventory_id"],
            row["email"],
            row["state"],
            row["job_id"],
            row["owner_token"],
            row["operation_id"],
            row["alias_phase"] if "alias_phase" in keys else None,
            row["alias_domain"] if "alias_domain" in keys else None,
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> CdkLease:
        return CdkLease(row["lease_id"], row["inventory_id"], row["owner_token"], row["fencing_token"], row["state"], float(row["expires_at"]), float(row["heartbeat_at"]))

    @staticmethod
    def _append_event(connection, inventory_id, event_type, operation_id, actor, payload):
        import json
        sequence = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM cdk_events WHERE inventory_id = ?", (inventory_id,)).fetchone()[0]
        connection.execute("INSERT INTO cdk_events (event_id, inventory_id, sequence, event_type, operation_id, actor, payload) VALUES (?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, inventory_id, sequence, event_type, operation_id, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def _write(self, operation: Callable[[sqlite3.Connection], Any]):
        deadline = time.monotonic() + self.busy_timeout_ms / 1000
        delay = 0.01
        while True:
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                connection.close()
                return result
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    try:
                        connection.rollback()
                    finally:
                        connection.close()
                if not self._is_busy(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdkInventoryBusy("CDK inventory remained busy until timeout") from exc
                time.sleep(min(remaining, delay * (0.8 + random.random() * 0.4)))
                delay = min(0.2, delay * 2)
            except Exception:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                raise

    @staticmethod
    def _is_busy(exc: sqlite3.OperationalError) -> bool:
        return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY
