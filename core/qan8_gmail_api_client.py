"""Small QAN8 client for lazy Gmail API URL source acquisition."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

DEFAULT_QAN8_API_BASE = "https://shop.qan8.com"
DEFAULT_QAN8_REQUEST_TIMEOUT = 15
DEFAULT_QAN8_API_PROXY = ""
_QAN8_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_GMAIL_EMAIL = re.compile(r"^[a-z0-9][a-z0-9.+'-]*@(gmail\.com|googlemail\.com)$")


class Qan8GmailApiError(RuntimeError):
    """Base error for QAN8 requests and response validation."""


class Qan8DeliveryError(Qan8GmailApiError):
    """QAN8 returned delivery data outside the verified source contract."""


class Qan8OrderUnknownError(Qan8GmailApiError):
    """The order request outcome is unknown and must be looked up."""


@dataclass(frozen=True)
class Qan8SourceRecord:
    email: str
    code_url: str


@dataclass(frozen=True)
class Qan8Order:
    order_no: str
    status: str
    delivery: object = None
    message: str = ""
    order_id: str | None = None


def _config_value(name: str, default: object) -> object:
    try:
        from config import email as email_config

        return getattr(email_config, name, default)
    except (ImportError, AttributeError):
        return default


def _normalize_proxy_url(value: object) -> str:
    """Normalize and validate the optional proxy used only for QAN8 HTTP calls."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_QAN8_API_PROXY
    try:
        if "://" not in text:
            from config.proxy import normalize_proxy_url

            text = normalize_proxy_url(text)
        parsed = urlsplit(text)
        if (
            parsed.scheme.lower() not in _QAN8_PROXY_SCHEMES
            or not parsed.hostname
            or parsed.port is None
        ):
            raise ValueError("unsupported proxy URL")
    except (TypeError, ValueError) as exc:
        raise Qan8GmailApiError(
            "QAN8 API proxy must be a valid http(s):// or socks5(h):// URL"
        ) from exc
    return text


class Qan8GmailApiClient:
    """HTTP-only adapter for QAN8's documented simple API."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        sku_id: int | str | None = None,
        request_timeout: float | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.api_base = str(
            api_base if api_base is not None else _config_value("QAN8_API_BASE", DEFAULT_QAN8_API_BASE)
        ).strip().rstrip("/")
        self.api_key = str(
            api_key if api_key is not None else _config_value("QAN8_API_KEY", "")
        ).strip()
        raw_sku = sku_id if sku_id is not None else _config_value("QAN8_GMAIL_SKU_ID", "")
        self.sku_id = str(raw_sku or "").strip()
        raw_timeout = request_timeout if request_timeout is not None else _config_value(
            "QAN8_REQUEST_TIMEOUT", DEFAULT_QAN8_REQUEST_TIMEOUT
        )
        try:
            self.request_timeout = max(1, int(raw_timeout))
        except (TypeError, ValueError):
            self.request_timeout = DEFAULT_QAN8_REQUEST_TIMEOUT
        raw_proxy = proxy_url if proxy_url is not None else _config_value(
            "QAN8_API_PROXY", DEFAULT_QAN8_API_PROXY
        )
        self.proxy_url = _normalize_proxy_url(raw_proxy)

    def list_products(self) -> list[dict]:
        payload = self._get("/api/v1/open/products", authenticated=False)
        data = self._payload_data(payload)
        if isinstance(data, dict):
            data = data.get("products")
        if not isinstance(data, list):
            raise Qan8GmailApiError("QAN8 products response is invalid")
        return [item for item in data if isinstance(item, dict)]

    def get_balance(self) -> dict:
        data = self._payload_data(
            self._get("/api/v1/open/balance", authenticated=True)
        )
        if not isinstance(data, dict):
            raise Qan8GmailApiError("QAN8 balance response is invalid")
        return dict(data)

    def create_order(self, out_order_no: str, *, quantity: int = 1) -> Qan8Order:
        if int(quantity) != 1:
            raise ValueError("QAN8 lazy acquisition requires quantity=1")
        self._require_credentials(require_sku=True)
        order_no = str(out_order_no or "").strip()
        if not order_no:
            raise ValueError("out_order_no is required")
        body = {
            "api_key": self.api_key,
            "sku_id": self._sku_value(),
            "quantity": 1,
            "out_order_no": order_no,
        }
        try:
            payload = self._request("POST", "/api/v1/open/orders", json=body)
        except Qan8GmailApiError:
            raise
        except Exception as exc:
            raise Qan8OrderUnknownError(
                f"QAN8 order outcome unknown for {order_no}; recheck the order before retrying"
            ) from exc
        return self._order_from_payload(payload, fallback_order_no=order_no)

    def get_order(self, order_no: str) -> Qan8Order:
        self._require_credentials()
        value = str(order_no or "").strip()
        if not value:
            raise ValueError("order_no is required")
        payload = self._get(
            f"/api/v1/open/orders/{value}",
            authenticated=True,
        )
        return self._order_from_payload(payload, fallback_order_no=value)

    def parse_delivery(self, delivery: object) -> list[Qan8SourceRecord]:
        if isinstance(delivery, str):
            lines = delivery.splitlines()
        elif isinstance(delivery, list) and all(isinstance(item, str) for item in delivery):
            lines = delivery
        else:
            raise Qan8DeliveryError("QAN8 delivery format is unsupported")

        records: list[Qan8SourceRecord] = []
        seen: set[tuple[str, str]] = set()
        for raw_line in lines:
            line = str(raw_line).strip()
            if not line or line.count("----") != 1:
                raise Qan8DeliveryError("QAN8 delivery must contain email----code_url records")
            email, code_url = (part.strip().lower() for part in line.split("----", 1))
            if not _GMAIL_EMAIL.fullmatch(email):
                raise Qan8DeliveryError("QAN8 delivery source email is not a Gmail address")
            parsed_url = urlsplit(code_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
                or any(char.isspace() for char in code_url)
            ):
                raise Qan8DeliveryError("QAN8 delivery code_url is invalid")
            key = (email, code_url)
            if key in seen:
                continue
            seen.add(key)
            records.append(Qan8SourceRecord(email=email, code_url=code_url))
        if not records:
            raise Qan8DeliveryError("QAN8 delivery is empty")
        return records

    def _require_credentials(self, *, require_sku: bool = False) -> None:
        if not self.api_base:
            raise Qan8GmailApiError("QAN8 API base is not configured")
        if not self.api_key:
            raise Qan8GmailApiError("QAN8 API key is not configured")
        if require_sku and not self.sku_id:
            raise Qan8GmailApiError("QAN8 Gmail SKU is not configured")

    def _sku_value(self) -> int | str:
        try:
            return int(self.sku_id)
        except (TypeError, ValueError):
            return self.sku_id

    def _get(self, path: str, *, authenticated: bool) -> object:
        if authenticated:
            self._require_credentials()
            return self._request("GET", path, params={"api_key": self.api_key})
        if not self.api_base:
            raise Qan8GmailApiError("QAN8 API base is not configured")
        return self._request("GET", path)

    def _request(self, method: str, path: str, **kwargs: object) -> object:
        url = f"{self.api_base}{path}"
        kwargs.setdefault("timeout", self.request_timeout)
        if self.proxy_url:
            kwargs.setdefault(
                "proxies",
                {"http": self.proxy_url, "https": self.proxy_url},
            )
        if method == "GET":
            response = requests.get(url, **kwargs)
        elif method == "POST":
            response = requests.post(url, **kwargs)
        else:
            raise ValueError(f"Unsupported QAN8 method: {method}")
        status_code = int(response.status_code)
        if not 200 <= status_code < 300:
            detail = ""
            try:
                error_payload = response.json()
            except (TypeError, ValueError):
                error_payload = None
            if isinstance(error_payload, dict):
                detail = str(
                    error_payload.get("message")
                    or error_payload.get("error")
                    or error_payload.get("msg")
                    or ""
                ).strip()
            if not detail:
                detail = str(getattr(response, "text", "") or "").strip()
            detail = detail.replace(self.api_key, "<redacted>")[:200]
            raise Qan8GmailApiError(f"QAN8 HTTP {status_code}: {detail}")
        response.raise_for_status()
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise Qan8GmailApiError("QAN8 response is not valid JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise Qan8GmailApiError("QAN8 response has an invalid JSON shape")
        return payload

    @staticmethod
    def _payload_data(payload: object) -> object:
        if not isinstance(payload, dict):
            return payload
        if payload.get("success") is False or payload.get("ok") is False:
            message = str(payload.get("message") or "QAN8 provider rejected the request")
            raise Qan8GmailApiError(f"QAN8 provider error: {message[:200]}")
        return payload.get("data", payload)

    def _order_from_payload(self, payload: object, *, fallback_order_no: str) -> Qan8Order:
        data = self._payload_data(payload)
        if not isinstance(data, dict):
            raise Qan8GmailApiError("QAN8 order response is invalid")
        order_no = str(data.get("order_no") or fallback_order_no).strip()
        status = str(data.get("status") or "processing").strip().lower()
        message = str(data.get("message") or "")[:200]
        order_id = data.get("order_id")
        return Qan8Order(
            order_no=order_no,
            status=status,
            delivery=data.get("delivery"),
            message=message,
            order_id=str(order_id) if order_id is not None else None,
        )
