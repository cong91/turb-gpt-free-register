# -*- coding: utf-8 -*-
"""NordVPN CLI configuration for local Windows NordVPN app control.

Uses the NordVPN Windows desktop app's Command Prompt interface
(nordvpn -c / nordvpn -d) to manage VPN connections locally.
All operations are no-ops when NORDVPN_ENABLED is False.
"""
from config.env_loader import apply_env_overrides

# Enable NordVPN CLI control.
#   False = all operations silently return False/None (safe default)
#   True  = connect/disconnect/status commands are executed
NORDVPN_ENABLED: bool = False

# NordVPN installation directory containing NordVPN.exe.
NORDVPN_INSTALL_DIR: str = r"C:\Program Files\NordVPN"

# Seconds to wait for a single connect/disconnect command to complete.
NORDVPN_CLI_TIMEOUT: int = 30

# NordVPN local service health-check endpoint.
# The nordvpn-service listens on this host:port when running.
NORDVPN_SERVICE_HOST: str = "127.0.0.1"
NORDVPN_SERVICE_PORT: int = 9247

# Wait seconds after a successful connect before allowing traffic.
# Gives the NordLynx tunnel time to stabilise.
NORDVPN_POST_CONNECT_DELAY: float = 3.0

# Comma-separated country or specialty group codes passed to
# "nordvpn -c -g <group>".  Empty means connect to the best server.
# Examples: "Japan", "United_States,Japan", "P2P"
NORDVPN_COUNTRY_GROUPS: str = ""

# Auto-rotate IP after N successful registrations.
#   False = no automatic rotation (safe default)
#   True  = call connect() every N successes
NORDVPN_AUTO_ROTATE_ENABLED: bool = False

# Number of successful accounts before triggering IP rotation.
# 1 = rotate after every account; 5 = rotate every 5 accounts.
NORDVPN_AUTO_ROTATE_INTERVAL: int = 1

# Country group for auto-rotation.  When set, overrides
# NORDVPN_COUNTRY_GROUPS for the rotation connect call.
# Empty = use NORDVPN_COUNTRY_GROUPS (or best server if both empty).
NORDVPN_AUTO_ROTATE_COUNTRY_GROUP: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'NORDVPN_ENABLED': 'bool',
    'NORDVPN_INSTALL_DIR': 'str',
    'NORDVPN_CLI_TIMEOUT': 'int',
    'NORDVPN_SERVICE_HOST': 'str',
    'NORDVPN_SERVICE_PORT': 'int',
    'NORDVPN_POST_CONNECT_DELAY': 'float',
    'NORDVPN_COUNTRY_GROUPS': 'str',
    'NORDVPN_AUTO_ROTATE_ENABLED': 'bool',
    'NORDVPN_AUTO_ROTATE_INTERVAL': 'int',
    'NORDVPN_AUTO_ROTATE_COUNTRY_GROUP': 'str',
})
