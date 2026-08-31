"""Coordinate rotating-proxy keys, proxy TTLs, and scoped workflow lanes."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.rotating_proxy_client import RotatingProxyClient
from core.rotating_proxy_store import RotatingProxyStore

logger = logging.getLogger(__name__)
_PURCHASED_KEY_TTL_SECONDS = 24 * 60 * 60
_COOLDOWN_SECONDS_RE = re.compile(r"\b(?:con|còn)\s+(\d+)\s*s\b", re.IGNORECASE)


class RotatingProxyError(RuntimeError):
    """A rotating-proxy lease cannot be acquired or refreshed."""


@dataclass(frozen=True)
class RotatingProxyLease:
    scope: str
    lane_id: int
    key: str
    proxy_url: str
    proxy_expires_at: float
    key_expires_at: float | None


def _mask_key(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return text[:2] + "..." + text[-2:]
    return text[:5] + "..." + text[-4:]


def _mask_proxy(value: object) -> str:
    text = str(value or "")
    if "://" not in text or "@" not in text:
        return text
    scheme, remainder = text.split("://", 1)
    _, host = remainder.rsplit("@", 1)
    return f"{scheme}://***:***@{host}"


class RotatingProxyManager:
    """Allocate one provider key per scoped lane and reuse it until TTL expiry."""

    def __init__(
        self,
        *,
        client: RotatingProxyClient | Any | None = None,
        store: RotatingProxyStore | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.client = client or RotatingProxyClient()
        self.store = store or RotatingProxyStore()
        self.clock = clock or time.time
        self._lock = threading.RLock()

    @staticmethod
    def _lane_id(lane_id: int) -> int:
        try:
            value = int(lane_id)
        except (TypeError, ValueError) as exc:
            raise RotatingProxyError("proxy lane_id phải là số nguyên không âm") from exc
        if value < 0:
            raise RotatingProxyError("proxy lane_id phải là số nguyên không âm")
        return value

    @staticmethod
    def _scope(scope: str) -> str:
        value = str(scope or "registration").strip()
        if not value:
            raise RotatingProxyError("proxy lane scope không được để trống")
        return value

    def _key_expired(self, key_info: dict[str, Any] | None) -> bool:
        expires_at = key_info.get("key_expires_at") if key_info else None
        return expires_at is not None and float(expires_at) <= self.clock()

    def _lease_active(self, lease: dict[str, Any]) -> bool:
        try:
            if float(lease.get("proxy_expires_at") or 0) <= self.clock():
                return False
        except (TypeError, ValueError):
            return False
        key = str(lease.get("rotating_key") or "").strip()
        return bool(key) and not self._key_expired(self.store.get_key(key))

    def _proxy_healthy(self, proxy_url: str) -> bool:
        checker = getattr(self.client, "check_proxy", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(proxy_url))
        except Exception as exc:  # noqa: BLE001 - a failed health probe means unhealthy.
            logger.warning(
                "[RotatingProxy] proxy health-check failed: %s: %s",
                type(exc).__name__,
                str(exc)[:180],
            )
            return False

    @staticmethod
    def _cooldown_seconds(error: Exception) -> int | None:
        match = _COOLDOWN_SECONDS_RE.search(str(error or ""))
        return max(1, int(match.group(1))) if match else None

    def _refresh_keys(self) -> list[dict[str, Any]]:
        try:
            keys = list(self.client.list_keys())
        except Exception as exc:
            raise RotatingProxyError(f"无法获取 proxy.vn keyxoay 列表: {exc}") from exc
        self.store.sync_keys(keys)
        refreshed_key_set = {
            str(item.get("key") or "").strip()
            for item in keys
            if str(item.get("key") or "").strip()
        }
        expired_at = self.clock()
        for item in self.store.list_keys():
            key = str(item.get("rotating_key") or "").strip()
            if key and key not in refreshed_key_set:
                self.store.set_key_expiration(key, expired_at)
        return keys

    def _choose_available_key(self, excluded: set[str]) -> dict[str, Any] | None:
        for item in self.store.list_keys():
            key = str(item.get("rotating_key") or "").strip()
            if key and key not in excluded and not self._key_expired(item):
                return item
        return None

    def _key_for_lane(
        self,
        lease: dict[str, Any] | None,
        *,
        excluded: set[str] | None = None,
    ) -> dict[str, Any]:
        if lease:
            key = str(lease.get("rotating_key") or "").strip()
            if key:
                info = self.store.get_key(key)
                if not self._key_expired(info):
                    return info or {"rotating_key": key, "key_expires_at": None}

        used_keys = {
            str(item.get("rotating_key") or "")
            for item in self.store.list_leases()
            if self._lease_active(item)
        }
        used_keys.update(excluded or set())
        candidate = self._choose_available_key(used_keys)
        if candidate is None:
            self._refresh_keys()
            candidate = self._choose_available_key(used_keys)
        if candidate is not None:
            return candidate
        try:
            return self._purchase_and_store(1)[0]
        except Exception as exc:
            raise RotatingProxyError(f"没有可用 keyxoay，购买新 key thất bại: {exc}") from exc

    def _purchase_and_store(self, quantity: int) -> list[dict[str, Any]]:
        if quantity < 1:
            return []
        raw_items = (
            [self.client.purchase_key()]
            if quantity == 1
            else list(self.client.purchase_keys(quantity))
        )
        purchased: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            expires_at = item.get("expires_at")
            if expires_at is None:
                expires_at = self.clock() + _PURCHASED_KEY_TTL_SECONDS
            self.store.upsert_key(key, expires_at)
            purchased.append(self.store.get_key(key) or {
                "rotating_key": key,
                "key_expires_at": expires_at,
            })
        if not purchased:
            raise RotatingProxyError("proxy.vn mua key thành công nhưng không có keyxoay")
        return purchased

    def ensure_key_inventory(self, lane_count: int, *, scope: str = "registration") -> list[dict[str, Any]]:
        """Ensure enough active, unclaimed keyxoay values exist for the requested lanes."""
        try:
            target = int(lane_count)
        except (TypeError, ValueError) as exc:
            raise RotatingProxyError("proxy lane_count phải là số nguyên dương") from exc
        if target < 1:
            raise RotatingProxyError("proxy lane_count phải là số nguyên dương")
        lane_scope = self._scope(scope)
        with self._lock:
            self.store.delete_expired_leases(self.clock())
            refreshed = self._refresh_keys()
            ordered_keys = [
                str(item.get("key") or "").strip()
                for item in refreshed
                if str(item.get("key") or "").strip()
            ]
            active_keys = {
                key
                for key in ordered_keys
                if not self._key_expired(self.store.get_key(key))
            }
            occupied_keys = set()
            for lease in self.store.list_leases():
                key = str(lease.get("rotating_key") or "").strip()
                if key not in active_keys or not self._lease_active(lease):
                    continue
                same_target_lane = (
                    str(lease.get("lane_scope") or "registration") == lane_scope
                    and 0 <= int(lease.get("lane_id") or 0) < target
                )
                if not same_target_lane:
                    occupied_keys.add(key)

            missing = max(0, target - len(active_keys - occupied_keys))
            known_keys = {str(item.get("rotating_key") or "").strip() for item in self.store.list_keys()}
            while missing:
                purchased = self._purchase_and_store(missing)
                new_keys = []
                for item in purchased:
                    key = str(item.get("rotating_key") or "").strip()
                    if key and key not in known_keys:
                        known_keys.add(key)
                        active_keys.add(key)
                        ordered_keys.append(key)
                        new_keys.append(key)
                if not new_keys:
                    raise RotatingProxyError("proxy.vn mua key thành công nhưng không bổ sung keyxoay mới")
                missing -= len(new_keys)

            available = active_keys - occupied_keys
            if len(available) < target:
                raise RotatingProxyError(
                    f"keyxoay khả dụng không đủ cho {target} lane, hiện chỉ có {len(available)} key"
                )
            return [
                self.store.get_key(key) or {"rotating_key": key, "key_expires_at": None}
                for key in ordered_keys
                if key in available
            ]

    def acquire(self, lane_id: int, *, scope: str = "registration") -> RotatingProxyLease:
        lane = self._lane_id(lane_id)
        lane_scope = self._scope(scope)
        with self._lock:
            now = self.clock()
            previous = self.store.get_lease(lane, scope=lane_scope)
            previous_key = str(previous.get("rotating_key") or "").strip() if previous else ""
            previous_key_info = self.store.get_key(previous_key) if previous_key else None
            fallback = None
            if (
                previous
                and str(previous.get("proxy_url") or "").strip()
                and float(previous.get("proxy_expires_at") or 0) <= now
                and not self._key_expired(previous_key_info)
                and self._proxy_healthy(str(previous["proxy_url"]))
            ):
                fallback = previous

            self.store.delete_expired_leases(now)
            existing = self.store.get_lease(lane, scope=lane_scope)
            existing_key = str(existing.get("rotating_key") or "").strip() if existing else ""
            key_info = self.store.get_key(existing_key) if existing_key else None
            if (
                existing
                and float(existing.get("proxy_expires_at") or 0) > now
                and not self._key_expired(key_info)
            ):
                if self._proxy_healthy(str(existing["proxy_url"])):
                    return RotatingProxyLease(
                        scope=lane_scope,
                        lane_id=lane,
                        key=str(existing["rotating_key"]),
                        proxy_url=str(existing["proxy_url"]),
                        proxy_expires_at=float(existing["proxy_expires_at"]),
                        key_expires_at=existing.get("key_expires_at"),
                    )
                self.store.delete_lease(
                    lane,
                    scope=lane_scope,
                    rotating_key=str(existing["rotating_key"]),
                    proxy_url=str(existing["proxy_url"]),
                )
                existing = None

            excluded_keys: set[str] = set()
            failed_attempts: dict[str, int] = {}
            last_error: Exception | None = None
            for _ in range(3):
                key_info = self._key_for_lane(existing, excluded=excluded_keys)
                key = str(key_info.get("rotating_key") or "").strip()
                if not key:
                    raise RotatingProxyError("Không xác định được keyxoay cho lane")
                try:
                    proxy = self.client.get_proxy(key)
                except Exception as exc:  # noqa: BLE001 - retry another rotating key.
                    last_error = exc
                    cooldown = self._cooldown_seconds(exc)
                    if cooldown and fallback:
                        fallback_expiry = now + cooldown
                        restored = self.store.try_upsert_lease(
                            lane,
                            scope=lane_scope,
                            rotating_key=str(fallback["rotating_key"]),
                            proxy_url=str(fallback["proxy_url"]),
                            proxy_expires_at=fallback_expiry,
                            key_expires_at=fallback.get("key_expires_at"),
                            assigned_at=fallback.get("assigned_at") or now,
                        )
                        if restored:
                            logger.warning(
                                "[RotatingProxy] provider cooldown còn %ss; giữ proxy hiện tại cho scope=%s lane=%s",
                                cooldown,
                                lane_scope,
                                lane,
                            )
                            return RotatingProxyLease(
                                scope=lane_scope,
                                lane_id=lane,
                                key=str(fallback["rotating_key"]),
                                proxy_url=str(fallback["proxy_url"]),
                                proxy_expires_at=fallback_expiry,
                                key_expires_at=fallback.get("key_expires_at"),
                            )
                    failed_attempts[key] = failed_attempts.get(key, 0) + 1
                    if failed_attempts[key] >= 2:
                        excluded_keys.add(key)
                    existing = None
                    logger.warning(
                        "[RotatingProxy] get proxy failed; retrying another proxy: key=%s attempt=%s error=%s",
                        _mask_key(key),
                        failed_attempts[key],
                        str(exc)[:180],
                    )
                    continue
                proxy_url = str(proxy.get("proxy_url") or "").strip()
                if not proxy_url:
                    last_error = RotatingProxyError("proxy.vn không trả proxy_url")
                    failed_attempts[key] = failed_attempts.get(key, 0) + 1
                    if failed_attempts[key] >= 2:
                        excluded_keys.add(key)
                    existing = None
                    continue
                if not self._proxy_healthy(proxy_url):
                    last_error = RotatingProxyError(
                        "proxy.vn trả proxy nhưng health-check thất bại"
                    )
                    failed_attempts[key] = failed_attempts.get(key, 0) + 1
                    if failed_attempts[key] >= 2:
                        excluded_keys.add(key)
                    existing = None
                    logger.warning(
                        "[RotatingProxy] proxy health-check failed; retrying: key=%s attempt=%s",
                        _mask_key(key),
                        failed_attempts[key],
                    )
                    continue
                ttl = proxy.get("ttl_seconds")
                try:
                    ttl_seconds = max(1, int(ttl)) if ttl is not None else 1800
                except (TypeError, ValueError):
                    ttl_seconds = 1800
                proxy_expires_at = now + ttl_seconds
                key_expires_at = key_info.get("key_expires_at")
                persisted = self.store.try_upsert_lease(
                    lane,
                    scope=lane_scope,
                    rotating_key=key,
                    proxy_url=proxy_url,
                    proxy_expires_at=proxy_expires_at,
                    key_expires_at=key_expires_at,
                    assigned_at=existing.get("assigned_at") if existing else now,
                )
                if persisted:
                    return RotatingProxyLease(
                        scope=lane_scope,
                        lane_id=lane,
                        key=key,
                        proxy_url=proxy_url,
                        proxy_expires_at=proxy_expires_at,
                        key_expires_at=key_expires_at,
                    )
                excluded_keys.add(key)
                existing = None
                logger.info(
                    "[RotatingProxy] keyxoay vừa được lane khác claim, chọn key khác: scope=%s lane=%s",
                    lane_scope,
                    lane,
                )
            if last_error is not None:
                raise RotatingProxyError(
                    f"Không lấy được proxy khả dụng sau khi thử lại: {last_error}"
                ) from last_error
            raise RotatingProxyError(
                "rotating proxy key vừa được lane khác claim, không tìm được key thay thế"
            )

    def release(
        self,
        lane_id: int,
        *,
        scope: str = "registration",
        rotating_key: str | None = None,
        proxy_url: str | None = None,
    ) -> bool:
        """Release a scoped worker-lane lease so another workflow can claim the key."""
        lane = self._lane_id(lane_id)
        lane_scope = self._scope(scope)
        with self._lock:
            released = self.store.delete_lease(
                lane,
                scope=lane_scope,
                rotating_key=rotating_key,
                proxy_url=proxy_url,
            )
        if released:
            logger.info(
                "[RotatingProxy] released lease: scope=%s lane=%s",
                lane_scope,
                lane,
            )
        return released

    def refresh_keys(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_keys()
            return self.status()

    def status(self) -> dict[str, Any]:
        from config import proxy as proxy_config

        self.store.delete_expired_leases(self.clock())
        keys = self.store.list_keys()
        leases = self.store.list_leases()
        return {
            "enabled": bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)),
            "configured": bool(str(getattr(proxy_config, "ROTATING_PROXY_API_KEY", "") or "").strip()),
            "protocol": str(getattr(proxy_config, "ROTATING_PROXY_PROTOCOL", "http") or "http"),
            "keys": [
                {
                    "key": _mask_key(item.get("rotating_key")),
                    "expires_at": item.get("key_expires_at"),
                    "lanes": self.store.lanes_for_key(str(item.get("rotating_key") or "")),
                    "assignments": self.store.scoped_lanes_for_key(str(item.get("rotating_key") or "")),
                }
                for item in keys
            ],
            "leases": [
                {
                    "scope": str(item.get("lane_scope") or "registration"),
                    "lane_id": int(item["lane_id"]),
                    "key": _mask_key(item.get("rotating_key")),
                    "proxy": _mask_proxy(item.get("proxy_url")),
                    "proxy_expires_at": item.get("proxy_expires_at"),
                }
                for item in leases
            ],
        }


_DEFAULT_MANAGER: RotatingProxyManager | None = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def get_rotating_proxy_manager() -> RotatingProxyManager:
    global _DEFAULT_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            manager = RotatingProxyManager()
            cleared = manager.store.clear_leases()
            if cleared:
                logger.info("[RotatingProxy] cleared %s lease(s) from previous process", cleared)
            _DEFAULT_MANAGER = manager
        return _DEFAULT_MANAGER


def mask_rotating_proxy(value: object) -> str:
    """Expose a safe proxy rendering for callers outside the manager."""
    return _mask_proxy(value)
