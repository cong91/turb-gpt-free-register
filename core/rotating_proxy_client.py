"""HTTP client for the proxy.vn rotating-key and proxy APIs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import requests

from config import proxy as proxy_config
from config.proxy import normalize_proxy_url


class RotatingProxyApiError(RuntimeError):
    """The rotating-proxy provider rejected or returned an invalid response."""


def _json_documents(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            break
        value, end = decoder.raw_decode(text, offset)
        if not isinstance(value, dict):
            raise RotatingProxyApiError("proxy.vn 返回的 JSON 结构无效")
        documents.append(value)
        offset = end
    if not documents:
        raise RotatingProxyApiError("proxy.vn 返回空响应")
    return documents


def _response_documents(response) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return _json_documents(str(getattr(response, "text", "") or ""))
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return list(payload)
    raise RotatingProxyApiError("proxy.vn 返回的 JSON 结构无效")


def _status(document: dict[str, Any]) -> int:
    try:
        return int(str(document.get("status") or "0"))
    except (TypeError, ValueError):
        return 0


def _provider_message(document: dict[str, Any]) -> str:
    return str(document.get("comen") or document.get("message") or "").strip()


def _is_empty_inventory_response(document: dict[str, Any]) -> bool:
    return _status(document) == 101


def _error_from(document: dict[str, Any]) -> RotatingProxyApiError:
    status = _status(document)
    message = _provider_message(document) or "未知错误"
    return RotatingProxyApiError(f"proxy.vn API status={status}: {message[:240]}")


def _parse_expiration(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    provider_timezone = timezone(timedelta(hours=7))
    for fmt in ("%H:%M %d-%m-%y", "%H:%M %d-%m-%Y"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=provider_timezone)
            return parsed.timestamp()
        except ValueError:
            continue
    return None


def _proxy_url(value: object, scheme: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RotatingProxyApiError("proxy.vn 未返回 proxy 地址")
    if "://" in text:
        return normalize_proxy_url(text)
    fields = text.split(":", 3)
    if (
        len(fields) < 2
        or not fields[0]
        or not fields[1].isdigit()
        or not 1 <= int(fields[1]) <= 65535
    ):
        raise RotatingProxyApiError("proxy.vn 返回的 proxy 地址无效")
    if len(fields) == 4 and (fields[2] or fields[3]):
        username = quote(fields[2], safe="")
        password = quote(fields[3], safe="")
        return f"{scheme}://{username}:{password}@{fields[0]}:{fields[1]}"
    return f"{scheme}://{fields[0]}:{fields[1]}"


class RotatingProxyClient:
    """Call provider endpoints while keeping provider response parsing local."""

    _HEALTHCHECK_URL = "https://chatgpt.com/"
    _HEALTHCHECK_TIMEOUT = 5.0

    def __init__(self, http_client=None):
        self._http = http_client or requests

    @staticmethod
    def _timeout() -> float:
        try:
            return max(1.0, float(getattr(proxy_config, "ROTATING_PROXY_REQUEST_TIMEOUT", 15.0)))
        except (TypeError, ValueError):
            return 15.0

    @staticmethod
    def _provider_url(path: str) -> str:
        base = str(getattr(proxy_config, "ROTATING_PROXY_API_BASE", "") or "").strip()
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))

    @staticmethod
    def _proxy_url_endpoint() -> str:
        base = str(getattr(proxy_config, "ROTATING_PROXY_PROXY_API_BASE", "") or "").strip()
        return urljoin(base.rstrip("/") + "/", "get.php")

    def _request_get(
        self,
        request_url: str,
        *,
        endpoint: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            if params is None:
                response = self._http.get(request_url, timeout=self._timeout())
            else:
                response = self._http.get(request_url, params=params, timeout=self._timeout())
            response.raise_for_status()
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            status = f"HTTP {status_code}" if status_code is not None else "HTTP error"
            raise RotatingProxyApiError(
                f"proxy.vn 请求失败: {status}, endpoint={endpoint}"
            ) from exc
        except requests.RequestException as exc:
            raise RotatingProxyApiError(f"proxy.vn 请求失败: {type(exc).__name__}") from exc
        return _response_documents(response)

    def _get(self, url: str, params: dict[str, object]) -> list[dict[str, Any]]:
        return self._request_get(url, endpoint=url, params=params)

    def _get_raw(self, url: str, params: dict[str, object]) -> list[dict[str, Any]]:
        query = urlencode(params).replace("&", "&&")
        request_url = f"{url}?{query}" if query else url
        return self._request_get(request_url, endpoint=url)

    @staticmethod
    def _api_key() -> str:
        value = str(getattr(proxy_config, "ROTATING_PROXY_API_KEY", "") or "").strip()
        if not value:
            raise RotatingProxyApiError(
                "ROTATING_PROXY_API_KEY chưa được cấu hình trong Settings"
            )
        return value

    def list_keys(self) -> list[dict[str, Any]]:
        documents = self._get(
            self._provider_url("apigetkeyxoay.php"),
            {"key": self._api_key()},
        )
        result = []
        for document in documents:
            if _status(document) != 100:
                # The provider uses status 101 when no purchased key remains active.
                if _is_empty_inventory_response(document):
                    continue
                raise _error_from(document)
            key = str(document.get("keyxoay") or "").strip()
            if key:
                result.append({"key": key, "expires_at": _parse_expiration(document.get("expired"))})
        return result

    def purchase_keys(self, quantity: int) -> list[dict[str, Any]]:
        try:
            amount = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity must be a positive integer") from exc
        if amount < 1:
            raise ValueError("quantity must be a positive integer")
        documents = self._get_raw(
            self._provider_url("apimuangay.php"),
            {"key": self._api_key(), "thoigian": 1, "soluong": amount},
        )
        result = []
        seen: set[str] = set()
        for document in documents:
            if _status(document) != 100:
                raise _error_from(document)
            key = str(document.get("keyxoay") or "").strip()
            if key and key not in seen:
                seen.add(key)
                result.append({"key": key, "expires_at": _parse_expiration(document.get("expired"))})
        if not result:
            raise RotatingProxyApiError("proxy.vn mua key thành công nhưng không trả keyxoay")
        return result

    def purchase_key(self) -> dict[str, Any]:
        return self.purchase_keys(1)[0]

    def renew_key(self, key: str) -> dict[str, Any]:
        documents = self._get(
            self._provider_url("apigiahanngay.php"),
            {"key": self._api_key(), "keyxoay": str(key), "thoigian": 1},
        )
        for document in documents:
            if _status(document) != 100:
                raise _error_from(document)
        expiry = next(
            (_parse_expiration(item.get("expired")) for item in documents if item.get("expired")),
            None,
        )
        return {"key": str(key), "expires_at": expiry}

    def get_proxy(self, key: str) -> dict[str, Any]:
        carrier = str(getattr(proxy_config, "ROTATING_PROXY_NHAMANG", "random") or "random").strip()
        province = str(getattr(proxy_config, "ROTATING_PROXY_TINHTHANH", "0") or "0").strip()
        whitelist = str(getattr(proxy_config, "ROTATING_PROXY_WHITELIST", "") or "").strip()
        protocol = str(getattr(proxy_config, "ROTATING_PROXY_PROTOCOL", "http") or "http").strip().lower()
        if protocol not in {"http", "socks5"}:
            raise RotatingProxyApiError("ROTATING_PROXY_PROTOCOL chỉ hỗ trợ http hoặc socks5")
        documents = self._get(
            self._proxy_url_endpoint(),
            {
                "key": str(key),
                "nhamang": carrier,
                "tinhthanh": province,
                "whitelist": whitelist,
            },
        )
        for document in documents:
            if _status(document) != 100:
                raise _error_from(document)
            field = "proxyhttp" if protocol == "http" else "proxysocks5"
            raw_proxy = document.get(field) or document.get("proxyhttp") or document.get("proxysocks5")
            ttl_match = re.search(r"(\d+)\s*s", str(document.get("message") or ""), re.IGNORECASE)
            return {
                "proxy_url": _proxy_url(raw_proxy, "socks5" if protocol == "socks5" else "http"),
                "ttl_seconds": int(ttl_match.group(1)) if ttl_match else None,
                "provider": "proxy.vn",
                "raw": document,
            }
        raise RotatingProxyApiError("proxy.vn 未返回可用 proxy")

    def check_proxy(self, proxy_url: str) -> bool:
        """Check a cached or newly fetched proxy without rotating its IP."""
        value = str(proxy_url or "").strip()
        if not value:
            return False
        try:
            response = self._http.get(
                self._HEALTHCHECK_URL,
                proxies={"http": value, "https": value},
                timeout=self._HEALTHCHECK_TIMEOUT,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            return 100 <= status_code < 500 and status_code != 407
        except (requests.RequestException, TypeError, ValueError):
            return False
