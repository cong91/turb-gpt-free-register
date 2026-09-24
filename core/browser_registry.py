"""Canonical registration-browser driver registry and lifecycle dispatch."""
from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_DRIVER = "roxy"


@dataclass(frozen=True, slots=True)
class BrowserDriverSpec:
    """Lazy lifecycle and registration entry points for one canonical driver."""

    runner: str
    profile_opener: str | None = None
    profile_closer: str | None = None
    profile_deleter: str | None = None
    managed_proxy: bool = False


ALIASES: Mapping[str, str] = {
    "roxy": "roxy",
    "roxybrowser": "roxy",
    "fingerprint": "roxy",
    "browser": "roxy",
    "cloak": "cloak",
    "cloakbrowser": "cloak",
    "browser_use": "browser_use",
    "browseruse": "browser_use",
    "browser-use": "browser_use",
    "bu": "browser_use",
    "skyvern": "skyvern",
    "sv": "skyvern",
}

DRIVER_SPECS: Mapping[str, BrowserDriverSpec] = {
    "protocol": BrowserDriverSpec("main:_run_protocol_registration"),
    "roxy": BrowserDriverSpec(
        "core.roxy_registration:run_roxy_registration",
        "core.browser_profile_adapters:_open_roxy",
        "core.browser_profile_adapters:_close_roxy",
        "core.browser_profile_adapters:_delete_roxy",
        managed_proxy=True,
    ),
    "cloak": BrowserDriverSpec(
        "core.cloakbrowser_registration:run_cloak_registration",
        "core.browser_profile_adapters:_open_cloak",
        "core.browser_profile_adapters:_close_cloak",
        "core.browser_profile_adapters:_delete_cloak",
        managed_proxy=True,
    ),
    "browser_use": BrowserDriverSpec(
        "core.browser_use_registration:run_browser_use_registration",
        "core.browser_profile_adapters:_open_browser_use",
    ),
    "skyvern": BrowserDriverSpec(
        "core.skyvern_registration:run_skyvern_registration",
        "core.browser_profile_adapters:_open_skyvern",
    ),
}

LIVE_BROWSER_DRIVERS = frozenset(name for name in DRIVER_SPECS if name != "protocol")
SUPPORTED_REGISTRATION_DRIVERS = frozenset(DRIVER_SPECS)


def normalize_driver(value: object, *, default: str = DEFAULT_DRIVER) -> str:
    """Normalize a configured alias to the canonical driver name."""
    raw = str(value or default).strip().lower()
    return ALIASES.get(raw, raw)


def configured_driver(config_module, *, default: str = DEFAULT_DRIVER) -> str:
    """Read and normalize REGISTRATION_DRIVER from a config module."""
    return normalize_driver(getattr(config_module, "REGISTRATION_DRIVER", default), default=default)


def resolve_registration_driver(config_module) -> str:
    """Resolve the live registration driver using the canonical default."""
    return configured_driver(config_module)


def resolve_twofa_retry_driver(config_module) -> str:
    """Resolve the driver used by reactive 2FA retry."""
    return configured_driver(config_module)


def resolve_browser_profile_provider(value: object) -> str:
    """Resolve a canonical driver with a registered profile lifecycle."""
    driver = normalize_driver(value)
    if driver in DRIVER_SPECS and DRIVER_SPECS[driver].profile_opener:
        return driver
    raise RuntimeError(
        f"personal-information changes do not support REGISTRATION_DRIVER={driver!r}"
    )


def is_live_browser_driver(driver: object) -> bool:
    """Whether a canonical/aliased value owns a reusable browser session."""
    return normalize_driver(driver) in LIVE_BROWSER_DRIVERS


def is_supported_driver(driver: object) -> bool:
    """Whether a configured value is one of the supported canonical drivers."""
    return normalize_driver(driver) in SUPPORTED_REGISTRATION_DRIVERS


def _load(path: str) -> Callable[..., Any]:
    module_name, separator, attr_name = path.partition(":")
    if not separator or not module_name or not attr_name:
        raise RuntimeError(f"invalid browser registry entry: {path!r}")
    return getattr(importlib.import_module(module_name), attr_name)


def resolve_driver_spec(value: object) -> BrowserDriverSpec:
    """Return the canonical lifecycle spec for a configured value."""
    driver = normalize_driver(value)
    try:
        return DRIVER_SPECS[driver]
    except KeyError as exc:
        raise RuntimeError(unsupported_driver_message(driver)) from exc


def resolve_registration_runner(value: object) -> Callable[..., Any]:
    """Load the registration runner for a canonical or aliased driver."""
    return _load(resolve_driver_spec(value).runner)


def resolve_profile_opener(value: object) -> Callable[..., Any]:
    """Load the lifecycle opener registered for a browser profile driver."""
    opener = resolve_driver_spec(resolve_browser_profile_provider(value)).profile_opener
    if opener is None:
        raise RuntimeError(f"driver has no profile lifecycle: {value!r}")
    return _load(opener)


def run_registered_registration(
    driver: object,
    *,
    email: str | None,
    name: str,
    birthday: str,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir=None,
    proxy_lane_id: int | None = None,
    lease_owner_id: str | None = None,
    on_email_acquired=None,
) -> dict:
    """Run one registered driver through its lifecycle entry point."""
    canonical = normalize_driver(driver)
    spec = resolve_driver_spec(canonical)
    runner = resolve_registration_runner(canonical)
    kwargs = {
        "email": email,
        "name": name,
        "birthday": birthday,
        "proxy": proxy,
        "otp_code": otp_code,
        "batch_dir": batch_dir,
        "on_email_acquired": on_email_acquired,
    }
    if spec.managed_proxy and proxy is None:
        from core.nordvpn_wireguard import proxy_for_registration

        proxy_context = (
            proxy_for_registration(owner_id=lease_owner_id)
            if lease_owner_id is not None
            else proxy_for_registration()
        )
        with proxy_context as managed_proxy:
            kwargs["proxy"] = managed_proxy
            return runner(**kwargs)
    return runner(**kwargs)


def open_registered_profile(driver: object, *, proxy: str | None = None):
    """Open a profile through the lifecycle opener stored in the registry."""
    return resolve_profile_opener(driver)(proxy=proxy)


def unsupported_driver_message(driver: object) -> str:
    """Return the existing user-facing unsupported-driver message."""
    return (
        f"不支持的 REGISTRATION_DRIVER={str(driver)!r}，"
        "可选 protocol / roxy / cloak / browser_use / skyvern"
    )

