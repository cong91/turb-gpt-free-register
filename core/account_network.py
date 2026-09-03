"""Network selection for account recovery and reauthorization workflows."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from core.rotating_proxy_runtime import (
    default_proxy_lane_id,
    resolve_rotating_proxy,
)
from core.rotating_proxy_runtime import (
    release_rotating_proxy as _release_rotating_proxy,
)

logger = logging.getLogger(__name__)

ACCOUNT_NETWORK_MODES = frozenset({"auto", "nord_wire", "rotating_proxy", "proxy_pool"})


def normalize_account_network_mode(value: str | None) -> str:
    """Normalize the operator-selected route for browser account workflows."""
    mode = str(value or "auto").strip().lower()
    if mode not in ACCOUNT_NETWORK_MODES:
        choices = ", ".join(sorted(ACCOUNT_NETWORK_MODES))
        raise ValueError(f"login_network_mode={mode!r} 无效，可选 {choices}")
    return mode


def release_rotating_proxy(
    *,
    scope: str,
    lane_id: int | None = None,
    proxy_url: str | None = None,
) -> bool:
    """Retain a rotating-proxy lane until TTL expiry after its workflow completes."""
    return _release_rotating_proxy(
        scope=scope,
        lane_id=default_proxy_lane_id() if lane_id is None else lane_id,
        proxy_url=proxy_url,
    )


@contextmanager
def preferred_account_proxy(
    explicit_proxy: str | None,
    *,
    rotating_scope: str,
    lane_id: int | None = None,
    lease_owner_id: str | None = None,
) -> Iterator[tuple[str | None, str]]:
    """Yield the best available route for an account workflow."""
    if explicit_proxy is not None:
        yield explicit_proxy, "explicit"
        return

    from core.nordvpn_wireguard import (
        is_per_profile_proxy_enabled,
        proxy_for_registration,
    )

    if is_per_profile_proxy_enabled():
        with ExitStack() as stack:
            try:
                proxy_context = (
                    proxy_for_registration(owner_id=lease_owner_id)
                    if lease_owner_id is not None
                    else proxy_for_registration()
                )
                active_proxy = stack.enter_context(proxy_context)
            except Exception as exc:  # noqa: BLE001 - provider acquisition must fall back safely.
                logger.warning(
                    "[Account network] NordVPN WireGuard unavailable; falling back: %s: %s",
                    type(exc).__name__,
                    str(exc)[:180],
                )
            else:
                if active_proxy:
                    yield active_proxy, "nordvpn_wireguard"
                    return
                logger.warning("[Account network] NordVPN WireGuard returned no proxy; falling back")

    active_proxy = resolve_rotating_proxy(None, scope=rotating_scope, lane_id=lane_id)
    try:
        yield active_proxy, "rotating_proxy" if active_proxy is not None else "direct"
    finally:
        if active_proxy is not None:
            release_rotating_proxy(
                scope=rotating_scope,
                lane_id=lane_id,
                proxy_url=active_proxy,
            )


@contextmanager
def selected_account_proxy(
    mode: str | None,
    *,
    rotating_scope: str,
    lane_id: int | None = None,
    lease_owner_id: str | None = None,
) -> Iterator[tuple[str | None, str]]:
    """Yield the route selected for a browser account workflow."""
    normalized_mode = normalize_account_network_mode(mode)
    if normalized_mode == "auto":
        with preferred_account_proxy(
            None,
            rotating_scope=rotating_scope,
            lane_id=lane_id,
            lease_owner_id=lease_owner_id,
        ) as route:
            yield route
        return

    if normalized_mode == "nord_wire":
        from core.nordvpn_wireguard import proxy_for_registration

        with proxy_for_registration(owner_id=lease_owner_id) as active_proxy:
            if not active_proxy:
                raise RuntimeError("NordVPN WireGuard chưa cấp được proxy cho lượt đăng nhập")
            yield active_proxy, normalized_mode
        return

    if normalized_mode == "rotating_proxy":
        active_proxy = resolve_rotating_proxy(None, scope=rotating_scope, lane_id=lane_id)
        if active_proxy is None:
            raise RuntimeError("Proxy xoay chưa bật hoặc chưa lấy được lease cho lượt đăng nhập")
        try:
            yield active_proxy, normalized_mode
        finally:
            release_rotating_proxy(
                scope=rotating_scope,
                lane_id=lane_id,
                proxy_url=active_proxy,
            )
        return

    from config.proxy import pick_proxy

    active_proxy = pick_proxy(
        probe_url="https://chatgpt.com/auth/login",
        probe_timeout=4.0,
    )
    if not active_proxy:
        raise RuntimeError("Proxy pool không có proxy truy cập được chatgpt.com")
    yield active_proxy, normalized_mode
