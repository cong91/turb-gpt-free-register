"""NordVPN account API configuration for dynamic NordLynx proxies."""
from config.env_loader import apply_env_overrides

# NordVPN access token from the Nord Account dashboard. This is a secret and
# must only be stored in .env. When set, it takes precedence over local .conf files.
NORDVPN_ACCESS_TOKEN: str = ""

# Official Core API used by the open-source NordVPN Linux client.
NORDVPN_API_BASE: str = "https://api.nordvpn.com"
NORDVPN_API_TIMEOUT: float = 20.0
NORDVPN_SERVER_CACHE_TTL: int = 300
NORDVPN_SERVER_LIMIT: int = 100
NORDVPN_RECENT_SERVER_COUNT: int = 20

apply_env_overrides(
    globals(),
    {
        "NORDVPN_ACCESS_TOKEN": "str",
        "NORDVPN_API_BASE": "str",
        "NORDVPN_API_TIMEOUT": "float",
        "NORDVPN_SERVER_CACHE_TTL": "int",
        "NORDVPN_SERVER_LIMIT": "int",
        "NORDVPN_RECENT_SERVER_COUNT": "int",
    },
)
