"""Resolve the configured rotating proxy for every network workflow."""
from __future__ import annotations

import logging
import threading
from urllib.parse import unquote, urlparse

REGISTRATION_PROXY_SCOPE = "registration"
CODEX_OAUTH_PROXY_SCOPE = "codex_oauth"
CODEX_RETRY_PROXY_SCOPE = "codex_retry"
PLAN_CHECK_PROXY_SCOPE = "plan_check"
LIVE_CHECK_PROXY_SCOPE = "live_check"
CODEX_AGENT_PROXY_SCOPE = "codex_agent"
TWOFA_RETRY_PROXY_SCOPE = "twofa_retry"
TWOFA_SETUP_PROXY_SCOPE = "twofa_setup"
TWOFA_CHANGE_PROXY_SCOPE = "twofa_change"
EMAIL_CHANGE_PROXY_SCOPE = "email_change"
EXTRACT_LINK_PROXY_SCOPE = "extract_link"
EXTRACT_LINK_PAYMENT_PROXY_SCOPE = "extract_link_payment"
EXTRACT_LINK_PROMOTION_PROXY_SCOPE = "extract_link_promotion"

logger = logging.getLogger(__name__)


def default_proxy_lane_id() -> int:
    """Use the stable worker suffix when available, otherwise the thread identity."""
    name = str(threading.current_thread().name or "")
    suffix = name.rsplit("_", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return int(threading.get_ident())


def custom_proxy_details(proxy: str) -> dict[str, object]:
    """Convert a proxy URL into a provider-neutral custom proxy payload."""
    value = str(proxy or "").strip()
    parsed = urlparse(value if "://" in value else f"//{value}")
    scheme = str(parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port:
        raise ValueError("rotating proxy URL không hợp lệ cho cloud browser")
    details: dict[str, object] = {
        "host": parsed.hostname,
        "port": int(parsed.port),
    }
    if parsed.username is not None:
        details["username"] = unquote(parsed.username)
    if parsed.password is not None:
        details["password"] = unquote(parsed.password)
    return details


def resolve_rotating_proxy(
    proxy: str | None,
    *,
    scope: str,
    lane_id: int | None = None,
) -> str | None:
    """Return an explicit proxy or acquire one rotating lease for this workflow lane."""
    if proxy is not None:
        return proxy

    from config import proxy as proxy_config

    if not bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)):
        return None

    effective_lane = default_proxy_lane_id() if lane_id is None else lane_id
    from core.rotating_proxy_manager import get_rotating_proxy_manager

    manager = get_rotating_proxy_manager()
    if scope == REGISTRATION_PROXY_SCOPE:
        lease = manager.acquire(effective_lane)
    else:
        lease = manager.acquire(effective_lane, scope=scope)
    return lease.proxy_url


def release_rotating_proxy(
    *,
    scope: str,
    lane_id: int | None = None,
    proxy_url: str | None = None,
    retire: bool = False,
) -> bool:
    """Release an active lane immediately while preserving a reusable proxy cache."""
    from core.rotating_proxy_manager import get_rotating_proxy_manager

    try:
        effective_lane = default_proxy_lane_id() if lane_id is None else lane_id
        manager = get_rotating_proxy_manager()
        if retire:
            return manager.retire(
                effective_lane,
                scope=scope,
                proxy_url=proxy_url,
            )
        return manager.release(
            effective_lane,
            scope=scope,
            proxy_url=proxy_url,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not hide the workflow result.
        logger.warning(
            "[RotatingProxy] release lease failed: scope=%s lane=%s error=%s: %s",
            scope,
            lane_id if lane_id is not None else "thread",
            type(exc).__name__,
            str(exc)[:180],
        )
        return False


def retire_rotating_proxy(
    *,
    scope: str,
    lane_id: int | None = None,
    proxy_url: str | None = None,
) -> bool:
    """Explicitly discard a lane lease before its provider TTL expires."""
    return release_rotating_proxy(
        scope=scope,
        lane_id=lane_id,
        proxy_url=proxy_url,
        retire=True,
    )


def prepare_rotating_proxy_lanes(lane_count: int, *, scope: str) -> None:
    """Pre-purchase enough unique rotating keys for a batch's worker lanes."""
    from config import proxy as proxy_config

    if not bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)):
        return
    if scope in {CODEX_RETRY_PROXY_SCOPE, PLAN_CHECK_PROXY_SCOPE, TWOFA_RETRY_PROXY_SCOPE}:
        from core.nordvpn_wireguard import is_per_profile_proxy_enabled

        if is_per_profile_proxy_enabled():
            return

    from core.rotating_proxy_manager import get_rotating_proxy_manager

    get_rotating_proxy_manager().ensure_key_inventory(lane_count, scope=scope)
