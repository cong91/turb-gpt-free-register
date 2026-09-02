"""Resolve a NordVPN access token into a per-profile NordLynx configuration.

The endpoint and response fields mirror NordVPN's open-source Linux client:
``GET /v1/users/services/credentials`` returns ``nordlynx_private_key`` and
``GET /v1/servers/recommendations`` returns online WireGuard technology 35
servers with their public keys. Tokens are sent only to the credentials
endpoint and are never logged.
"""
import base64
import ipaddress
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_WIREGUARD_TECHNOLOGY_ID = 35
_NORDLYNX_ADDRESS = "10.5.0.2/16"
_NORDLYNX_DNS = "103.86.96.100"
_NORDLYNX_PORT = 51820
_CREDENTIALS_PATH = "/v1/users/services/credentials"
_COUNTRIES_PATH = "/v1/servers/countries"
_RECOMMENDATIONS_PATH = "/v1/servers/recommendations"


class NordVPNAccountError(RuntimeError):
    """Raised when access-token credentials or NordLynx servers are invalid."""


def _cfg_attr(name: str, default=None):
    """Read config.nordvpn_account dynamically for WebUI hot reload."""
    from config import nordvpn_account as _cfg

    return getattr(_cfg, name, default)


def _valid_wireguard_key(value: object) -> str | None:
    """Return a normalized 32-byte WireGuard key or None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
    return text if len(decoded) == 32 else None


@dataclass(frozen=True)
class NordLynxServer:
    hostname: str
    station: str
    public_key: str
    country_code: str
    load: int


@dataclass(frozen=True)
class NordLynxConfig:
    server: NordLynxServer
    text: str


class NordVPNAccountClient:
    """Small HTTP client for NordVPN credentials and recommendation APIs."""

    def __init__(
        self,
        access_token: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self._access_token = str(
            access_token if access_token is not None else _cfg_attr("NORDVPN_ACCESS_TOKEN", "")
        ).strip()
        self._api_base = str(
            api_base if api_base is not None else _cfg_attr("NORDVPN_API_BASE", "https://api.nordvpn.com")
        ).strip().rstrip("/")
        self._timeout = float(
            timeout if timeout is not None else _cfg_attr("NORDVPN_API_TIMEOUT", 20.0)
        )
        self._http = http or requests.Session()
        self._cache_lock = threading.Lock()
        self._private_key_cache: str | None = None
        self._countries_cache: tuple[float, dict[str, int]] | None = None
        self._servers_cache: dict[str, tuple[float, list[NordLynxServer]]] = {}
        recent_count = max(0, int(_cfg_attr("NORDVPN_RECENT_SERVER_COUNT", 20) or 0))
        self._recent_hostnames: deque[str] = deque(maxlen=recent_count)

    @property
    def configured(self) -> bool:
        """Return whether an access token is available without exposing it."""
        return bool(self._access_token)

    def _url(self, path: str) -> str:
        return urljoin(self._api_base + "/", str(path).lstrip("/"))

    def _get_json(
        self,
        path: str,
        *,
        params: dict | None = None,
        authenticated: bool = False,
    ):
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self._access_token:
                raise NordVPNAccountError(
                    "NORDVPN_ACCESS_TOKEN chưa được cấu hình trong .env"
                )
            headers["Authorization"] = f"Bearer token:{self._access_token}"
        try:
            response = self._http.get(
                self._url(path),
                params=params or None,
                headers=headers,
                timeout=max(1.0, self._timeout),
            )
        except requests.RequestException as exc:
            raise NordVPNAccountError(f"NordVPN API request failed: {exc}") from exc
        if not (200 <= response.status_code < 300):
            detail = (response.text or "").strip()[:300]
            if response.status_code in (401, 403):
                raise NordVPNAccountError(
                    "NordVPN access token không hợp lệ, hết hạn hoặc không có VPN service"
                )
            raise NordVPNAccountError(
                f"NordVPN API HTTP {response.status_code} at {path}"
                + (f": {detail}" if detail else "")
            )
        try:
            return response.json()
        except ValueError as exc:
            raise NordVPNAccountError(
                f"NordVPN API returned invalid JSON at {path}"
            ) from exc

    def private_key(self) -> str:
        """Fetch and cache the account's NordLynx private key."""
        with self._cache_lock:
            if self._private_key_cache:
                return self._private_key_cache
        payload = self._get_json(_CREDENTIALS_PATH, authenticated=True)
        if not isinstance(payload, dict):
            raise NordVPNAccountError("NordVPN credentials response is not an object")
        private_key = _valid_wireguard_key(payload.get("nordlynx_private_key"))
        if not private_key:
            raise NordVPNAccountError(
                "NordVPN credentials response is missing a valid nordlynx_private_key"
            )
        with self._cache_lock:
            self._private_key_cache = private_key
        return private_key

    def country_ids(self) -> dict[str, int]:
        """Return uppercase ISO country code to NordVPN country id."""
        ttl = max(0, int(_cfg_attr("NORDVPN_SERVER_CACHE_TTL", 300) or 0))
        now = time.monotonic()
        with self._cache_lock:
            cached = self._countries_cache
            if cached and now - cached[0] <= ttl:
                return dict(cached[1])
        payload = self._get_json(_COUNTRIES_PATH)
        if not isinstance(payload, list):
            raise NordVPNAccountError("NordVPN countries response is not a list")
        countries: dict[str, int] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().upper()
            try:
                country_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if code:
                countries[code] = country_id
        if not countries:
            raise NordVPNAccountError("NordVPN countries response contains no usable entries")
        with self._cache_lock:
            self._countries_cache = (now, countries)
        return dict(countries)

    @staticmethod
    def _wireguard_public_key(server: dict) -> str | None:
        technologies = server.get("technologies")
        if not isinstance(technologies, list):
            return None
        for technology in technologies:
            if not isinstance(technology, dict):
                continue
            try:
                technology_id = int(technology.get("id"))
            except (TypeError, ValueError):
                continue
            pivot = technology.get("pivot") or {}
            if technology_id != _WIREGUARD_TECHNOLOGY_ID or pivot.get("status") != "online":
                continue
            metadata = technology.get("metadata")
            if not isinstance(metadata, list):
                continue
            for item in metadata:
                if isinstance(item, dict) and item.get("name") == "public_key":
                    return _valid_wireguard_key(item.get("value"))
        return None

    @classmethod
    def _parse_server(cls, item: object) -> NordLynxServer | None:
        if not isinstance(item, dict) or item.get("status") != "online":
            return None
        station = str(item.get("station") or "").strip()
        hostname = str(item.get("hostname") or "").strip()
        try:
            ipaddress.ip_address(station)
        except ValueError:
            return None
        public_key = cls._wireguard_public_key(item)
        if not hostname or not public_key:
            return None
        country_code = ""
        locations = item.get("locations")
        if isinstance(locations, list) and locations and isinstance(locations[0], dict):
            country = locations[0].get("country") or {}
            country_code = str(country.get("code") or "").strip().upper()
        try:
            load = int(item.get("load") or 0)
        except (TypeError, ValueError):
            load = 0
        return NordLynxServer(
            hostname=hostname,
            station=station,
            public_key=public_key,
            country_code=country_code,
            load=max(0, load),
        )

    def servers(self, country_code: str | None = None) -> list[NordLynxServer]:
        """Fetch online NordLynx recommendations, optionally for a country."""
        normalized_country = str(country_code or "").strip().upper()
        ttl = max(0, int(_cfg_attr("NORDVPN_SERVER_CACHE_TTL", 300) or 0))
        now = time.monotonic()
        with self._cache_lock:
            cached = self._servers_cache.get(normalized_country)
            if cached and now - cached[0] <= ttl:
                return list(cached[1])

        params: dict[str, object] = {
            "limit": max(1, int(_cfg_attr("NORDVPN_SERVER_LIMIT", 100) or 100)),
            "filters[servers.status]": "online",
            "filters[servers_technologies]": _WIREGUARD_TECHNOLOGY_ID,
            "filters[servers_technologies][pivot][status]": "online",
        }
        if normalized_country:
            country_id = self.country_ids().get(normalized_country)
            if country_id is None:
                raise NordVPNAccountError(
                    f"NordVPN không hỗ trợ country code {normalized_country!r}"
                )
            params["filters[country_id]"] = country_id

        payload = self._get_json(_RECOMMENDATIONS_PATH, params=params)
        if not isinstance(payload, list):
            raise NordVPNAccountError("NordVPN recommendations response is not a list")
        servers = [server for item in payload if (server := self._parse_server(item))]
        if not servers:
            suffix = f" cho {normalized_country}" if normalized_country else ""
            raise NordVPNAccountError(f"NordVPN không trả về server NordLynx online{suffix}")
        with self._cache_lock:
            self._servers_cache[normalized_country] = (now, servers)
        return list(servers)

    def choose_server(
        self,
        country_code: str | None = None,
        *,
        excluded_hostnames: Iterable[str] = (),
    ) -> NordLynxServer:
        """Prefer a server not used recently, then choose among the lowest loads."""
        servers = self.servers(country_code)
        excluded = {
            str(hostname).strip().lower()
            for hostname in excluded_hostnames
            if str(hostname).strip()
        }
        if excluded:
            servers = [
                server for server in servers
                if server.hostname.strip().lower() not in excluded
            ]
        if not servers:
            suffix = f" cho {str(country_code).strip().upper()}" if country_code else ""
            raise NordVPNAccountError(
                f"NordVPN không còn server NordLynx chưa được lease{suffix}"
            )
        with self._cache_lock:
            recent = set(self._recent_hostnames)
            candidates = [server for server in servers if server.hostname not in recent] or servers
            candidates.sort(key=lambda server: server.load)
            low_load = candidates[: min(10, len(candidates))]
            selected = random.choice(low_load)
            self._recent_hostnames.append(selected.hostname)
            return selected

    def build_config(
        self,
        country_code: str | None = None,
        *,
        excluded_hostnames: Iterable[str] = (),
    ) -> NordLynxConfig:
        """Build a wireproxy-compatible NordLynx config for one Roxy profile."""
        private_key = self.private_key()
        server = self.choose_server(
            country_code,
            excluded_hostnames=excluded_hostnames,
        )
        text = (
            "[Interface]\n"
            f"Address = {_NORDLYNX_ADDRESS}\n"
            f"PrivateKey = {private_key}\n"
            f"DNS = {_NORDLYNX_DNS}\n"
            "\n"
            "[Peer]\n"
            f"PublicKey = {server.public_key}\n"
            f"Endpoint = {server.station}:{_NORDLYNX_PORT}\n"
            "AllowedIPs = 0.0.0.0/0, ::/0\n"
            "PersistentKeepalive = 25\n"
        )
        logger.info(
            "[NordVPN] selected NordLynx server: host=%s country=%s load=%s",
            server.hostname,
            server.country_code or "-",
            server.load,
        )
        return NordLynxConfig(server=server, text=text)


_CLIENT: NordVPNAccountClient | None = None
_CLIENT_CONFIG: tuple[str, str, float] | None = None
_CLIENT_LOCK = threading.Lock()


def get_account_client() -> NordVPNAccountClient:
    """Return a client keyed by current hot-reloaded account configuration."""
    global _CLIENT, _CLIENT_CONFIG
    token = str(_cfg_attr("NORDVPN_ACCESS_TOKEN", "") or "").strip()
    api_base = str(_cfg_attr("NORDVPN_API_BASE", "https://api.nordvpn.com") or "").strip()
    timeout = float(_cfg_attr("NORDVPN_API_TIMEOUT", 20.0) or 20.0)
    signature = (token, api_base, timeout)
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_CONFIG != signature:
            _CLIENT = NordVPNAccountClient(
                access_token=token,
                api_base=api_base,
                timeout=timeout,
            )
            _CLIENT_CONFIG = signature
        return _CLIENT
