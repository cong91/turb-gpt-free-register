"""OTPGmail public API client used by the registration email provider.

One remote OTPGmail order owns one mailbox and one OTP stream.  The mailbox is
expanded locally into twelve Gmail/Googlemail aliases; those aliases must keep
the same ``order_id`` for the lifetime of the order.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import requests

from core.gmail_aliases import GmailAliasError, generate_gmail_dual_domain_variants
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://otpgmail.net"
# OTPGmail currently exposes OpenAI/ChatGPT as service code ``dr``.
DEFAULT_SERVICE_CODE = "dr"
DEFAULT_REQUEST_TIMEOUT = 15
DEFAULT_POLL_INTERVAL = 3
DEFAULT_MAX_WAIT = 120
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503})
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_CANCELLED_ORDER_STATUSES = frozenset({
    "cancelled",
    "canceled",
    "order_cancelled",
    "order_canceled",
    "order_expired",
    "cancelled_by_provider",
    "canceled_by_provider",
    "expired",
})
_MAX_ORDER_RECOVERY_ATTEMPTS = 3
# v1 is the canonical fan-out format (one row per local alias).  v2 was the
# temporary one-alias-per-order format; read it for restart recovery but never
# let it override a canonical v1 context.
_STATE_KEY = "otpmail.orders.v1"
_LEGACY_STATE_KEY = "otpmail.orders.v2"
_ALIASES_PER_ORDER = 12


class OtpGmailError(RuntimeError):
    """OTPGmail request, order, or OTP polling failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class OtpGmailAccount:
    """One claimed Gmail address backed by one rented OTPGmail order."""

    email: str
    order_id: str
    service_code: str
    query_email: str = ""
    aliases: tuple[str, ...] = ()
    state: str = "reserved"
    order_status: str = "waiting_code"
    seen_codes: set[str] = field(default_factory=set)


_LOCK = threading.RLock()
_CONTEXT_CACHE: dict[str, OtpGmailAccount] = {}
_POOL: list[OtpGmailAccount] = []
_ORDER_LOCKS: dict[str, threading.RLock] = {}
_PREFLIGHTED_ORDERS: set[str] = set()
_STATE_LOADED = False


def _cache_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _order_key(value: object) -> str:
    return str(value or "").strip()


def _normalize_aliases(
    email: str,
    raw_aliases: object,
    *,
    include_email: bool = True,
) -> tuple[str, ...]:
    """Normalize a persisted alias list without crossing Gmail order boundaries."""
    values = (
        [str(alias or "").strip().lower() for alias in raw_aliases]
        if isinstance(raw_aliases, (list, tuple))
        else []
    )
    normalized_email = str(email or "").strip().lower()
    if include_email and normalized_email not in values:
        values.insert(0, normalized_email)
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in values:
        key = _cache_key(alias)
        if not key or key in seen or not _is_gmail(alias):
            continue
        seen.add(key)
        normalized.append(alias)
        if len(normalized) >= _ALIASES_PER_ORDER:
            break
    return tuple(normalized)


def _merge_aliases(existing: tuple[str, ...], incoming: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for alias in (*existing, *incoming):
        key = _cache_key(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        values.append(alias)
        if len(values) >= _ALIASES_PER_ORDER:
            break
    return tuple(values)


def _matches_order(account: OtpGmailAccount, expected_order_id: str | None) -> bool:
    expected = _order_key(expected_order_id)
    return not expected or account.order_id == expected


def _config() -> tuple[str, str, str, int, int, int]:
    from config import email as email_config

    base_url = str(getattr(email_config, "OTPGMAIL_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE).strip()
    api_key = str(getattr(email_config, "OTPGMAIL_API_KEY", "") or "").strip()
    service_code = str(
        getattr(email_config, "OTPGMAIL_SERVICE_CODE", DEFAULT_SERVICE_CODE)
        or DEFAULT_SERVICE_CODE
    ).strip()
    try:
        timeout = int(getattr(email_config, "OTPGMAIL_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT) or DEFAULT_REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_REQUEST_TIMEOUT
    try:
        interval = int(getattr(email_config, "OTPGMAIL_POLL_INTERVAL", DEFAULT_POLL_INTERVAL) or DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    try:
        max_wait = int(getattr(email_config, "OTPGMAIL_OTP_MAX_WAIT", DEFAULT_MAX_WAIT) or DEFAULT_MAX_WAIT)
    except (TypeError, ValueError):
        max_wait = DEFAULT_MAX_WAIT
    return base_url, api_key, service_code, max(1, min(120, timeout)), max(1, min(30, interval)), max(0, min(1800, max_wait))


def _endpoint(base_url: str, path: str) -> str:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OtpGmailError("Địa chỉ OTPGmail API không hợp lệ, hãy cấu hình OTPGMAIL_API_BASE")
    return f"{str(base_url).strip().rstrip('/')}/{path.lstrip('/')}"


def _retry_after(response: requests.Response) -> float | None:
    value = str(response.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, min(120.0, float(value))) if value else None
    except (TypeError, ValueError):
        return None


def _error_fields(payload: object) -> tuple[str | None, str]:
    if not isinstance(payload, dict):
        return None, "dịch vụ không trả về lỗi chi tiết"
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip() or None
        message = str(error.get("message") or error.get("detail") or "请求失败").strip()
        return code, message
    return None, str(payload.get("message") or payload.get("detail") or "请求失败").strip()


def _parse_payload(response: requests.Response, path: str) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise OtpGmailError(
            f"OTPGmail 响应不是 JSON: {path} (HTTP {response.status_code})",
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
            retry_after=_retry_after(response),
        ) from exc
    if not isinstance(payload, dict):
        raise OtpGmailError(
            f"OTPGmail 响应格式无效: {path}",
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
        )
    return payload


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    base_url, api_key, _, timeout, _, _ = _config()
    if not api_key:
        raise OtpGmailError("OTPGmail API Key 未配置，请填写 OTPGMAIL_API_KEY")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    url = _endpoint(base_url, path)
    try:
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OtpGmailError(
            f"OTPGmail 请求失败: {method.upper()} {path}",
            retryable=True,
        ) from exc
    payload = _parse_payload(response, path)
    if response.status_code < 200 or response.status_code >= 300 or payload.get("success") is not True:
        error_code, message = _error_fields(payload)
        status_code = response.status_code
        retryable = status_code in _RETRYABLE_STATUS_CODES
        raise OtpGmailError(
            f"OTPGmail 请求失败: HTTP {status_code}; {message}",
            status_code=status_code,
            error_code=error_code,
            retryable=retryable,
            retry_after=_retry_after(response),
        )
    return payload


def _ensure_state_loaded() -> None:
    global _STATE_LOADED
    if _STATE_LOADED:
        return
    from core.app_state_db import get_named_document

    order_accounts: dict[str, list[OtpGmailAccount]] = {}
    order_aliases: dict[str, tuple[str, ...]] = {}
    order_seen_codes: dict[str, set[str]] = {}
    # Canonical v1 is read first.  If the same alias is present in the legacy
    # v2 document, retaining the v1 context prevents a stale one-order record
    # from overwriting its twelve-alias order mapping.
    for state_key in (_STATE_KEY, _LEGACY_STATE_KEY):
        raw = get_named_document(state_key, default=[])
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            order_id = _order_key(item.get("order_id") or item.get("orderId"))
            service_code = str(item.get("service_code") or DEFAULT_SERVICE_CODE).strip()
            if not email or not order_id or not _is_gmail(email):
                continue
            query_email = str(item.get("query_email") or email).strip().lower()
            if not _is_gmail(query_email):
                continue
            state = str(item.get("state") or "available").strip().lower()
            # A process can die after claiming an order. Re-open only the local
            # reservation; the remote order remains identified by order_id.
            if state == "reserved":
                state = "available"
            raw_aliases = item.get("aliases")
            aliases = _normalize_aliases(email, raw_aliases, include_email=False)
            if raw_aliases and _cache_key(email) not in {
                _cache_key(alias) for alias in aliases
            }:
                logger.warning(
                    "[OTPGmail] bỏ qua persisted alias không thuộc order=%s: %s",
                    order_id,
                    email,
                )
                continue
            if len(aliases) < _ALIASES_PER_ORDER:
                try:
                    generated_aliases = tuple(
                        generate_gmail_dual_domain_variants(
                            query_email,
                            _ALIASES_PER_ORDER,
                        )
                    )
                except GmailAliasError:
                    generated_aliases = ()
                if len(generated_aliases) == _ALIASES_PER_ORDER:
                    aliases = _normalize_aliases(
                        query_email,
                        generated_aliases,
                        include_email=False,
                    )
                    if _cache_key(email) not in {
                        _cache_key(alias) for alias in aliases
                    }:
                        logger.warning(
                            "[OTPGmail] bỏ qua persisted email ngoài alias set order=%s: %s",
                            order_id,
                            email,
                        )
                        continue
            if not aliases:
                continue
            raw_seen_codes = item.get("seen_codes", [])
            if not isinstance(raw_seen_codes, (list, tuple, set)):
                raw_seen_codes = []
            seen_codes = {
                str(code).strip()
                for code in raw_seen_codes
                if str(code).strip()
            }
            shared_codes = order_seen_codes.setdefault(order_id, set())
            shared_codes.update(seen_codes)

            existing = _CONTEXT_CACHE.get(_cache_key(email))
            if existing is not None:
                if existing.order_id != order_id:
                    logger.warning(
                        "[OTPGmail] bỏ qua alias trùng giữa order=%s và order=%s: %s",
                        existing.order_id,
                        order_id,
                        email,
                    )
                    continue
                # Duplicate rows are expected in v1 (one row per alias, each
                # carrying the complete alias set). Merge only the order-level
                # inventory and OTP identities; never replace its order id.
                merged = _merge_aliases(existing.aliases, aliases)
                order_aliases[order_id] = merged
                for member in order_accounts.get(order_id, []):
                    member.aliases = merged
                existing.seen_codes = shared_codes
                continue

            existing_order_accounts = order_accounts.setdefault(order_id, [])
            merged_aliases = _merge_aliases(order_aliases.get(order_id, ()), aliases)
            order_aliases[order_id] = merged_aliases
            account = OtpGmailAccount(
                email=email,
                order_id=order_id,
                service_code=service_code,
                query_email=query_email,
                aliases=merged_aliases,
                state=state,
                order_status=str(item.get("order_status") or "waiting_code").strip().lower(),
                seen_codes=shared_codes,
            )
            # A malformed persisted row may list an alias already owned by a
            # different order. Keep the first canonical binding and discard
            # this row instead of making retrieval depend on load order.
            merged_keys = {_cache_key(alias) for alias in merged_aliases}
            collision = next(
                (
                    other
                    for other in _POOL
                    if other.order_id != order_id
                    and (
                        _cache_key(other.email) in merged_keys
                        or bool(merged_keys.intersection(_cache_key(alias) for alias in other.aliases))
                    )
                ),
                None,
            )
            if collision is not None:
                logger.warning(
                    "[OTPGmail] bỏ qua alias thuộc order khác: order=%s alias=%s",
                    order_id,
                    email,
                )
                continue
            existing_order_accounts.append(account)
            _POOL.append(account)
            _CONTEXT_CACHE[_cache_key(email)] = account
            for member in existing_order_accounts:
                member.aliases = merged_aliases
    # A crash can leave only one persisted row even though its alias list
    # already identifies the complete order. Materialize the missing local
    # slots so the next twelve claims still come from this same order.
    for order_id, aliases in order_aliases.items():
        existing_accounts = order_accounts.get(order_id, [])
        if not existing_accounts:
            continue
        template = existing_accounts[0]
        existing_keys = {_cache_key(item.email) for item in existing_accounts}
        for alias in aliases:
            alias_key = _cache_key(alias)
            if alias_key in existing_keys:
                continue
            account = OtpGmailAccount(
                email=alias,
                order_id=order_id,
                service_code=template.service_code,
                query_email=template.query_email,
                aliases=aliases,
                state=template.state,
                order_status=template.order_status,
                seen_codes=template.seen_codes,
            )
            _POOL.append(account)
            existing_accounts.append(account)
            _CONTEXT_CACHE[alias_key] = account
            existing_keys.add(alias_key)
    _STATE_LOADED = True


def _persist_state() -> None:
    from core.app_state_db import set_named_document

    payload = [
        {
            "email": account.email,
            "order_id": account.order_id,
            "service_code": account.service_code,
            "query_email": account.query_email or account.email,
            "aliases": list(account.aliases or (account.email,)),
            "state": account.state,
            "order_status": account.order_status,
            "seen_codes": sorted(account.seen_codes),
        }
        for account in _POOL
    ]
    set_named_document(_STATE_KEY, payload)


def _is_gmail(email: str) -> bool:
    return email.count("@") == 1 and email.rsplit("@", 1)[1].casefold() in _GMAIL_DOMAINS


def _is_cancelled_order_status(value: object) -> bool:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in _CANCELLED_ORDER_STATUSES


def _is_cancelled_order_error(error: BaseException) -> bool:
    error_code = str(getattr(error, "error_code", "") or "").strip().casefold().replace("-", "_")
    if error_code in {"order_cancelled", "order_canceled", "order_expired", "cancelled", "canceled"}:
        return True
    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "order đã bị hủy",
            "order da bi huy",
            "order cancelled",
            "order canceled",
            "order_cancelled",
            "order_canceled",
        )
    )


def _order_data(payload: dict, path: str) -> dict:
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise OtpGmailError(f"OTPGmail 响应缺少订单数据: {path}")
    return data


def _validate_order(data: dict) -> tuple[str, str]:
    email = str(data.get("email") or "").strip().lower()
    order_id = str(data.get("orderId") or data.get("order_id") or data.get("id") or "").strip()
    if not order_id or not _is_gmail(email):
        raise OtpGmailError("OTPGmail 未返回有效 Gmail order")
    return email, order_id


def _timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        number = None
    if number is not None:
        return number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _otp_entries(order: dict) -> list[tuple[str, object, str]]:
    raw_entries = order.get("otp")
    if not isinstance(raw_entries, list):
        return []
    entries: list[tuple[str, object, str]] = []
    for item in raw_entries:
        if isinstance(item, dict):
            raw_code = item.get("code")
            received_at = item.get("receivedAt") or item.get("received_at")
            text = str(raw_code or item.get("text") or item.get("content") or "").strip()
            code = text if re.fullmatch(r"\d{6}", text) else extract_otp({"text": text})
        else:
            received_at = None
            text = str(item or "").strip()
            code = text if re.fullmatch(r"\d{6}", text) else extract_otp({"text": text})
        if not code:
            continue
        identity = f"{received_at}:{code}" if received_at not in (None, "") else f"code:{code}"
        entries.append((code, received_at, identity))
    return entries


def _account_copy(account: OtpGmailAccount) -> OtpGmailAccount:
    return OtpGmailAccount(
        email=account.email,
        order_id=account.order_id,
        service_code=account.service_code,
        query_email=account.query_email or account.email,
        aliases=account.aliases or (account.email,),
        state=account.state,
        order_status=account.order_status,
        seen_codes=account.seen_codes,
    )


def _order_lock(order_id: str) -> threading.RLock:
    with _LOCK:
        return _ORDER_LOCKS.setdefault(str(order_id), threading.RLock())


def registration_order_lock(email: str, order_id: str | None = None) -> threading.RLock:
    """Return the reentrant lock for one OTPGmail order."""
    account = get_account_context(email, order_id=order_id)
    order_id = account.order_id if account else ""
    if not order_id:
        raise OtpGmailError(f"OTPGmail order context not found: {email}")
    return _order_lock(order_id)


def _order_accounts(order_id: str) -> list[OtpGmailAccount]:
    return [account for account in _POOL if account.order_id == str(order_id)]


def _set_order_status(order_id: str, status: str) -> None:
    for account in _order_accounts(order_id):
        account.order_status = status


def _fail_order(order_id: str, *, status: str = "cancelled") -> None:
    for account in _order_accounts(order_id):
        account.state = "failed"
        account.order_status = status
    _PREFLIGHTED_ORDERS.discard(str(order_id))


def _find_context(email: str, order_id: str | None = None) -> OtpGmailAccount | None:
    normalized_email = str(email or "").strip().lower()
    if not _is_gmail(normalized_email):
        return None
    account = _CONTEXT_CACHE.get(_cache_key(normalized_email))
    if account is not None:
        return account if _matches_order(account, order_id) else None
    all_matching_orders = [
        item
        for item in _POOL
        if _cache_key(normalized_email) in {_cache_key(alias) for alias in item.aliases}
    ]
    expected_order_id = _order_key(order_id)
    if expected_order_id:
        matching_orders = [
            item for item in all_matching_orders if item.order_id == expected_order_id
        ]
        if all_matching_orders and not matching_orders:
            return None
    else:
        matching_orders = all_matching_orders
    matching_order_ids = {item.order_id for item in matching_orders}
    if len(matching_order_ids) > 1:
        logger.warning(
            "[OTPGmail] alias mapping is ambiguous across orders: email=%s orders=%s",
            normalized_email,
            sorted(matching_order_ids),
        )
        return None
    loaded_order = matching_orders[0] if matching_orders else None
    if loaded_order is not None:
        if not _matches_order(loaded_order, order_id):
            return None
        account = OtpGmailAccount(
            email=normalized_email,
            order_id=loaded_order.order_id,
            service_code=loaded_order.service_code,
            query_email=loaded_order.query_email or normalized_email,
            aliases=loaded_order.aliases,
            state=loaded_order.state,
            order_status=loaded_order.order_status,
            seen_codes=loaded_order.seen_codes,
        )
        _POOL.append(account)
        _CONTEXT_CACHE[_cache_key(normalized_email)] = account
        _persist_state()
        return account
    try:
        from core import db

        row = db.get_account_by_email(normalized_email)
        raw = row.get("extra_json") if isinstance(row, dict) else None
        extra = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
        service = extra.get("email_service") if isinstance(extra, dict) else None
        if not isinstance(service, dict) or str(service.get("source") or "").strip().lower() != "otpmail":
            return None
        saved_order_id = _order_key(service.get("order_id") or service.get("orderId"))
        service_code = str(service.get("service_code") or DEFAULT_SERVICE_CODE).strip()
        query_email = str(service.get("query_email") or normalized_email).strip().lower()
        raw_aliases = service.get("aliases")
        if not raw_aliases:
            try:
                aliases = _normalize_aliases(
                    query_email,
                    generate_gmail_dual_domain_variants(
                        query_email,
                        _ALIASES_PER_ORDER,
                    ),
                    include_email=False,
                )
            except GmailAliasError:
                aliases = ()
        else:
            aliases = _normalize_aliases(query_email, raw_aliases, include_email=False)
            if raw_aliases and _cache_key(normalized_email) not in {
                _cache_key(alias) for alias in aliases
            }:
                return None
        if _cache_key(normalized_email) not in {
            _cache_key(alias) for alias in aliases
        }:
            return None
        if len(aliases) < _ALIASES_PER_ORDER:
            try:
                generated_aliases = generate_gmail_dual_domain_variants(
                    query_email,
                    _ALIASES_PER_ORDER,
                )
            except GmailAliasError:
                generated_aliases = ()
            if generated_aliases:
                aliases = _normalize_aliases(
                    query_email,
                    generated_aliases,
                    include_email=False,
                )
                if _cache_key(normalized_email) not in {
                    _cache_key(alias) for alias in aliases
                }:
                    return None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not saved_order_id or not _is_gmail(query_email):
        return None
    expected_order_id = _order_key(order_id)
    if expected_order_id and expected_order_id != saved_order_id:
        return None
    if aliases and normalized_email not in {_cache_key(alias) for alias in aliases}:
        return None
    alias_keys = {_cache_key(alias) for alias in aliases}
    if any(
        item.order_id != saved_order_id
        and alias_keys.intersection(
            {_cache_key(item.email), *(_cache_key(alias) for alias in item.aliases)}
        )
        for item in _POOL
    ):
        return None
    existing_order = next((item for item in _POOL if item.order_id == saved_order_id), None)
    if existing_order is not None:
        if normalized_email not in {_cache_key(alias) for alias in existing_order.aliases}:
            return None
        existing_alias = next(
            (
                item
                for item in _order_accounts(saved_order_id)
                if _cache_key(item.email) == _cache_key(normalized_email)
            ),
            None,
        )
        if existing_alias is not None:
            _CONTEXT_CACHE[_cache_key(normalized_email)] = existing_alias
            return existing_alias
        account = OtpGmailAccount(
            email=normalized_email,
            order_id=saved_order_id,
            service_code=existing_order.service_code,
            query_email=existing_order.query_email or query_email,
            aliases=existing_order.aliases,
            state="used",
            order_status=existing_order.order_status,
            seen_codes=existing_order.seen_codes,
        )
        _POOL.append(account)
        _CONTEXT_CACHE[_cache_key(normalized_email)] = account
        _persist_state()
        return account
    account = OtpGmailAccount(
        email=normalized_email,
        order_id=saved_order_id,
        service_code=service_code,
        query_email=query_email,
        aliases=aliases or (normalized_email,),
        state="used",
    )
    _POOL.append(account)
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    _persist_state()
    return account


def get_account_context(email: str, order_id: str | None = None) -> OtpGmailAccount | None:
    with _LOCK:
        _ensure_state_loaded()
        return _find_context(email, order_id=order_id)


def _get_order(account: OtpGmailAccount) -> dict:
    payload = _request("GET", f"/v1/orders/{account.order_id}")
    order = _order_data(payload, f"/v1/orders/{account.order_id}")
    returned_id = str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip()
    email = str(order.get("email") or "").strip().lower()
    if returned_id != account.order_id:
        raise OtpGmailError("OTPGmail 返回了不匹配的 order")
    expected_email = account.query_email or account.email
    if email != expected_email:
        raise OtpGmailError("OTPGmail 返回了不匹配的 Gmail")
    return order


def _preflight(account: OtpGmailAccount) -> None:
    order = _get_order(account)
    account.order_status = str(order.get("status") or account.order_status).strip().lower()
    _set_order_status(account.order_id, account.order_status)
    account.seen_codes.update(identity for _, _, identity in _otp_entries(order))
    if _is_cancelled_order_status(account.order_status):
        raise OtpGmailError("OTPGmail order đã bị hủy", error_code="ORDER_CANCELLED")
    with _LOCK:
        _persist_state()


def _create_order() -> list[OtpGmailAccount]:
    _, _, service_code, _, interval, _ = _config()
    if not service_code:
        raise OtpGmailError("OTPGmail service code 未配置，请填写 OTPGMAIL_SERVICE_CODE")
    idempotency_key = str(uuid.uuid4())
    payload = None
    for attempt in range(3):
        try:
            payload = _request(
                "POST",
                "/v1/orders",
                json_body={"service": service_code, "quantity": 1},
                idempotency_key=idempotency_key,
            )
            break
        except OtpGmailError as exc:
            if not exc.retryable or attempt == 2:
                raise
            delay = exc.retry_after if exc.retry_after is not None else interval
            logger.warning("[OTPGmail] order creation retry %s/3 after %.1fs", attempt + 1, delay)
            time.sleep(delay)
    if payload is None:
        raise OtpGmailError("OTPGmail order creation returned no response")
    data = _order_data(payload, "/v1/orders")
    email, order_id = _validate_order(data)
    def cancel_unusable_order() -> None:
        unusable = OtpGmailAccount(
            email=email,
            order_id=order_id,
            service_code=service_code,
            query_email=email,
            aliases=(email,),
            state="failed",
            order_status="cancelled",
        )
        if not _cancel_order(unusable):
            logger.warning(
                "[OTPGmail] cannot cancel order with incomplete aliases: order=%s",
                order_id,
            )
    try:
        aliases = tuple(generate_gmail_dual_domain_variants(email, _ALIASES_PER_ORDER))
    except GmailAliasError as exc:
        cancel_unusable_order()
        raise OtpGmailError("OTPGmail 返回的 Gmail 无法生成 12 个 alias") from exc
    if len(aliases) != _ALIASES_PER_ORDER:
        cancel_unusable_order()
        raise OtpGmailError("OTPGmail 未生成完整的 12 个 Gmail alias")
    shared_codes: set[str] = set()
    order_status = str(data.get("status") or "waiting_code").strip().lower()
    accounts = [
        OtpGmailAccount(
            email=alias,
            order_id=order_id,
            service_code=service_code,
            query_email=email,
            aliases=aliases,
            state="available",
            order_status=order_status,
            seen_codes=shared_codes,
        )
        for alias in aliases
    ]
    account = accounts[0]
    alias_keys = {_cache_key(alias) for alias in aliases}
    if any(
        item.order_id == order_id
        or alias_keys.intersection(
            {_cache_key(item.email), *(_cache_key(alias) for alias in item.aliases)}
        )
        for item in _POOL
    ):
        # The public provider API addresses OTP retrieval by email/order. A
        # repeated email would make the existing email context ambiguous, so
        # cancel the new paid order before asking for another one.
        account.order_status = "cancelled"
        if not _cancel_order(account):
            logger.warning(
                "[OTPGmail] duplicate email order cancel failed: order=%s email=%s",
                order_id,
                email,
            )
        raise OtpGmailError(
            "OTPGmail 返回了已分配的 Gmail/order，拒绝复用",
            error_code="DUPLICATE_EMAIL",
        )
    for item in accounts:
        _CONTEXT_CACHE[_cache_key(item.email)] = item
    logger.info("[OTPGmail] allocated order=%s email=%s aliases=%s", order_id, email, len(accounts))
    return accounts


def pick_account() -> OtpGmailAccount:
    """Claim one available order or rent a new Gmail order."""
    with _LOCK:
        _ensure_state_loaded()
        for account in _POOL:
            if account.state != "available":
                continue
            account.state = "reserved"
            _persist_state()
            try:
                if account.order_id not in _PREFLIGHTED_ORDERS:
                    _preflight(account)
                    _PREFLIGHTED_ORDERS.add(account.order_id)
            except OtpGmailError as exc:
                if _is_cancelled_order_error(exc) or exc.status_code in {404, 410}:
                    _fail_order(
                        account.order_id,
                        status="cancelled" if _is_cancelled_order_error(exc) else "missing",
                    )
                    logger.warning(
                        "[OTPGmail] bỏ qua order hỏng %s (%s), cấp order mới",
                        account.order_id,
                        exc,
                    )
                    _persist_state()
                    continue
                account.state = "failed"
                _persist_state()
                raise
            logger.info("[OTPGmail] claimed order=%s email=%s", account.order_id, account.email)
            return _account_copy(account)

        last_cancelled: OtpGmailError | None = None
        for attempt in range(_MAX_ORDER_RECOVERY_ATTEMPTS):
            try:
                accounts = _create_order()
            except OtpGmailError as exc:
                if getattr(exc, "error_code", None) == "DUPLICATE_EMAIL":
                    last_cancelled = exc
                    logger.warning(
                        "[OTPGmail] duplicate Gmail rejected, thử order mới %s/%s",
                        attempt + 1,
                        _MAX_ORDER_RECOVERY_ATTEMPTS,
                    )
                    continue
                raise
            _POOL.extend(accounts)
            _persist_state()
            account = accounts[0]
            try:
                _preflight(account)
                _PREFLIGHTED_ORDERS.add(account.order_id)
            except OtpGmailError as exc:
                if _is_cancelled_order_error(exc) or exc.status_code in {404, 410}:
                    status = "cancelled" if _is_cancelled_order_error(exc) else "missing"
                    _fail_order(account.order_id, status=status)
                    _persist_state()
                    last_cancelled = exc
                    logger.warning(
                        "[OTPGmail] order %s không dùng được (%s), thử order mới %s/%s",
                        account.order_id,
                        exc,
                        attempt + 1,
                        _MAX_ORDER_RECOVERY_ATTEMPTS,
                    )
                    continue
                for item in accounts:
                    item.state = "failed"
                _persist_state()
                # A newly-created order is paid inventory.  Cancel it when the
                # initial consistency check fails so a bad response cannot leave
                # an untracked order waiting for auto-expiry.
                _cancel_order(account)
                _persist_state()
                raise
            account.state = "reserved"
            _persist_state()
            logger.info("[OTPGmail] claimed order=%s email=%s", account.order_id, account.email)
            return _account_copy(account)
        if last_cancelled is not None:
            raise OtpGmailError(
                f"OTPGmail liên tiếp trả order đã hủy, đã thử {_MAX_ORDER_RECOVERY_ATTEMPTS} order mới",
                error_code="ORDER_CANCELLED",
            ) from last_cancelled
        raise OtpGmailError("OTPGmail không thể cấp order mới")


def _latest_candidate(
    account: OtpGmailAccount,
    order: dict,
    *,
    after_ts: float | None,
    before_code: str | None,
) -> tuple[str, str] | None:
    for code, received_at, identity in reversed(_otp_entries(order)):
        if before_code and code == str(before_code).strip():
            continue
        timestamp = _timestamp(received_at)
        if after_ts is not None and timestamp is not None and timestamp <= after_ts:
            continue
        if identity in account.seen_codes:
            continue
        return code, identity
    return None


def snapshot_verification_code(email: str, order_id: str | None = None) -> str | None:
    account = get_account_context(email, order_id=order_id)
    if account is None:
        return None
    order = _get_order(account)
    entries = _otp_entries(order)
    return entries[-1][0] if entries else None


def acknowledge_verification_code(email: str, otp: str, order_id: str | None = None) -> None:
    account = get_account_context(email, order_id=order_id)
    if account is None:
        return
    code = str(otp or "").strip()
    if not code:
        return
    order = _get_order(account)
    identities = [identity for value, _, identity in _otp_entries(order) if value == code]
    account.seen_codes.update(identities or {f"code:{code}"})
    with _LOCK:
        _persist_state()


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    before_code: str | None = None,
    order_id: str | None = None,
) -> str:
    """Poll the order until a new six-digit OTP is available."""
    del settle_seconds  # OTPGmail returns an ordered list; no settle is needed.
    account = get_account_context(email, order_id=order_id)
    if account is None:
        expected = _order_key(order_id)
        suffix = f" for order {expected}" if expected else ""
        raise OtpGmailError(f"OTPGmail order context not found: {email}{suffix}")
    _, _, _, _, configured_interval, configured_wait = _config()
    wait_seconds = max(0, int(max_wait if max_wait is not None else configured_wait))
    interval = max(1, int(poll_interval if poll_interval is not None else configured_interval))
    deadline = time.monotonic() + wait_seconds
    last_error = "尚未收到 OTP"
    retry_delay: float | None = None
    with _order_lock(account.order_id):
        first_poll = True
        while first_poll or time.monotonic() <= deadline:
            first_poll = False
            try:
                order = _get_order(account)
                account.order_status = str(order.get("status") or account.order_status).strip().lower()
                if _is_cancelled_order_status(account.order_status):
                    raise OtpGmailError("OTPGmail order đã bị hủy", error_code="ORDER_CANCELLED")
                candidate = _latest_candidate(
                    account,
                    order,
                    after_ts=after_ts,
                    before_code=before_code,
                )
                if candidate:
                    code, identity = candidate
                    account.seen_codes.add(identity)
                    with _LOCK:
                        _persist_state()
                    return code
                with _LOCK:
                    _persist_state()
                retry_delay = None
            except OtpGmailError as exc:
                if _is_cancelled_order_error(exc):
                    with _LOCK:
                        _fail_order(account.order_id)
                        _persist_state()
                    raise
                if not exc.retryable:
                    with _LOCK:
                        account.state = "failed"
                        _persist_state()
                    raise
                last_error = str(exc)
                retry_delay = exc.retry_after
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = retry_delay if retry_delay is not None else interval
            time.sleep(min(delay, remaining))
    raise OtpGmailError(f"等待 OTPGmail 验证码超时: {email}; {last_error}")


def _cancel_order(account: OtpGmailAccount) -> bool:
    try:
        _request("POST", f"/v1/orders/{account.order_id}/cancel")
    except OtpGmailError as exc:
        logger.warning("[OTPGmail] order cancel failed: HTTP %s code=%s", exc.status_code or "-", exc.error_code or "-")
        return False
    _set_order_status(account.order_id, "cancelled")
    return True


def release_account(
    email: str,
    status: str = "available",
    note: str | None = None,
    order_id: str | None = None,
) -> bool:
    """Finish an order; cancel it when it has not received an OTP."""
    with _LOCK:
        _ensure_state_loaded()
        account = get_account_context(email, order_id=order_id)
        if account is None:
            return False
        normalized = str(status or "available").strip().lower()
        cleanup_succeeded = True
        if note and _is_cancelled_order_error(OtpGmailError(str(note))):
            _fail_order(account.order_id)
            _persist_state()
            return True
        account.state = "used" if normalized == "used" else "failed"
        order_accounts = _order_accounts(account.order_id)
        order_finished = all(item.state not in {"available", "reserved"} for item in order_accounts)
        if (
            normalized != "used"
            and not account.seen_codes
            and order_finished
            and account.order_status == "waiting_code"
        ):
            cleanup_succeeded = _cancel_order(account)
        _persist_state()
        return cleanup_succeeded


def list_accounts(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        _ensure_state_loaded()
        wanted = str(status or "").strip().lower()
        rows = [
            {
                "email": account.email,
                "source": "otpmail",
                "status": account.state,
                "order_id": account.order_id,
                "order_status": account.order_status,
                "service_code": account.service_code,
            }
            for account in _POOL
            if not wanted or account.state == wanted
        ]
        return rows[: max(1, int(limit or 1))]


def pool_summary() -> dict[str, int]:
    with _LOCK:
        _ensure_state_loaded()
        return {
            "total": len(_POOL),
            "available": sum(account.state == "available" for account in _POOL),
            "used": sum(account.state == "used" for account in _POOL),
            "failed": sum(account.state == "failed" for account in _POOL),
        }


def delete_account(email: str) -> bool:
    with _LOCK:
        _ensure_state_loaded()
        account = _CONTEXT_CACHE.pop(_cache_key(email), None)
        if account is None:
            return False
        _POOL[:] = [item for item in _POOL if item is not account]
        _persist_state()
        return True


def mark_account_consumed(email: str, order_id: str | None = None) -> bool:
    return release_account(email, status="used", order_id=order_id)


def get_account_context_metadata(email: str, order_id: str | None = None) -> dict | None:
    account = get_account_context(email, order_id=order_id)
    if account is None:
        return None
    return {
        "source": "otpmail",
        "email": account.email,
        "query_email": account.query_email or account.email,
        "order_id": account.order_id,
        "service_code": account.service_code,
        "aliases": list(account.aliases or (account.email,)),
    }


def list_services() -> list[dict]:
    payload = _request("GET", "/v1/services")
    data = payload.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def get_quota() -> dict:
    payload = _request("GET", "/v1/balance")
    data = payload.get("data")
    return dict(data) if isinstance(data, dict) else {}


def reset_runtime_state(*, clear_persisted: bool = False) -> None:
    global _STATE_LOADED
    with _LOCK:
        _CONTEXT_CACHE.clear()
        _POOL.clear()
        _ORDER_LOCKS.clear()
        _PREFLIGHTED_ORDERS.clear()
        _STATE_LOADED = False
        if clear_persisted:
            from core.app_state_db import set_named_document

            set_named_document(_STATE_KEY, [])
            set_named_document(_LEGACY_STATE_KEY, [])
