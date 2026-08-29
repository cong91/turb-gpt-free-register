"""Durable local state for rotating-proxy keys and lane leases."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from core import app_state_db

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rotating_proxy_keys (
    rotating_key TEXT PRIMARY KEY,
    key_expires_at REAL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rotating_proxy_leases (
    lane_id INTEGER PRIMARY KEY,
    rotating_key TEXT NOT NULL,
    proxy_url TEXT NOT NULL,
    proxy_expires_at REAL NOT NULL,
    key_expires_at REAL,
    assigned_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rotating_proxy_leases_key_idx
    ON rotating_proxy_leases(rotating_key);
CREATE TABLE IF NOT EXISTS rotating_proxy_scoped_leases (
    lane_scope TEXT NOT NULL,
    lane_id INTEGER NOT NULL,
    rotating_key TEXT NOT NULL UNIQUE,
    proxy_url TEXT NOT NULL,
    proxy_expires_at REAL NOT NULL,
    key_expires_at REAL,
    assigned_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(lane_scope, lane_id)
);
CREATE INDEX IF NOT EXISTS rotating_proxy_scoped_leases_key_idx
    ON rotating_proxy_scoped_leases(rotating_key);
"""


class RotatingProxyStore:
    """Persist rotating-proxy inventory and one lease per scoped worker lane."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else app_state_db.APP_STATE_DB_PATH

    def _connect(self):
        connection = app_state_db.connect(self.path)
        app_state_db.ensure_schema(connection)
        connection.executescript(_SCHEMA_SQL)
        connection.execute(
            "INSERT OR IGNORE INTO rotating_proxy_scoped_leases "
            "(lane_scope, lane_id, rotating_key, proxy_url, proxy_expires_at, "
            "key_expires_at, assigned_at, updated_at) "
            "SELECT 'registration', lane_id, rotating_key, proxy_url, proxy_expires_at, "
            "key_expires_at, assigned_at, updated_at FROM rotating_proxy_leases"
        )
        return connection

    @staticmethod
    def _scope(scope: str) -> str:
        value = str(scope or "registration").strip()
        if not value:
            raise ValueError("proxy lane scope không được để trống")
        return value

    def sync_keys(self, keys: list[dict[str, Any]]) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            for item in keys:
                rotating_key = str(item.get("key") or "").strip()
                if not rotating_key:
                    continue
                expires_at = item.get("expires_at")
                connection.execute(
                    "INSERT INTO rotating_proxy_keys "
                    "(rotating_key, key_expires_at, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(rotating_key) DO UPDATE SET "
                    "key_expires_at = COALESCE(excluded.key_expires_at, rotating_proxy_keys.key_expires_at), "
                    "last_seen_at = excluded.last_seen_at",
                    (rotating_key, expires_at, now, now),
                )

    def upsert_key(self, key: str, expires_at: float | None) -> None:
        self.sync_keys([{"key": key, "expires_at": expires_at}])

    def set_key_expiration(self, key: str, expires_at: float | None) -> None:
        """Set an authoritative expiry, including clearing an expired value."""
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO rotating_proxy_keys "
                "(rotating_key, key_expires_at, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(rotating_key) DO UPDATE SET "
                "key_expires_at = excluded.key_expires_at, last_seen_at = excluded.last_seen_at",
                (str(key), expires_at, now, now),
            )

    def get_key(self, key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT rotating_key, key_expires_at, first_seen_at, last_seen_at "
                "FROM rotating_proxy_keys WHERE rotating_key = ?",
                (str(key),),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_keys(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT rotating_key, key_expires_at, first_seen_at, last_seen_at "
                "FROM rotating_proxy_keys ORDER BY first_seen_at, rotating_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_lease(
        self,
        lane_id: int,
        *,
        scope: str = "registration",
        rotating_key: str,
        proxy_url: str,
        proxy_expires_at: float,
        key_expires_at: float | None,
        assigned_at: float | None = None,
    ) -> None:
        now = time.time()
        assigned = now if assigned_at is None else float(assigned_at)
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO rotating_proxy_scoped_leases "
                "(lane_scope, lane_id, rotating_key, proxy_url, proxy_expires_at, key_expires_at, assigned_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(lane_scope, lane_id) DO UPDATE SET "
                "rotating_key = excluded.rotating_key, proxy_url = excluded.proxy_url, "
                "proxy_expires_at = excluded.proxy_expires_at, key_expires_at = excluded.key_expires_at, "
                "updated_at = excluded.updated_at",
                (
                    self._scope(scope),
                    int(lane_id),
                    str(rotating_key),
                    str(proxy_url),
                    float(proxy_expires_at),
                    key_expires_at,
                    assigned,
                    now,
                ),
            )

    def try_upsert_lease(
        self,
        lane_id: int,
        *,
        scope: str = "registration",
        rotating_key: str,
        proxy_url: str,
        proxy_expires_at: float,
        key_expires_at: float | None,
        assigned_at: float | None = None,
    ) -> bool:
        """Persist a lease, returning false when another scope claimed the key first."""
        try:
            self.upsert_lease(
                lane_id,
                scope=scope,
                rotating_key=rotating_key,
                proxy_url=proxy_url,
                proxy_expires_at=proxy_expires_at,
                key_expires_at=key_expires_at,
                assigned_at=assigned_at,
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_lease(self, lane_id: int, *, scope: str = "registration") -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT lane_scope, lane_id, rotating_key, proxy_url, proxy_expires_at, key_expires_at, "
                "assigned_at, updated_at FROM rotating_proxy_scoped_leases "
                "WHERE lane_scope = ? AND lane_id = ?",
                (self._scope(scope), int(lane_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_leases(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT lane_scope, lane_id, rotating_key, proxy_url, proxy_expires_at, key_expires_at, "
                "assigned_at, updated_at FROM rotating_proxy_scoped_leases "
                "ORDER BY lane_scope, lane_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def lanes_for_key(self, rotating_key: str) -> list[int]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT lane_id FROM rotating_proxy_scoped_leases "
                "WHERE rotating_key = ? ORDER BY lane_scope, lane_id",
                (str(rotating_key),),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def scoped_lanes_for_key(self, rotating_key: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT lane_scope, lane_id FROM rotating_proxy_scoped_leases "
                "WHERE rotating_key = ? ORDER BY lane_scope, lane_id",
                (str(rotating_key),),
            ).fetchall()
        return [dict(row) for row in rows]
