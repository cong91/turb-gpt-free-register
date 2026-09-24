"""Provider-neutral lifecycle contract and registry façade."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class BrowserProfileSession:
    """A PageDriver plus provider-owned cleanup callbacks."""

    driver: Any
    provider: str
    timeout: int
    keep_open: bool
    _cleanup: Callable[[], None]
    session_info: Any | None = None

    def close(self) -> None:
        if not self.keep_open:
            quit_driver = getattr(self.driver, "quit", None)
            if callable(quit_driver):
                quit_driver()

    def cleanup(self) -> None:
        self._cleanup()


def _configured_provider() -> str:
    from config import roxybrowser as driver_config
    from core.browser_registry import resolve_registration_driver

    return resolve_registration_driver(driver_config)


def open_browser_profile(proxy: str | None = None) -> BrowserProfileSession:
    """Open the configured browser provider through the canonical registry."""
    from core.browser_registry import open_registered_profile

    return open_registered_profile(_configured_provider(), proxy=proxy)
