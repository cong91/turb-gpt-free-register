"""Central SQLite state store for application-owned runtime state.

JSON/TXT files remain compatibility exports. Runtime reads and writes use this
database exclusively; exports are never imported implicitly.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The origin database name remains the single runtime source of truth. The
# app-state module owns fork-added schemas, but it does not introduce a second
# runtime database.
APP_STATE_DB_PATH = PROJECT_ROOT / "turb.sqlite3"
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_documents (
    document_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS app_state_migrations (
    migration_key TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def is_app_state_path(path: str | Path) -> bool:
    """Return whether *path* is the canonical application database."""
    return Path(path).resolve() == APP_STATE_DB_PATH.resolve()


def connect(path: str | Path | None = None, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a configured SQLite connection for application state."""
    target = Path(path) if path is not None else APP_STATE_DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        target,
        timeout=max(1, int(busy_timeout_ms)) / 1000,
        isolation_level=None,
        check_same_thread=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {max(1, int(busy_timeout_ms))}")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the central document and migration tables when needed."""
    connection.executescript(_SCHEMA_SQL)


def _document_key(path: str | Path) -> str:
    return str(Path(path).resolve()).casefold()


def get_document(path: str | Path, default: Any = None) -> Any:
    """Read a JSON document from the canonical SQLite database."""
    key = _document_key(path)
    with closing(connect()) as connection:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT payload_json FROM app_documents WHERE document_key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            try:
                return json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return default

        return default


def _write_compatibility_export(target: Path, payload: str) -> None:
    """Atomically refresh an export at its physical filesystem target."""
    # Docker exposes compatibility exports under /app as symlinks into the
    # writable runtime volume. Resolve before creating the sibling temp file,
    # otherwise an atomic write tries to create /app/<file>.tmp on the
    # read-only application filesystem.
    export_target = target.resolve()
    export_target.parent.mkdir(parents=True, exist_ok=True)
    temp = export_target.with_suffix(export_target.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(export_target)


def set_document(path: str | Path, value: Any) -> None:
    """Persist a JSON document in SQLite and refresh its compatibility export."""
    target = Path(path)
    key = _document_key(target)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    with closing(connect()) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT INTO app_documents(document_key, payload_json, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(document_key) DO UPDATE SET "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at "
            "WHERE app_documents.payload_json <> excluded.payload_json",
            (key, payload),
        )
    _write_compatibility_export(target, payload)


def get_named_document(document_key: str, default: Any = None) -> Any:
    """Read a JSON document stored under a logical application key."""
    with closing(connect()) as connection:
        ensure_schema(connection)
        row = connection.execute(
            "SELECT payload_json FROM app_documents WHERE document_key = ?",
            (f"named:{document_key}",),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return default


def set_named_document(document_key: str, value: Any) -> None:
    """Write a JSON document under a logical application key."""
    with closing(connect()) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT INTO app_documents(document_key, payload_json, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(document_key) DO UPDATE SET "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at "
            "WHERE app_documents.payload_json <> excluded.payload_json",
            (f"named:{document_key}", json.dumps(value, ensure_ascii=False)),
        )


def get_component_version(connection: sqlite3.Connection, component: str) -> int:
    """Read a component-local schema version from the central migration table."""
    ensure_schema(connection)
    row = connection.execute(
        "SELECT migration_key FROM app_state_migrations WHERE migration_key LIKE ? "
        "ORDER BY migration_key DESC LIMIT 1",
        (f"schema:{component}:%",),
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(str(row[0]).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return 0


def set_component_version(connection: sqlite3.Connection, component: str, version: int) -> None:
    ensure_schema(connection)
    connection.execute(
        "DELETE FROM app_state_migrations WHERE migration_key LIKE ?",
        (f"schema:{component}:%",),
    )
    connection.execute(
        "INSERT INTO app_state_migrations(migration_key, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
        (f"schema:{component}:{int(version)}",),
    )
