# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from core import app_state_db


class OtpIdentityStoreError(RuntimeError):
    """OTP identity store cannot be initialized or updated."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS otp_identities (
    provider TEXT NOT NULL,
    cdk_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, cdk_fingerprint, identity_digest)
);
"""


class OtpIdentityStore:
    """Durable one-time claim store for provider OTP identities."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 3000):
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    @staticmethod
    def fingerprint(provider: str, raw_cdk: str) -> str:
        provider_name = str(provider or "").strip().lower()
        canonical = str(raw_cdk or "").strip().upper()
        if not provider_name or not canonical:
            raise OtpIdentityStoreError("Provider and CDK are required")
        return "sha256:" + hashlib.sha256(
            f"{provider_name}:{canonical}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def digest_identity(provider: str, identity: str) -> str:
        provider_name = str(provider or "").strip().lower()
        value = str(identity or "").strip()
        if not provider_name or not value:
            raise OtpIdentityStoreError("Provider and OTP identity are required")
        return "sha256:" + hashlib.sha256(
            f"{provider_name}:{value}".encode("utf-8")
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        if app_state_db.is_app_state_path(self.path):
            app_state_db.ensure_schema(connection)
        connection.execute(_SCHEMA_SQL)
        return connection

    def claim_if_unseen(self, provider: str, cdk_fingerprint: str, identity: str) -> bool:
        provider_name = str(provider or "").strip().lower()
        fingerprint = str(cdk_fingerprint or "").strip()
        digest = self.digest_identity(provider_name, identity)
        if not provider_name or not fingerprint:
            raise OtpIdentityStoreError("Provider and CDK fingerprint are required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "INSERT OR IGNORE INTO otp_identities "
                "(provider, cdk_fingerprint, identity_digest) VALUES (?, ?, ?)",
                (provider_name, fingerprint, digest),
            )
            connection.commit()
            return result.rowcount == 1
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise OtpIdentityStoreError("Unable to claim OTP identity") from exc
        finally:
            connection.close()

    def claim_with_snapshot(
        self,
        provider: str,
        cdk_fingerprint: str,
        *,
        claim_identities: list[str] | set[str] | tuple[str, ...],
        observed_identities: list[str] | set[str] | tuple[str, ...],
    ) -> bool:
        provider_name = str(provider or "").strip().lower()
        fingerprint = str(cdk_fingerprint or "").strip()
        claim_digests = self._digests(provider_name, claim_identities)
        observed_digests = self._digests(provider_name, observed_identities)
        if not provider_name or not fingerprint:
            raise OtpIdentityStoreError("Provider and CDK fingerprint are required")
        if not claim_digests:
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in claim_digests)
            found = connection.execute(
                "SELECT 1 FROM otp_identities WHERE provider = ? AND cdk_fingerprint = ? "
                f"AND identity_digest IN ({placeholders}) LIMIT 1",
                (provider_name, fingerprint, *claim_digests),
            ).fetchone()
            connection.executemany(
                "INSERT OR IGNORE INTO otp_identities "
                "(provider, cdk_fingerprint, identity_digest) VALUES (?, ?, ?)",
                [
                    (provider_name, fingerprint, digest)
                    for digest in dict.fromkeys([*claim_digests, *observed_digests])
                ],
            )
            connection.commit()
            return found is None
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise OtpIdentityStoreError("Unable to claim OTP snapshot") from exc
        finally:
            connection.close()

    @classmethod
    def _digests(
        cls,
        provider: str,
        identities: list[str] | set[str] | tuple[str, ...],
    ) -> list[str]:
        values = dict.fromkeys(str(item or "").strip() for item in identities)
        return [cls.digest_identity(provider, value) for value in values if value]

    def remember_many(
        self,
        provider: str,
        cdk_fingerprint: str,
        identities: list[str] | set[str] | tuple[str, ...],
    ) -> int:
        provider_name = str(provider or "").strip().lower()
        fingerprint = str(cdk_fingerprint or "").strip()
        digests = self._digests(provider_name, identities)
        if not provider_name or not fingerprint:
            raise OtpIdentityStoreError("Provider and CDK fingerprint are required")
        if not digests:
            return 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO otp_identities "
                "(provider, cdk_fingerprint, identity_digest) VALUES (?, ?, ?)",
                [
                    (provider_name, fingerprint, digest)
                    for digest in dict.fromkeys(digests)
                ],
            )
            remembered = connection.total_changes - before
            connection.commit()
            return remembered
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise OtpIdentityStoreError("Unable to remember OTP identities") from exc
        finally:
            connection.close()
