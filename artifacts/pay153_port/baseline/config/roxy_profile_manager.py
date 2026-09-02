# -*- coding: utf-8 -*-
"""Independent RoxyBrowser profile-manager configuration."""
from pathlib import Path

from config.env_loader import apply_env_overrides, env_str

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

ROXY_PROFILE_MANAGER_ENABLED: bool = True
ROXY_PROFILE_MANAGER_OWNER_PREFIX: str = "zcode-profile-manager"
ROXY_PROFILE_ARCHIVE_DIR: str = str(_PROJECT_ROOT / "roxy_profile_archives")
ROXY_PROFILE_ARCHIVE_KEY: str = env_str("ROXY_PROFILE_ARCHIVE_KEY", "")
ROXY_PROFILE_ARCHIVE_MAX_BYTES: int = 10 * 1024 * 1024
ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES: int = 2 * 1024 * 1024 * 1024
ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED: bool = True
ROXY_PROFILE_ROXY_CHROME_PATH: str = env_str("ROXY_PROFILE_ROXY_CHROME_PATH", "")
ROXY_PROFILE_CACHE_ROOT: str = env_str(
    "ROXY_PROFILE_CACHE_ROOT",
    str(Path.home() / "AppData" / "Roaming" / "RoxyBrowser" / "browser-cache"),
)
ROXY_PROFILE_OFFLINE_STAGING_DIR: str = env_str(
    "ROXY_PROFILE_OFFLINE_STAGING_DIR",
    str(_PROJECT_ROOT / "roxy_profile_staging"),
)
ROXY_PROFILE_OFFLINE_TIMEOUT: int = 20
ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH: bool = False

apply_env_overrides(
    globals(),
    {
        "ROXY_PROFILE_MANAGER_ENABLED": "bool",
        "ROXY_PROFILE_MANAGER_OWNER_PREFIX": "str",
        "ROXY_PROFILE_ARCHIVE_DIR": "str",
        "ROXY_PROFILE_ARCHIVE_KEY": "str",
        "ROXY_PROFILE_ARCHIVE_MAX_BYTES": "int",
        "ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES": "int",
        "ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED": "bool",
        "ROXY_PROFILE_ROXY_CHROME_PATH": "str",
        "ROXY_PROFILE_CACHE_ROOT": "str",
        "ROXY_PROFILE_OFFLINE_STAGING_DIR": "str",
        "ROXY_PROFILE_OFFLINE_TIMEOUT": "int",
        "ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH": "bool",
    },
)

# Zero/negative archive limits are invalid and otherwise reject every upload.
# Treat them as an omitted override while preserving explicit positive limits.
if ROXY_PROFILE_ARCHIVE_MAX_BYTES <= 0:
    ROXY_PROFILE_ARCHIVE_MAX_BYTES = 10 * 1024 * 1024
if ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES <= 0:
    ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
