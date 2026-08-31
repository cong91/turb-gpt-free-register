"""Durable country outcomes for HeroSMS Codex phone verification."""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

from config import codex as _cfg
from core import app_state_db

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hero_sms_country_records (
    profile_key TEXT NOT NULL,
    country_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('blocked', 'verified')),
    verified_price TEXT NOT NULL DEFAULT '',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    number_rejected_count INTEGER NOT NULL DEFAULT 0,
    last_failure_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (profile_key, country_id)
);
"""


class HeroSmsCountryStoreError(RuntimeError):
    """The durable HeroSMS country state could not be read or updated."""


DEFAULT_MIN_ATTEMPTS = 4
DEFAULT_HIGH_FAILURE_RATE = 0.75


def _clean(value: object) -> str:
    return str(value or "").strip()


def make_profile_key(
    api_base: object,
    service: object,
    max_price: object,
    *,
    lane_key: object = "",
) -> str:
    """Build a non-secret key for one HeroSMS price/acquisition profile and lane."""
    parts = [_clean(api_base), _clean(service), _clean(max_price)]
    lane = _clean(lane_key)
    if lane:
        parts.append(lane)
    return "\x1f".join(parts)


def _normalize_price(value: object) -> str:
    raw = _clean(value)
    if not raw:
        raise HeroSmsCountryStoreError("verified price is required")
    try:
        price = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise HeroSmsCountryStoreError(f"verified price is invalid: {raw!r}") from exc
    if not price.is_finite() or price < 0:
        raise HeroSmsCountryStoreError(f"verified price is invalid: {raw!r}")
    return format(price, "f")


def _failure_policy() -> tuple[int, float]:
    try:
        min_attempts = max(
            1,
            int(getattr(_cfg, "HERO_SMS_COUNTRY_MIN_ATTEMPTS", DEFAULT_MIN_ATTEMPTS)),
        )
    except (TypeError, ValueError):
        min_attempts = DEFAULT_MIN_ATTEMPTS
    try:
        high_failure_rate = float(
            getattr(_cfg, "HERO_SMS_COUNTRY_HIGH_FAILURE_RATE", DEFAULT_HIGH_FAILURE_RATE)
        )
    except (TypeError, ValueError):
        high_failure_rate = DEFAULT_HIGH_FAILURE_RATE
    if not math.isfinite(high_failure_rate) or not 0 < high_failure_rate <= 1:
        high_failure_rate = DEFAULT_HIGH_FAILURE_RATE
    return min_attempts, high_failure_rate


def _number_reject_threshold() -> int:
    try:
        return max(1, int(getattr(_cfg, "HERO_SMS_NUMBER_REJECT_THRESHOLD", 3)))
    except (TypeError, ValueError):
        return 3


def _is_high_failure(
    success_count: int,
    failure_count: int,
    *,
    min_attempts: int,
    high_failure_rate: float,
) -> bool:
    total = success_count + failure_count
    return total >= min_attempts and failure_count / total >= high_failure_rate


class HeroSmsCountryStore:
    """Persist HeroSMS country outcomes in the application state DB."""

    def __init__(self, path: str | Path | None = None, *, busy_timeout_ms: int = 5000):
        self.path = Path(path) if path is not None else app_state_db.APP_STATE_DB_PATH
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        connection = app_state_db.connect(self.path, busy_timeout_ms=self.busy_timeout_ms)
        app_state_db.ensure_schema(connection)
        connection.executescript(_SCHEMA_SQL)
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(hero_sms_country_records)"
            ).fetchall()
        }
        if "number_rejected_count" not in columns:
            try:
                connection.execute(
                    "ALTER TABLE hero_sms_country_records "
                    "ADD COLUMN number_rejected_count INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError as exc:
                # Another worker may have completed this additive migration
                # between PRAGMA and ALTER TABLE.
                if "duplicate column name" not in str(exc).lower():
                    raise
        return connection

    def country_health(self, profile_key: str) -> dict[str, dict[str, object]]:
        key = _clean(profile_key)
        if not key:
            return {}
        min_attempts, high_failure_rate = _failure_policy()
        reject_threshold = _number_reject_threshold()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT country_id, success_count, failure_count, number_rejected_count, verified_price "
                    "FROM hero_sms_country_records WHERE profile_key = ?",
                    (key,),
                ).fetchall()
            result: dict[str, dict[str, object]] = {}
            for row in rows:
                country = str(row[0])
                success_count = int(row[1] or 0)
                failure_count = int(row[2] or 0)
                number_rejected_count = int(row[3] or 0)
                total = success_count + failure_count
                result[country] = {
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "number_rejected_count": number_rejected_count,
                    "failure_rate": failure_count / total if total else 0.0,
                    "verified_price": str(row[4] or ""),
                    "high_failure": _is_high_failure(
                        success_count,
                        failure_count,
                        min_attempts=min_attempts,
                        high_failure_rate=high_failure_rate,
                    ) or number_rejected_count >= reject_threshold,
                }
            return result
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to read HeroSMS country health") from exc

    def country_health_for_provider(self, api_base: object, service: object) -> dict[str, dict[str, object]]:
        """Aggregate country outcomes across all observed price profiles."""
        base = _clean(api_base)
        service_code = _clean(service)
        if not base or not service_code:
            return {}
        profile_prefix = f"{base}\x1f{service_code}"
        min_attempts, high_failure_rate = _failure_policy()
        reject_threshold = _number_reject_threshold()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT profile_key, country_id, verified_price, success_count, "
                    "failure_count, number_rejected_count, last_failure_reason, updated_at "
                    "FROM hero_sms_country_records"
                ).fetchall()
            aggregate: dict[str, dict[str, object]] = {}
            for row in rows:
                row_profile = str(row[0] or "")
                if row_profile != profile_prefix and not row_profile.startswith(f"{profile_prefix}\x1f"):
                    continue
                country = str(row[1])
                item = aggregate.setdefault(
                    country,
                    {
                        "success_count": 0,
                        "failure_count": 0,
                        "number_rejected_count": 0,
                        "verified_price": "",
                        "last_failure_reason": "",
                        "updated_at": "",
                    },
                )
                item["success_count"] = int(item["success_count"]) + int(row[3] or 0)
                item["failure_count"] = int(item["failure_count"]) + int(row[4] or 0)
                item["number_rejected_count"] = int(item["number_rejected_count"]) + int(row[5] or 0)
                updated_at = str(row[7] or "")
                if updated_at >= str(item["updated_at"]):
                    item["updated_at"] = updated_at
                    item["verified_price"] = str(row[2] or "")
                    item["last_failure_reason"] = str(row[6] or "")
            for item in aggregate.values():
                success_count = int(item["success_count"])
                failure_count = int(item["failure_count"])
                total = success_count + failure_count
                item["failure_rate"] = failure_count / total if total else 0.0
                item["high_failure"] = _is_high_failure(
                    success_count,
                    failure_count,
                    min_attempts=min_attempts,
                    high_failure_rate=high_failure_rate,
                ) or int(item["number_rejected_count"]) >= reject_threshold
            return aggregate
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to read aggregate HeroSMS country health") from exc

    def blocked_countries(self, profile_key: str) -> set[str]:
        """Return countries whose observed failure rate is currently high."""
        return {
            country
            for country, health in self.country_health(profile_key).items()
            if health["high_failure"]
        }

    def verified_countries(self, profile_key: str) -> dict[str, str]:
        """Return successful countries that are not currently high risk."""
        return {
            country: str(health["verified_price"])
            for country, health in self.country_health(profile_key).items()
            if int(health["success_count"]) > 0 and not health["high_failure"]
        }

    def sticky_countries(self, profile_key: str) -> list[str]:
        """Return the lane's most recently successful low-risk countries first."""
        key = _clean(profile_key)
        if not key:
            return []
        min_attempts, high_failure_rate = _failure_policy()
        reject_threshold = _number_reject_threshold()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT country_id, success_count, failure_count, "
                    "number_rejected_count, last_failure_reason, updated_at "
                    "FROM hero_sms_country_records WHERE profile_key = ?",
                    (key,),
                ).fetchall()
            return [
                str(row[0])
                for row in sorted(rows, key=lambda row: str(row[5] or ""), reverse=True)
                if int(row[1] or 0) > 0
                and not _is_high_failure(
                    int(row[1] or 0),
                    int(row[2] or 0),
                    min_attempts=min_attempts,
                    high_failure_rate=high_failure_rate,
                )
                and int(row[3] or 0) < reject_threshold
                and not str(row[4] or "")
            ]
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to read sticky HeroSMS countries") from exc

    def blocked_countries_for_provider(self, api_base: object, service: object) -> set[str]:
        """Return high-risk countries across all price profiles for a provider."""
        return {
            country
            for country, health in self.country_health_for_provider(api_base, service).items()
            if health["high_failure"]
        }

    def verified_countries_for_provider(self, api_base: object, service: object) -> dict[str, str]:
        """Return successful low-risk countries across all price profiles."""
        return {
            country: str(health["verified_price"])
            for country, health in self.country_health_for_provider(api_base, service).items()
            if int(health["success_count"]) > 0 and not health["high_failure"]
        }

    def sticky_countries_for_provider(self, api_base: object, service: object) -> list[str]:
        """Return countries whose latest provider outcome was successful, newest first."""
        health = self.country_health_for_provider(api_base, service)
        return [
            country
            for country, item in sorted(
                health.items(),
                key=lambda pair: str(pair[1]["updated_at"]),
                reverse=True,
            )
            if int(item["success_count"]) > 0
            and not item["high_failure"]
            and not str(item["last_failure_reason"] or "")
        ]

    def mark_unusable(self, profile_key: str, country_id: str, reason: str = "") -> bool:
        """Record one failed verification and recompute the country risk state."""
        key = _clean(profile_key)
        country = _clean(country_id)
        if not key or not country:
            return False
        detail = _clean(reason)[:500]
        min_attempts, high_failure_rate = _failure_policy()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT success_count, failure_count, number_rejected_count "
                    "FROM hero_sms_country_records "
                    "WHERE profile_key = ? AND country_id = ?",
                    (key, country),
                ).fetchone()
                success_count = int(row[0] or 0) if row is not None else 0
                failure_count = (int(row[1] or 0) if row is not None else 0) + 1
                number_rejected_count = int(row[2] or 0) if row is not None else 0
                reject_threshold = _number_reject_threshold()
                state = "blocked" if _is_high_failure(
                    success_count,
                    failure_count,
                    min_attempts=min_attempts,
                    high_failure_rate=high_failure_rate,
                ) or number_rejected_count >= reject_threshold else "verified"
                if row is None:
                    connection.execute(
                        "INSERT INTO hero_sms_country_records "
                        "(profile_key, country_id, state, failure_count, last_failure_reason) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (key, country, state, failure_count, detail),
                    )
                else:
                    connection.execute(
                        "UPDATE hero_sms_country_records SET state = ?, failure_count = ?, "
                        "last_failure_reason = ?, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                        "WHERE profile_key = ? AND country_id = ?",
                        (state, failure_count, detail, key, country),
                    )
                connection.commit()
            return True
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to persist unusable HeroSMS country") from exc

    def mark_number_rejected(self, profile_key: str, country_id: str, reason: str) -> bool:
        """Record repeated used-phone rejections for one HeroSMS country."""
        key = _clean(profile_key)
        country = _clean(country_id)
        if not key or not country:
            return False
        detail = _clean(reason)[:500]
        min_attempts, high_failure_rate = _failure_policy()
        reject_threshold = _number_reject_threshold()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT success_count, failure_count, number_rejected_count "
                    "FROM hero_sms_country_records WHERE profile_key = ? AND country_id = ?",
                    (key, country),
                ).fetchone()
                success_count = int(row[0] or 0) if row is not None else 0
                # Keep pool-exhaustion evidence separate from ordinary OTP/send
                # failures so one provider-stock issue does not poison the
                # generic country failure-rate health score.
                failure_count = int(row[1] or 0) if row is not None else 0
                number_rejected_count = (int(row[2] or 0) if row is not None else 0) + 1
                state = "blocked" if (
                    _is_high_failure(
                        success_count,
                        failure_count,
                        min_attempts=min_attempts,
                        high_failure_rate=high_failure_rate,
                    ) or number_rejected_count >= reject_threshold
                ) else "verified"
                if row is None:
                    connection.execute(
                        "INSERT INTO hero_sms_country_records "
                        "(profile_key, country_id, state, failure_count, number_rejected_count, "
                        "last_failure_reason) VALUES (?, ?, ?, ?, ?, ?)",
                        (key, country, state, failure_count, number_rejected_count, detail),
                    )
                else:
                    connection.execute(
                        "UPDATE hero_sms_country_records SET state = ?, failure_count = ?, "
                        "number_rejected_count = ?, last_failure_reason = ?, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                        "WHERE profile_key = ? AND country_id = ?",
                        (state, failure_count, number_rejected_count, detail, key, country),
                    )
                connection.commit()
            return True
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to persist rejected HeroSMS number") from exc

    def mark_verified(self, profile_key: str, country_id: str, price: object) -> bool:
        """Record one successful verification and recompute the country risk state."""
        key = _clean(profile_key)
        country = _clean(country_id)
        if not key or not country:
            return False
        normalized_price = _normalize_price(price)
        min_attempts, high_failure_rate = _failure_policy()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT success_count, failure_count FROM hero_sms_country_records "
                    "WHERE profile_key = ? AND country_id = ?",
                    (key, country),
                ).fetchone()
                success_count = (int(row[0] or 0) if row is not None else 0) + 1
                failure_count = int(row[1] or 0) if row is not None else 0
                state = "blocked" if _is_high_failure(
                    success_count,
                    failure_count,
                    min_attempts=min_attempts,
                    high_failure_rate=high_failure_rate,
                ) else "verified"
                if row is None:
                    connection.execute(
                        "INSERT INTO hero_sms_country_records "
                        "(profile_key, country_id, state, verified_price, success_count, "
                        "number_rejected_count) VALUES (?, ?, ?, ?, ?, 0)",
                        (key, country, state, normalized_price, success_count),
                    )
                else:
                    connection.execute(
                        "UPDATE hero_sms_country_records SET state = ?, verified_price = ?, "
                        "success_count = ?, number_rejected_count = 0, last_failure_reason = '', "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                        "WHERE profile_key = ? AND country_id = ?",
                        (state, normalized_price, success_count, key, country),
                    )
                connection.commit()
            return True
        except sqlite3.DatabaseError as exc:
            raise HeroSmsCountryStoreError("Unable to persist verified HeroSMS country") from exc
