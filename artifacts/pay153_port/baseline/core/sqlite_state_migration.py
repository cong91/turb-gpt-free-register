"""Audit and migrate the application's split SQLite state databases."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from core import app_state_db

FORMAT_VERSION = 2
MIGRATION_KEY = "migration:fork_state_into_turb:1"
# These tables are created by the fork runtime before the one-time merge. They
# are allowed to overlap, but their schemas still must match exactly.
FORK_OWNED_OVERLAP_TABLES = frozenset({"app_documents", "app_state_migrations"})


class SqliteStateMigrationError(RuntimeError):
    """Base error for SQLite state audit and migration failures."""


class SqliteStateConflict(SqliteStateMigrationError):
    """A target schema or row conflicts with an authoritative source row."""


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SqliteStateMigrationError(f"SQLite source does not exist: {path}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_signature(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SqliteStateMigrationError(f"SQLite source does not exist: {path}")
    sidecars: dict[str, dict[str, Any]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file():
            sidecars[suffix] = {
                "size": sidecar.stat().st_size,
                "sha256": _file_sha256(sidecar),
            }
    return {
        "file_name": path.name,
        "size": path.stat().st_size,
        "sha256": _file_sha256(path),
        "sidecars": sidecars,
    }


def _main_file_signature(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise SqliteStateMigrationError(f"SQLite source does not exist: {path}")
    return path.stat().st_size, _file_sha256(path)


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _digest_rows(connection: sqlite3.Connection, table: str, columns: list[str]) -> str:
    rows = connection.execute(
        f'SELECT * FROM "{table}"'
    ).fetchall()
    encoded = []
    for row in rows:
        item = {column: _encode_value(row[column]) for column in columns}
        encoded.append(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    encoded.sort()
    return hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()


def _table_metadata(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = [dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if sql_row is None:
        raise SqliteStateMigrationError(f"SQLite table is missing: {table}")
    schema_sql = str(sql_row[0] or "")
    return {
        "row_count": int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]),
        "column_names": [str(column["name"]) for column in columns],
        "schema_digest": hashlib.sha256(schema_sql.encode("utf-8")).hexdigest(),
        "row_digest": _digest_rows(connection, table, [str(column["name"]) for column in columns]),
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def _table_schema_signature(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = [
        (
            str(row["name"]),
            str(row["type"] or ""),
            int(row["notnull"]),
            row["dflt_value"],
            int(row["pk"]),
        )
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    ]
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if sql_row is None:
        raise SqliteStateMigrationError(f"SQLite table is missing: {table}")
    normalized_sql = " ".join(str(sql_row[0] or "").split()).casefold()
    return {"columns": columns, "sql": normalized_sql}


def _schema_objects(connection: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    return [
        (str(row["type"]), str(row["name"]), str(row["sql"]))
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name=? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
            "ORDER BY type, name",
            (table,),
        )
    ]


def _merge_table_order(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> tuple[str, ...]:
    """Return a parent-before-child order for the source table graph."""
    pending = set(tables)
    ordered: list[str] = []
    while pending:
        ready = sorted(
            table
            for table in pending
            if all(
                str(row[2]) not in pending
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({_quote_identifier(table)})"
                )
            )
        )
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise SqliteStateConflict(f"SQLite foreign-key cycle prevents table merge: {cycle}")
        ordered.extend(ready)
        pending.difference_update(ready)
    return tuple(ordered)


def _row_values(row: sqlite3.Row, columns: list[str]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


def _canonical_row(columns: list[str], values: tuple[Any, ...]) -> str:
    payload = {
        column: _encode_value(value)
        for column, value in zip(columns, values, strict=True)
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key_digest(columns: list[str], values: tuple[Any, ...]) -> str:
    return _digest_text(_canonical_row(columns, values))


def _copy_table_definitions(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> None:
    for table in tables:
        if not _table_exists(source_connection, table):
            raise SqliteStateMigrationError(f"SQLite source table is missing: {table}")
        source_sql_row = source_connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        source_sql = str(source_sql_row[0] or "") if source_sql_row else ""
        if not source_sql:
            raise SqliteStateMigrationError(f"SQLite table has no create statement: {table}")
        if _table_exists(target_connection, table):
            if _table_schema_signature(source_connection, table) != _table_schema_signature(target_connection, table):
                raise SqliteStateConflict(f"SQLite schema conflict for table {table}")
        else:
            target_connection.execute(source_sql)

    for table in tables:
        for object_type, name, sql in _schema_objects(source_connection, table):
            existing = target_connection.execute(
                "SELECT type, sql FROM sqlite_master WHERE name=?",
                (name,),
            ).fetchone()
            if existing is not None:
                existing_sql = " ".join(str(existing["sql"] or "").split()).casefold()
                if str(existing["type"]) != object_type or existing_sql != " ".join(sql.split()).casefold():
                    raise SqliteStateConflict(f"SQLite schema object conflict: {object_type}:{name}")
                continue
            target_connection.execute(sql)


def _merge_table_rows(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
    table: str,
) -> dict[str, int]:
    source_columns = [
        str(row["name"])
        for row in source_connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    ]
    target_columns = [
        str(row["name"])
        for row in target_connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    ]
    if source_columns != target_columns:
        raise SqliteStateConflict(f"SQLite column conflict for table {table}")

    key_columns = [
        str(row["name"])
        for row in sorted(
            source_connection.execute(f"PRAGMA table_info({_quote_identifier(table)})"),
            key=lambda item: int(item["pk"]),
        )
        if int(row["pk"]) > 0
    ]
    insert_sql = (
        f"INSERT INTO {_quote_identifier(table)} "
        f"({', '.join(_quote_identifier(column) for column in source_columns)}) "
        f"VALUES ({', '.join('?' for _ in source_columns)})"
    )
    inserted = 0
    skipped_identical = 0
    target_rows_by_key: dict[tuple[Any, ...], sqlite3.Row] = {}
    target_row_digests: set[str] = set()
    if key_columns:
        for row in target_connection.execute(f"SELECT * FROM {_quote_identifier(table)}"):
            target_rows_by_key[tuple(row[column] for column in key_columns)] = row
    else:
        for row in target_connection.execute(f"SELECT * FROM {_quote_identifier(table)}"):
            target_row_digests.add(_digest_text(_canonical_row(source_columns, _row_values(row, source_columns))))

    for row in source_connection.execute(f"SELECT * FROM {_quote_identifier(table)}"):
        values = _row_values(row, source_columns)
        existing = None
        if key_columns:
            key_values = tuple(row[column] for column in key_columns)
            existing = target_rows_by_key.get(key_values)
        else:
            row_digest = _digest_text(_canonical_row(source_columns, values))
            if row_digest in target_row_digests:
                skipped_identical += 1
                continue
        if existing is not None:
            existing_values = _row_values(existing, source_columns)
            if _canonical_row(source_columns, existing_values) == _canonical_row(source_columns, values):
                skipped_identical += 1
                continue
            digest = _key_digest(key_columns, tuple(row[column] for column in key_columns))
            raise SqliteStateConflict(f"SQLite row conflict for table {table}, key_digest={digest}")
        target_connection.execute(insert_sql, values)
        inserted += 1
        if key_columns:
            target_rows_by_key[tuple(row[column] for column in key_columns)] = row
        else:
            target_row_digests.add(_digest_text(_canonical_row(source_columns, values)))
    return {"inserted": inserted, "skipped_identical": skipped_identical}


def _validate_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
    foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    foreign_key_errors = [
        {
            "table": str(row[0]),
            "rowid": row[1],
            "parent": str(row[2]),
            "foreign_key_index": row[3],
        }
        for row in foreign_key_rows
    ]
    return {
        "integrity_check": integrity_check,
        "foreign_key_errors": foreign_key_errors,
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
    }


def validate_database(path: str | Path) -> dict[str, Any]:
    """Validate one SQLite file and return non-sensitive metadata."""
    source_path = Path(path)
    with closing(_readonly_connection(source_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        result = _validate_connection(connection)
    result["format_version"] = FORMAT_VERSION
    result["database"] = audit_database(source_path, "database")
    return result


def _backup_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.05)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _remove_sqlite_family(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        if candidate.exists():
            candidate.unlink()


def _assert_target_available(
    app_state_path: Path,
    turb_path: Path,
    target_path: Path,
) -> None:
    source_paths = {app_state_path.resolve(), turb_path.resolve()}
    if target_path.resolve() in source_paths:
        raise SqliteStateMigrationError("Migration target must be different from both source databases")
    if target_path.exists() or any(Path(f"{target_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise SqliteStateMigrationError(f"Migration target already exists: {target_path.name}")


def _snapshot_path(source_path: Path, backup_dir: Path) -> Path:
    return backup_dir / f"{source_path.stem}.snapshot{source_path.suffix or '.sqlite3'}"


def _verify_target_metadata(
    source_audit: dict[str, Any],
    target_audit: dict[str, Any],
) -> dict[str, Any]:
    target_tables = target_audit["tables"]
    app_tables = source_audit["sources"]["app_state"]["tables"]
    turb_tables = source_audit["sources"]["turb"]["tables"]
    fork_owned_overlaps = set(app_tables) & set(turb_tables) & FORK_OWNED_OVERLAP_TABLES

    for table, metadata in turb_tables.items():
        if table in fork_owned_overlaps:
            continue
        actual = target_tables.get(table)
        if actual is None or actual != metadata:
            raise SqliteStateMigrationError(f"Target metadata mismatch for origin table {table}")

    merged_tables: dict[str, dict[str, Any]] = {}
    for table, metadata in app_tables.items():
        actual = target_tables.get(table)
        if actual is None:
            raise SqliteStateMigrationError(f"Target metadata missing for fork table {table}")
        # The migration marker is intentionally added after copying the fork
        # source, so its row digest is expected to change in the target.
        if table not in FORK_OWNED_OVERLAP_TABLES and actual != metadata:
            raise SqliteStateMigrationError(f"Target metadata mismatch for fork table {table}")
        merged_tables[table] = {
            "row_count": actual["row_count"],
            "row_digest": actual["row_digest"],
        }
    return {
        "preserved_origin_tables": list(turb_tables),
        "merged_fork_tables": merged_tables,
        "origin_authority": "turb",
    }


def audit_database(path: str | Path, source_name: str) -> dict[str, Any]:
    """Return non-sensitive schema and row metadata for one SQLite database."""
    source_path = Path(path)
    with closing(_readonly_connection(source_path)) as connection:
        tables = _table_names(connection)
        return {
            "source": source_name,
            "file_name": source_path.name,
            "file_size": source_path.stat().st_size,
            "file_sha256": _file_sha256(source_path),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "tables": {table: _table_metadata(connection, table) for table in tables},
        }


def audit_databases(app_state_path: str | Path, turb_path: str | Path) -> dict[str, Any]:
    """Audit both source databases without exposing row values."""
    app_audit = audit_database(app_state_path, "app_state")
    turb_audit = audit_database(turb_path, "turb")
    app_tables = set(app_audit["tables"])
    turb_tables = set(turb_audit["tables"])
    return {
        "format_version": FORMAT_VERSION,
        "sources": {
            "app_state": app_audit,
            "turb": turb_audit,
        },
        "authoritative_turb_tables": sorted(turb_tables),
        "app_state_extra_tables": sorted(app_tables - turb_tables),
    }


def migrate_databases(
    app_state_path: str | Path,
    turb_path: str | Path,
    target_path: str | Path,
    backup_dir: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a validated unified database from two offline SQLite sources."""
    app_source = Path(app_state_path)
    turb_source = Path(turb_path)
    target = Path(target_path)
    backups = Path(backup_dir)
    _assert_target_available(app_source, turb_source, target)
    app_signature_before = _main_file_signature(app_source)
    turb_signature_before = _main_file_signature(turb_source)
    source_audit = audit_databases(app_source, turb_source)
    app_snapshot = _snapshot_path(app_source, backups)
    turb_snapshot = _snapshot_path(turb_source, backups)
    if app_snapshot.resolve() == turb_snapshot.resolve():
        raise SqliteStateMigrationError("Source snapshot names must be distinct")
    if target.resolve() in {app_snapshot.resolve(), turb_snapshot.resolve()}:
        raise SqliteStateMigrationError("Migration target must be different from backup snapshots")
    if dry_run:
        return {
            "format_version": FORMAT_VERSION,
            "action": "dry-run",
            "source_audit": source_audit,
            "target_file_name": target.name,
            "backup_file_names": [app_snapshot.name, turb_snapshot.name],
        }
    if app_snapshot.exists() or turb_snapshot.exists():
        raise SqliteStateMigrationError("Migration backup snapshot already exists")

    backups.mkdir(parents=True, exist_ok=True)
    target_created = False
    target_connection: sqlite3.Connection | None = None
    try:
        _backup_database(app_source, app_snapshot)
        _backup_database(turb_source, turb_snapshot)
        if _main_file_signature(app_source) != app_signature_before or _main_file_signature(turb_source) != turb_signature_before:
            raise SqliteStateMigrationError("Source database changed during snapshot; no target was promoted")

        target.parent.mkdir(parents=True, exist_ok=True)
        target_created = True
        # Start from origin so every existing turb table and row remains
        # authoritative. Fork state is added only after this snapshot.
        _backup_database(turb_snapshot, target)
        target_connection = app_state_db.connect(target, busy_timeout_ms=30000)
        app_state_db.ensure_schema(target_connection)
        target_connection.execute("BEGIN IMMEDIATE")
        app_tables = tuple(source_audit["sources"]["app_state"]["tables"])
        with closing(_readonly_connection(app_snapshot)) as app_connection:
            _copy_table_definitions(app_connection, target_connection, app_tables)
            merge_order = _merge_table_order(app_connection, app_tables)
            table_merge = {
                table: _merge_table_rows(app_connection, target_connection, table)
                for table in merge_order
            }
        validation_before_marker = _validate_connection(target_connection)
        if validation_before_marker["integrity_check"] != "ok":
            raise SqliteStateMigrationError("Target integrity_check failed before migration marker")
        if validation_before_marker["foreign_key_errors"]:
            raise SqliteStateMigrationError("Target foreign_key_check failed before migration marker")
        target_connection.execute(
            "INSERT INTO app_state_migrations(migration_key, applied_at) "
            "VALUES (?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(migration_key) DO UPDATE SET applied_at=excluded.applied_at",
            (MIGRATION_KEY,),
        )
        target_connection.commit()
        target_connection.close()
        target_connection = None
        validation = validate_database(target)
        if validation["integrity_check"] != "ok" or validation["foreign_key_errors"]:
            raise SqliteStateMigrationError("Target validation failed after migration marker")
        metadata_verification = _verify_target_metadata(
            source_audit,
            validation["database"],
        )
        source_audit_after = audit_databases(app_source, turb_source)
        source_unchanged = (
            _main_file_signature(app_source) == app_signature_before
            and _main_file_signature(turb_source) == turb_signature_before
            and source_audit_after["sources"]["app_state"]["tables"] == source_audit["sources"]["app_state"]["tables"]
            and source_audit_after["sources"]["turb"]["tables"] == source_audit["sources"]["turb"]["tables"]
        )
        if not source_unchanged:
            raise SqliteStateMigrationError("Source database changed after migration; target was not promoted")
        return {
            "format_version": FORMAT_VERSION,
            "action": "migrate",
            "source_audit": source_audit,
            "snapshot_file_names": [app_snapshot.name, turb_snapshot.name],
            "target_file_name": target.name,
            "table_merge": table_merge,
            "validation": validation,
            "metadata_verification": metadata_verification,
            "source_unchanged": source_unchanged,
        }
    except Exception as exc:
        if target_connection is not None:
            target_connection.rollback()
            target_connection.close()
        if target_created:
            _remove_sqlite_family(target)
        if isinstance(exc, sqlite3.IntegrityError):
            raise SqliteStateMigrationError("Target constraint validation failed") from exc
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit or migrate split application SQLite state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="report schema/count/digest metadata")
    audit_parser.add_argument("--app-state", required=True, type=Path)
    audit_parser.add_argument("--turb", required=True, type=Path)

    migrate_parser = subparsers.add_parser("migrate", help="snapshot and merge two SQLite sources")
    migrate_parser.add_argument("--app-state", required=True, type=Path)
    migrate_parser.add_argument("--turb", required=True, type=Path)
    migrate_parser.add_argument("--target", required=True, type=Path)
    migrate_parser.add_argument("--backup-dir", required=True, type=Path)
    migrate_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            report = audit_databases(args.app_state, args.turb)
        else:
            report = migrate_databases(
                args.app_state,
                args.turb,
                args.target,
                args.backup_dir,
                dry_run=args.dry_run,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, SqliteStateMigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
