# -*- coding: utf-8 -*-
"""Durable catalog and operation ledger for managed Roxy profiles."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import app_state_db

SCHEMA_VERSION = 3

PROFILE_STATES = {
    "LOCAL_ONLY",
    "REMOTE_CREATING",
    "ACTIVE_STOPPED",
    "RUNNING",
    "SNAPSHOTTING",
    "ARCHIVE_COMMITTED",
    "SOFT_DELETE_PENDING",
    "TRASHED",
    "RESTORE_REQUIRED",
    "OFFLINE_STAGING",
    "OFFLINE_RUNNING",
    "OFFLINE_STOPPED",
    "OFFLINE_UNVERIFIED",
    "NEEDS_RECONCILIATION",
}

_ALLOWED_TRANSITIONS = {
    "LOCAL_ONLY": {"REMOTE_CREATING", "OFFLINE_STOPPED"},
    "REMOTE_CREATING": {"ACTIVE_STOPPED", "NEEDS_RECONCILIATION"},
    "ACTIVE_STOPPED": {"RUNNING", "SNAPSHOTTING", "NEEDS_RECONCILIATION"},
    "RUNNING": {"ACTIVE_STOPPED", "NEEDS_RECONCILIATION"},
    "SNAPSHOTTING": {
        "ARCHIVE_COMMITTED", "ACTIVE_STOPPED", "OFFLINE_STOPPED",
        "OFFLINE_UNVERIFIED", "NEEDS_RECONCILIATION",
    },
    "ARCHIVE_COMMITTED": {
        "ACTIVE_STOPPED", "SNAPSHOTTING", "SOFT_DELETE_PENDING",
        "OFFLINE_STAGING", "NEEDS_RECONCILIATION",
    },
    "SOFT_DELETE_PENDING": {"TRASHED", "NEEDS_RECONCILIATION"},
    "TRASHED": {"RESTORE_REQUIRED", "ACTIVE_STOPPED", "OFFLINE_STAGING", "NEEDS_RECONCILIATION"},
    "RESTORE_REQUIRED": {"ACTIVE_STOPPED", "OFFLINE_STAGING", "NEEDS_RECONCILIATION"},
    "OFFLINE_STAGING": {"OFFLINE_RUNNING", "OFFLINE_UNVERIFIED", "NEEDS_RECONCILIATION"},
    "OFFLINE_RUNNING": {"OFFLINE_STOPPED", "SNAPSHOTTING", "OFFLINE_UNVERIFIED", "NEEDS_RECONCILIATION"},
    "OFFLINE_STOPPED": {"OFFLINE_STAGING", "SNAPSHOTTING", "ACTIVE_STOPPED", "NEEDS_RECONCILIATION"},
    "OFFLINE_UNVERIFIED": {"OFFLINE_STAGING", "SNAPSHOTTING", "NEEDS_RECONCILIATION"},
    "NEEDS_RECONCILIATION": {
        "REMOTE_CREATING", "ACTIVE_STOPPED", "RUNNING", "TRASHED",
        "RESTORE_REQUIRED", "OFFLINE_STOPPED", "OFFLINE_UNVERIFIED",
    },
}


class RoxyProfileStoreError(RuntimeError):
    """Base error for profile-manager persistence."""


class RoxyProfileConflict(RoxyProfileStoreError):
    """A requested state transition conflicts with durable state."""


class RoxyProfileSchemaError(RoxyProfileStoreError):
    """The profile-manager database schema is unsupported or corrupt."""


@dataclass(frozen=True)
class ManagedRoxyProfile:
    local_id: str
    dir_id: str | None
    workspace_id: str
    project_id: str
    display_name: str
    owner_marker: str
    state: str
    remote_state: str
    archive_id: str | None
    official_signature_sha256: str
    offline_staging_path: str
    last_error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_id": self.local_id,
            "dir_id": self.dir_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "display_name": self.display_name,
            "owner_marker": self.owner_marker,
            "state": self.state,
            "remote_state": self.remote_state,
            "archive_id": self.archive_id,
            "official_signature_sha256": self.official_signature_sha256,
            "offline_staging_path": self.offline_staging_path,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RoxyProfileArchiveRecord:
    archive_id: str
    local_id: str
    format_version: str
    archive_kind: str
    source_core_version: str
    path: str
    byte_size: int
    sha256: str
    encrypted: bool
    capabilities: dict[str, Any]
    created_at: str
    verified_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "local_id": self.local_id,
            "format_version": self.format_version,
            "archive_kind": self.archive_kind,
            "source_core_version": self.source_core_version,
            "path": self.path,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "encrypted": self.encrypted,
            "capabilities": dict(self.capabilities),
            "created_at": self.created_at,
            "verified_at": self.verified_at,
        }


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roxy_profile_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS roxy_profiles (
    local_id TEXT PRIMARY KEY,
    dir_id TEXT UNIQUE,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    owner_marker TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    remote_state TEXT NOT NULL DEFAULT 'unknown',
    archive_id TEXT,
    official_signature_sha256 TEXT NOT NULL DEFAULT '',
    offline_staging_path TEXT NOT NULL DEFAULT '',
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS roxy_profiles_state_idx
    ON roxy_profiles(state, updated_at DESC);
CREATE TABLE IF NOT EXISTS roxy_profile_archives (
    archive_id TEXT PRIMARY KEY,
    local_id TEXT NOT NULL REFERENCES roxy_profiles(local_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    format_version TEXT NOT NULL,
    archive_kind TEXT NOT NULL,
    source_core_version TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    encrypted INTEGER NOT NULL CHECK (encrypted IN (0, 1)),
    capabilities TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roxy_profile_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    local_id TEXT NOT NULL REFERENCES roxy_profiles(local_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    operation_type TEXT NOT NULL,
    state TEXT NOT NULL,
    checkpoint TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS roxy_profile_launches (
    local_id TEXT PRIMARY KEY REFERENCES roxy_profiles(local_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    backend TEXT NOT NULL,
    executable_path TEXT NOT NULL,
    pid INTEGER NOT NULL,
    debugger_address TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    process_started_at TEXT NOT NULL,
    core_version TEXT NOT NULL DEFAULT '',
    fingerprint_status TEXT NOT NULL DEFAULT 'unknown',
    signature_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS roxy_profile_events (
    event_id TEXT PRIMARY KEY,
    local_id TEXT NOT NULL REFERENCES roxy_profiles(local_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    operation_id TEXT,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(local_id, sequence)
);
CREATE TRIGGER IF NOT EXISTS roxy_profile_events_no_update
BEFORE UPDATE ON roxy_profile_events BEGIN
    SELECT RAISE(ABORT, 'roxy_profile_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS roxy_profile_events_no_delete
BEFORE DELETE ON roxy_profile_events BEGIN
    SELECT RAISE(ABORT, 'roxy_profile_events is append-only');
END;
"""


class RoxyProfileStore:
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
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        if app_state_db.is_app_state_path(self.path):
            app_state_db.ensure_schema(connection)
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            central = app_state_db.is_app_state_path(self.path)
            current = (
                app_state_db.get_component_version(connection, "roxy_profile")
                if central
                else int(connection.execute("PRAGMA user_version").fetchone()[0])
            )
            if current > SCHEMA_VERSION:
                raise RoxyProfileSchemaError(
                    f"Unsupported future Roxy profile schema version: {current}"
                )
            if current == 0:
                connection.executescript(_SCHEMA_SQL)
                connection.execute(
                    "INSERT OR IGNORE INTO roxy_profile_schema_migrations(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                if central:
                    app_state_db.set_component_version(connection, "roxy_profile", SCHEMA_VERSION)
                else:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current == SCHEMA_VERSION:
                connection.executescript(_SCHEMA_SQL)
            else:
                raise RoxyProfileSchemaError(
                    f"Unsupported Roxy profile schema version: {current}"
                )
        except sqlite3.DatabaseError as exc:
            raise RoxyProfileSchemaError(
                "Unable to initialize Roxy profile-manager schema"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _profile(row: sqlite3.Row) -> ManagedRoxyProfile:
        return ManagedRoxyProfile(
            local_id=row["local_id"],
            dir_id=row["dir_id"],
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            display_name=row["display_name"],
            owner_marker=row["owner_marker"],
            state=row["state"],
            remote_state=row["remote_state"],
            archive_id=row["archive_id"],
            official_signature_sha256=row["official_signature_sha256"],
            offline_staging_path=row["offline_staging_path"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _archive(row: sqlite3.Row) -> RoxyProfileArchiveRecord:
        return RoxyProfileArchiveRecord(
            archive_id=row["archive_id"],
            local_id=row["local_id"],
            format_version=row["format_version"],
            archive_kind=row["archive_kind"],
            source_core_version=row["source_core_version"],
            path=row["path"],
            byte_size=int(row["byte_size"]),
            sha256=row["sha256"],
            encrypted=bool(row["encrypted"]),
            capabilities=json.loads(row["capabilities"] or "{}"),
            created_at=row["created_at"],
            verified_at=row["verified_at"],
        )

    def create_profile(
        self,
        *,
        workspace_id: str,
        project_id: str,
        display_name: str,
        owner_marker: str,
        local_id: str | None = None,
    ) -> ManagedRoxyProfile:
        self.initialize()
        profile_id = str(local_id or uuid.uuid4().hex)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO roxy_profiles "
                    "(local_id, workspace_id, project_id, display_name, owner_marker, state) "
                    "VALUES (?, ?, ?, ?, ?, 'LOCAL_ONLY')",
                    (
                        profile_id,
                        str(workspace_id),
                        str(project_id or ""),
                        str(display_name),
                        str(owner_marker),
                    ),
                )
                self._append_event_tx(
                    connection,
                    profile_id,
                    "profile_created",
                    actor="system",
                    payload={"state": "LOCAL_ONLY"},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RoxyProfileConflict("Managed profile identity already exists") from exc
        profile = self.get_profile(profile_id)
        if profile is None:
            raise RoxyProfileStoreError("Unable to read newly created profile")
        return profile

    def import_offline_profile(
        self,
        *,
        local_id: str,
        workspace_id: str,
        project_id: str,
        display_name: str,
        owner_marker: str,
        archive_id: str,
        format_version: str,
        archive_kind: str,
        source_core_version: str,
        path: str,
        byte_size: int,
        sha256: str,
        capabilities: dict[str, Any],
        verified_at: str,
    ) -> tuple[ManagedRoxyProfile, RoxyProfileArchiveRecord]:
        self.initialize()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO roxy_profiles "
                    "(local_id, workspace_id, project_id, display_name, owner_marker, "
                    "state, remote_state, archive_id) "
                    "VALUES (?, ?, ?, ?, ?, 'OFFLINE_STOPPED', 'local_only', ?)",
                    (
                        str(local_id), str(workspace_id), str(project_id or ""),
                        str(display_name), str(owner_marker), str(archive_id),
                    ),
                )
                connection.execute(
                    "INSERT INTO roxy_profile_archives "
                    "(archive_id, local_id, format_version, archive_kind, "
                    "source_core_version, path, byte_size, sha256, encrypted, "
                    "capabilities, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        str(archive_id), str(local_id), str(format_version),
                        str(archive_kind), str(source_core_version), str(path),
                        int(byte_size), str(sha256),
                        json.dumps(capabilities, sort_keys=True), str(verified_at),
                    ),
                )
                self._append_event_tx(
                    connection,
                    str(local_id),
                    "profile_imported",
                    actor="system",
                    payload={"state": "OFFLINE_STOPPED", "archive_id": str(archive_id)},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RoxyProfileConflict(
                    "Imported profile or archive identity already exists"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        profile = self.get_profile(local_id)
        archive = self.get_archive(archive_id)
        if profile is None or archive is None:
            raise RoxyProfileStoreError("Unable to read imported profile catalog")
        return profile, archive

    def get_profile(self, local_id: str) -> ManagedRoxyProfile | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM roxy_profiles WHERE local_id = ?",
                (str(local_id),),
            ).fetchone()
        return self._profile(row) if row else None

    def list_profiles(self) -> list[ManagedRoxyProfile]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM roxy_profiles ORDER BY updated_at DESC, local_id"
            ).fetchall()
        return [self._profile(row) for row in rows]

    def find_by_owner_marker(self, owner_marker: str) -> ManagedRoxyProfile | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM roxy_profiles WHERE owner_marker = ?",
                (str(owner_marker),),
            ).fetchone()
        return self._profile(row) if row else None

    def transition(
        self,
        local_id: str,
        new_state: str,
        *,
        expected_state: str | None = None,
        dir_id: str | None = None,
        remote_state: str | None = None,
        last_error: str | None = None,
        event_type: str = "state_changed",
        operation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ManagedRoxyProfile:
        target = str(new_state or "").strip().upper()
        if target not in PROFILE_STATES:
            raise RoxyProfileConflict(f"Unsupported profile state: {target}")
        self.initialize()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM roxy_profiles WHERE local_id = ?",
                    (str(local_id),),
                ).fetchone()
                if row is None:
                    raise RoxyProfileConflict("Managed profile does not exist")
                current = row["state"]
                if expected_state is not None and current != expected_state:
                    raise RoxyProfileConflict(
                        f"Profile state is {current}, expected {expected_state}"
                    )
                if target != current and target not in _ALLOWED_TRANSITIONS.get(current, set()):
                    raise RoxyProfileConflict(
                        f"Invalid profile transition: {current} -> {target}"
                    )
                connection.execute(
                    "UPDATE roxy_profiles SET state = ?, "
                    "dir_id = COALESCE(?, dir_id), "
                    "remote_state = COALESCE(?, remote_state), last_error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE local_id = ?",
                    (
                        target,
                        str(dir_id) if dir_id else None,
                        str(remote_state) if remote_state is not None else None,
                        str(last_error)[:1000] if last_error else None,
                        str(local_id),
                    ),
                )
                event_payload = {"from": current, "to": target}
                event_payload.update(payload or {})
                self._append_event_tx(
                    connection,
                    str(local_id),
                    event_type,
                    operation_id=operation_id,
                    actor="system",
                    payload=event_payload,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        profile = self.get_profile(local_id)
        if profile is None:
            raise RoxyProfileStoreError("Managed profile disappeared after transition")
        return profile

    def update_identity(
        self,
        local_id: str,
        *,
        dir_id: str,
        display_name: str | None = None,
        remote_state: str = "active",
    ) -> ManagedRoxyProfile:
        self.initialize()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT display_name FROM roxy_profiles WHERE local_id = ?",
                    (str(local_id),),
                ).fetchone()
                if row is None:
                    raise RoxyProfileConflict("Managed profile does not exist")
                connection.execute(
                    "UPDATE roxy_profiles SET dir_id = ?, display_name = ?, "
                    "remote_state = ?, updated_at = CURRENT_TIMESTAMP WHERE local_id = ?",
                    (
                        str(dir_id),
                        str(display_name or row["display_name"]),
                        str(remote_state),
                        str(local_id),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RoxyProfileConflict("Remote profile is already managed") from exc
        profile = self.get_profile(local_id)
        if profile is None:
            raise RoxyProfileStoreError("Managed profile disappeared after identity update")
        return profile

    def save_official_signature(self, local_id: str, signature_sha256: str) -> None:
        signature = str(signature_sha256 or "").strip().lower()
        if len(signature) != 64 or any(
            char not in "0123456789abcdef" for char in signature
        ):
            raise RoxyProfileConflict("Official signature hash is invalid")
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE roxy_profiles SET official_signature_sha256 = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE local_id = ?",
                (signature, str(local_id)),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RoxyProfileConflict("Managed profile does not exist")
            self._append_event_tx(
                connection,
                str(local_id),
                "official_signature_captured",
                actor="system",
                payload={"signature_sha256": signature},
            )
            connection.commit()

    def set_offline_staging_path(self, local_id: str, path: str = "") -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE roxy_profiles SET offline_staging_path = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE local_id = ?",
                (str(path or ""), str(local_id)),
            )
            if cursor.rowcount != 1:
                raise RoxyProfileConflict("Managed profile does not exist")
            connection.commit()

    def save_archive(
        self,
        *,
        local_id: str,
        archive_id: str,
        format_version: str,
        archive_kind: str,
        source_core_version: str,
        path: str,
        byte_size: int,
        sha256: str,
        capabilities: dict[str, Any],
        verified_at: str,
    ) -> RoxyProfileArchiveRecord:
        self.initialize()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO roxy_profile_archives "
                    "(archive_id, local_id, format_version, archive_kind, source_core_version, path, byte_size, sha256, "
                    "encrypted, capabilities, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        str(archive_id), str(local_id), str(format_version),
                        str(archive_kind), str(source_core_version), str(path),
                        int(byte_size), str(sha256),
                        json.dumps(capabilities, sort_keys=True), str(verified_at),
                    ),
                )
                connection.execute(
                    "UPDATE roxy_profiles SET archive_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE local_id = ?",
                    (str(archive_id), str(local_id)),
                )
                self._append_event_tx(
                    connection,
                    str(local_id),
                    "archive_saved",
                    actor="system",
                    payload={"archive_id": str(archive_id), "sha256": str(sha256)},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RoxyProfileConflict("Archive identity already exists") from exc
        record = self.get_archive(archive_id)
        if record is None:
            raise RoxyProfileStoreError("Unable to read saved archive")
        return record

    def get_archive(self, archive_id: str) -> RoxyProfileArchiveRecord | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM roxy_profile_archives WHERE archive_id = ?",
                (str(archive_id),),
            ).fetchone()
        return self._archive(row) if row else None

    def save_launch(
        self,
        *,
        local_id: str,
        backend: str,
        pid: int,
        debugger_address: str,
        staging_path: str,
        executable_path: str,
        process_started_at: str,
        core_version: str = "",
        fingerprint_status: str = "unknown",
        signature_sha256: str = "",
    ) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO roxy_profile_launches "
                "(local_id, backend, executable_path, pid, debugger_address, staging_path, process_started_at, core_version, fingerprint_status, signature_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(local_id) DO UPDATE SET backend=excluded.backend, "
                "executable_path=excluded.executable_path, pid=excluded.pid, "
                "debugger_address=excluded.debugger_address, "
                "staging_path=excluded.staging_path, "
                "process_started_at=excluded.process_started_at, "
                "core_version=excluded.core_version, "
                "fingerprint_status=excluded.fingerprint_status, "
                "signature_sha256=excluded.signature_sha256, "
                "updated_at=CURRENT_TIMESTAMP",
                (
                    str(local_id), str(backend), str(executable_path), int(pid),
                    str(debugger_address), str(staging_path), str(process_started_at),
                    str(core_version), str(fingerprint_status), str(signature_sha256),
                ),
            )
            connection.execute(
                "UPDATE roxy_profiles SET offline_staging_path = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE local_id = ?",
                (str(staging_path), str(local_id)),
            )
            connection.commit()
        launch = self.get_launch(local_id)
        if launch is None:
            raise RoxyProfileStoreError("Unable to read saved local launch")
        return launch

    def get_launch(self, local_id: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM roxy_profile_launches WHERE local_id = ?",
                (str(local_id),),
            ).fetchone()
        return dict(row) if row else None

    def clear_launch(self, local_id: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM roxy_profile_launches WHERE local_id = ?",
                (str(local_id),),
            )
            connection.commit()

    def prepare_operation(
        self,
        *,
        local_id: str,
        operation_type: str,
        idempotency_key: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM roxy_profile_operations WHERE idempotency_key = ?",
                (str(idempotency_key),),
            ).fetchone()
            if existing:
                connection.commit()
                return self._operation_dict(existing), False
            operation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO roxy_profile_operations "
                "(operation_id, idempotency_key, local_id, operation_type, state, checkpoint) "
                "VALUES (?, ?, ?, ?, 'prepared', ?)",
                (
                    operation_id, str(idempotency_key), str(local_id),
                    str(operation_type), json.dumps(checkpoint or {}, sort_keys=True),
                ),
            )
            row = connection.execute(
                "SELECT * FROM roxy_profile_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            connection.commit()
        return self._operation_dict(row), True

    def update_operation(
        self,
        operation_id: str,
        *,
        state: str,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE roxy_profile_operations SET state = ?, checkpoint = ?, error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (
                    str(state), json.dumps(checkpoint or {}, sort_keys=True),
                    str(error)[:1000] if error else None, str(operation_id),
                ),
            )
            row = connection.execute(
                "SELECT * FROM roxy_profile_operations WHERE operation_id = ?",
                (str(operation_id),),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RoxyProfileConflict("Profile operation does not exist")
        return self._operation_dict(row)

    @staticmethod
    def _operation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": row["operation_id"],
            "idempotency_key": row["idempotency_key"],
            "local_id": row["local_id"],
            "operation_type": row["operation_type"],
            "state": row["state"],
            "checkpoint": json.loads(row["checkpoint"] or "{}"),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        local_id: str,
        event_type: str,
        *,
        actor: str,
        operation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        sequence = int(connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM roxy_profile_events "
            "WHERE local_id = ?",
            (str(local_id),),
        ).fetchone()[0])
        connection.execute(
            "INSERT INTO roxy_profile_events "
            "(event_id, local_id, sequence, event_type, operation_id, actor, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex, str(local_id), sequence, str(event_type),
                str(operation_id) if operation_id else None, str(actor),
                json.dumps(payload or {}, sort_keys=True),
            ),
        )
