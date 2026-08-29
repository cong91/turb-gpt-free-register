"""Durable ownership registry for per-profile NordVPN WireGuard leases."""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from core import app_state_db

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nordvpn_wireguard_leases (
    lease_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL UNIQUE,
    profile_id TEXT UNIQUE,
    source_label TEXT NOT NULL UNIQUE,
    local_port INTEGER NOT NULL UNIQUE,
    proxy_url TEXT NOT NULL,
    conf_path TEXT,
    source_temp_conf TEXT,
    wireproxy_pid INTEGER,
    owner_pid INTEGER NOT NULL,
    owner_thread_id INTEGER,
    server_country TEXT,
    server_load INTEGER,
    tunnel_egress_ip TEXT UNIQUE,
    acquired_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS nordvpn_wireguard_leases_owner_pid_idx
    ON nordvpn_wireguard_leases(owner_pid);
"""


def _normalize_source_label(source_label: str) -> str:
    """Use one durable identity for a hostname and its downloaded .conf file."""
    value = str(source_label or "").strip()
    if value.casefold().endswith(".conf"):
        value = value[:-5].rstrip()
    return value.casefold()


class NordVPNWireGuardLeaseStore:
    """Persist one active WireGuard lease per owner, source, port, and profile."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else app_state_db.APP_STATE_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        connection = app_state_db.connect(self.path)
        app_state_db.ensure_schema(connection)
        connection.executescript(_SCHEMA_SQL)
        return connection

    def try_claim(
        self,
        *,
        lease_id: str | None = None,
        owner_id: str,
        source_label: str,
        local_port: int,
        proxy_url: str,
        conf_path: str | None,
        owner_pid: int,
        owner_thread_id: int,
        server_country: str | None = None,
        server_load: int | None = None,
        acquired_at: str,
    ) -> str | None:
        """Atomically claim a source and port, returning None on a uniqueness race."""
        lease_key = str(lease_id or uuid.uuid4())
        values = (
            lease_key,
            str(owner_id),
            _normalize_source_label(source_label),
            int(local_port),
            str(proxy_url),
            str(conf_path) if conf_path else None,
            int(owner_pid),
            int(owner_thread_id),
            server_country,
            server_load,
            str(acquired_at),
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO nordvpn_wireguard_leases "
                    "(lease_id, owner_id, source_label, local_port, proxy_url, conf_path, "
                    "owner_pid, owner_thread_id, server_country, server_load, acquired_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return None
        return lease_key

    def get_owner_lease(self, owner_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM nordvpn_wireguard_leases WHERE owner_id = ?",
                (str(owner_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_lease(self, lease_id: str, **fields: Any) -> bool:
        """Update non-secret runtime metadata, returning False on a unique conflict."""
        allowed = {
            "profile_id",
            "conf_path",
            "source_temp_conf",
            "wireproxy_pid",
            "server_country",
            "server_load",
            "tunnel_egress_ip",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return True
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [values[key] for key in values]
        params.append(str(lease_id))
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    f"UPDATE nordvpn_wireguard_leases SET {assignments}, "
                    "updated_at = CURRENT_TIMESTAMP WHERE lease_id = ?",
                    params,
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release(self, lease_id: str | None) -> bool:
        if not lease_id:
            return False
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM nordvpn_wireguard_leases WHERE lease_id = ?",
                (str(lease_id),),
            )
        return cursor.rowcount > 0

    def list_active(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM nordvpn_wireguard_leases "
                "ORDER BY acquired_at, source_label"
            ).fetchall()
        return [dict(row) for row in rows]

    def active_source_labels(self) -> set[str]:
        return {
            str(row["source_label"])
            for row in self.list_active()
            if str(row.get("source_label") or "").strip()
        }

    def active_local_ports(self) -> set[int]:
        """Return ports already reserved by another live lease."""
        ports: set[int] = set()
        for row in self.list_active():
            try:
                ports.add(int(row["local_port"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ports

    def cleanup_stale(self, is_process_alive: Callable[[int], bool]) -> list[dict[str, Any]]:
        """Remove leases whose owning process no longer exists and return their metadata."""
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM nordvpn_wireguard_leases"
            ).fetchall()]
            stale = [
                row for row in rows
                if not is_process_alive(int(row.get("owner_pid") or 0))
            ]
            if stale:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "DELETE FROM nordvpn_wireguard_leases WHERE lease_id = ?",
                    [(row["lease_id"],) for row in stale],
                )
                connection.execute("COMMIT")
        return stale
