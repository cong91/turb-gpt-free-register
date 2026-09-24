"""BambooMMO rented Gmail provider client."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from core.gmail_aliases import GmailAliasError, generate_gmail_dual_domain_variants
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.bamboommo.com"
DEFAULT_SERVER = 2
DEFAULT_MAIL_TYPE = "GM"
DEFAULT_SERVICE = "OP"
DEFAULT_REQUEST_TIMEOUT = 20
DEFAULT_POLL_INTERVAL = 3
DEFAULT_MAX_WAIT = 120
_STATE_KEY = "bamboommo.rentals.v1"
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_CANCELLED_RENTAL_STATUSES = frozenset({
    "cancelled",
    "canceled",
    "cancel",
    "rental_cancelled",
    "rental_canceled",
    "cancelled_by_provider",
    "canceled_by_provider",
    "expired",
    "closed",
})
_CANCELLED_RENTAL_KEYS = frozenset({
    "cancelled",
    "canceled",
    "rental_cancelled",
    "rental_canceled",
    "rental_expired",
    "order_expired",
    "order_cancelled",
    "order_canceled",
    "cancelled_by_provider",
    "canceled_by_provider",
    "expired",
})
_MAX_RENTAL_RECOVERY_ATTEMPTS = 3


class BambooMmoError(RuntimeError):
    """BambooMMO request or rental failure."""

    def __init__(self, message: str, *, status_code: int | None = None, resource_key: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.resource_key = resource_key


@dataclass
class BambooMmoAccount:
    email: str
    rental_id: str
    server: int
    code_type_mail: str
    code_service: str
    query_email: str = ""
    aliases: tuple[str, ...] = ()
    state: str = "reserved"
    rental_status: str = "WAITING_OTP"
    can_request_next_otp: bool = False
    next_requested: bool = False
    seen_codes: set[str] = field(default_factory=set)


_LOCK = threading.RLock()
_CONTEXT_CACHE: dict[str, BambooMmoAccount] = {}
_POOL: list[BambooMmoAccount] = []
_RENTAL_LOCKS: dict[str, threading.RLock] = {}
_PREFLIGHTED_RENTALS: set[str] = set()
_STATE_LOADED = False


def _cache_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _config() -> tuple[str, str, int, str, str, int, int, int]:
    from config import email as email_config

    base_url = str(getattr(email_config, "BAMBOOMMO_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE).strip()
    api_key = str(getattr(email_config, "BAMBOOMMO_API_KEY", "") or "").strip()
    try:
        server = int(getattr(email_config, "BAMBOOMMO_SERVER", DEFAULT_SERVER) or DEFAULT_SERVER)
    except (TypeError, ValueError):
        server = DEFAULT_SERVER
    if server not in (1, 2):
        server = DEFAULT_SERVER
    mail_type = str(getattr(email_config, "BAMBOOMMO_MAIL_TYPE", DEFAULT_MAIL_TYPE) or DEFAULT_MAIL_TYPE).strip().upper()
    service = str(getattr(email_config, "BAMBOOMMO_SERVICE", DEFAULT_SERVICE) or DEFAULT_SERVICE).strip().upper()
    try:
        timeout = int(getattr(email_config, "BAMBOOMMO_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT) or DEFAULT_REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_REQUEST_TIMEOUT
    try:
        interval = int(getattr(email_config, "BAMBOOMMO_POLL_INTERVAL", DEFAULT_POLL_INTERVAL) or DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    try:
        max_wait = int(getattr(email_config, "BAMBOOMMO_OTP_MAX_WAIT", DEFAULT_MAX_WAIT) or DEFAULT_MAX_WAIT)
    except (TypeError, ValueError):
        max_wait = DEFAULT_MAX_WAIT
    return (
        base_url,
        api_key,
        server,
        mail_type,
        service,
        max(1, min(120, timeout)),
        max(1, min(30, interval)),
        max(0, min(1800, max_wait)),
    )


def _endpoint(base_url: str, path: str) -> str:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BambooMmoError("Địa chỉ BambooMMO API không hợp lệ")
    return f"{str(base_url).strip().rstrip('/')}/{path.lstrip('/')}"


def _response_error(payload: object) -> tuple[str | None, str]:
    if not isinstance(payload, dict):
        return None, "API không trả về JSON object"
    return (
        str(payload.get("resourceKey") or "").strip() or None,
        str(payload.get("message") or "BambooMMO API request failed").strip(),
    )


def _request(path: str, *, body: dict[str, object]) -> dict:
    base_url, api_key, _, _, _, timeout, _, _ = _config()
    if not api_key:
        raise BambooMmoError("BambooMMO API Key chưa được cấu hình")
    try:
        response = requests.request(
            "POST",
            _endpoint(base_url, path),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise BambooMmoError(f"BambooMMO request failed: {path}") from exc
    if not isinstance(payload, dict):
        raise BambooMmoError(f"BambooMMO response format invalid: {path}")
    status = payload.get("statusCode")
    resource_key, message = _response_error(payload)
    if response.status_code < 200 or response.status_code >= 300 or status != 200 or payload.get("isSuccessStatusCode") is False:
        raise BambooMmoError(
            f"BambooMMO API error {resource_key or 'UNKNOWN'}: {message}",
            status_code=int(status) if isinstance(status, int) else response.status_code,
            resource_key=resource_key,
        )
    return payload


def _is_gmail(email: str) -> bool:
    return email.count("@") == 1 and email.rsplit("@", 1)[1].casefold() in _GMAIL_DOMAINS


def _normalize_status(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _is_cancelled_rental_status(value: object) -> bool:
    normalized = _normalize_status(value)
    return normalized in _CANCELLED_RENTAL_STATUSES


def _is_cancelled_rental_error(error: BaseException) -> bool:
    resource_key = _normalize_status(getattr(error, "resource_key", ""))
    if (
        resource_key in _CANCELLED_RENTAL_KEYS
        or resource_key.endswith(("_cancelled", "_canceled", "_expired"))
    ):
        return True
    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "rental đã bị hủy",
            "rental da bi huy",
            "rental cancelled",
            "rental canceled",
            "rental_cancelled",
            "rental_canceled",
        )
    )


def _header(payload: dict) -> dict:
    value = payload.get("responseHeader")
    return value if isinstance(value, dict) else {}


def _update_rental_state(
    account: BambooMmoAccount,
    *,
    header: dict | None = None,
    next_requested: bool | None = None,
) -> None:
    """Keep all local aliases for one rental on the same remote state."""
    header = header or {}
    status = header.get("status")
    can_request = header.get("canRequestNextOtp")
    with _LOCK:
        matching = [item for item in _POOL if item.rental_id == account.rental_id]
        if account not in matching:
            matching.append(account)
        for item in matching:
            if status:
                item.rental_status = str(status)
            if can_request is not None:
                item.can_request_next_otp = bool(can_request)
            if next_requested is not None:
                item.next_requested = next_requested


def _rental_accounts(rental_id: str) -> list[BambooMmoAccount]:
    return [item for item in _POOL if item.rental_id == str(rental_id)]


def _fail_rental(rental_id: str, *, status: str = "CANCELLED") -> None:
    for item in _rental_accounts(rental_id):
        item.state = "failed"
        item.rental_status = status
    _PREFLIGHTED_RENTALS.discard(str(rental_id))


def _copy(account: BambooMmoAccount) -> BambooMmoAccount:
    return BambooMmoAccount(
        email=account.email,
        rental_id=account.rental_id,
        server=account.server,
        code_type_mail=account.code_type_mail,
        code_service=account.code_service,
        query_email=account.query_email or account.email,
        aliases=account.aliases or (account.email,),
        state=account.state,
        rental_status=account.rental_status,
        can_request_next_otp=account.can_request_next_otp,
        next_requested=account.next_requested,
        seen_codes=account.seen_codes,
    )


def _persist_state() -> None:
    from core.app_state_db import set_named_document

    set_named_document(
        _STATE_KEY,
        [
            {
                "email": account.email,
                "rental_id": account.rental_id,
                "server": account.server,
                "code_type_mail": account.code_type_mail,
                "code_service": account.code_service,
                "query_email": account.query_email or account.email,
                "aliases": list(account.aliases or (account.email,)),
                "state": account.state,
                "rental_status": account.rental_status,
                "can_request_next_otp": account.can_request_next_otp,
                "next_requested": account.next_requested,
                "seen_codes": sorted(account.seen_codes),
            }
            for account in _POOL
        ],
    )


def _ensure_state_loaded() -> None:
    global _STATE_LOADED
    if _STATE_LOADED:
        return
    from core.app_state_db import get_named_document

    raw = get_named_document(_STATE_KEY, default=[])
    if not isinstance(raw, list):
        raw = []
    seen_by_rental: dict[str, set[str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        rental_id = str(item.get("rental_id") or item.get("rentalId") or "").strip()
        if not email or not rental_id or not _is_gmail(email):
            continue
        aliases = item.get("aliases")
        alias_values = tuple(str(value or "").strip().lower() for value in aliases if str(value or "").strip()) if isinstance(aliases, list) else (email,)
        if email not in alias_values:
            alias_values = (email,) + alias_values
        state = str(item.get("state") or "available").strip().lower()
        if state == "reserved":
            state = "available"
        seen_codes = seen_by_rental.setdefault(rental_id, set())
        seen_codes.update(
            str(value).strip() for value in item.get("seen_codes", []) if str(value).strip()
        )
        try:
            server = int(item.get("server") or DEFAULT_SERVER)
        except (TypeError, ValueError):
            server = DEFAULT_SERVER
        account = BambooMmoAccount(
            email=email,
            rental_id=rental_id,
            server=server if server in (1, 2) else DEFAULT_SERVER,
            code_type_mail=str(item.get("code_type_mail") or DEFAULT_MAIL_TYPE).strip().upper(),
            code_service=str(item.get("code_service") or DEFAULT_SERVICE).strip().upper(),
            query_email=str(item.get("query_email") or email).strip().lower(),
            aliases=alias_values,
            state=state,
            rental_status=str(item.get("rental_status") or "WAITING_OTP"),
            can_request_next_otp=bool(item.get("can_request_next_otp")),
            next_requested=bool(item.get("next_requested")),
            seen_codes=seen_codes,
        )
        _POOL.append(account)
        _CONTEXT_CACHE[_cache_key(email)] = account
    _STATE_LOADED = True


def _find_context(email: str) -> BambooMmoAccount | None:
    account = _CONTEXT_CACHE.get(_cache_key(email))
    if account:
        return account
    try:
        from core import db

        row = db.get_account_by_email(email) or {}
        raw = row.get("extra_json")
        extra = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
        service = extra.get("email_service") if isinstance(extra, dict) else None
        if not isinstance(service, dict) or str(service.get("source") or "").strip().lower() != "bamboommo":
            return None
        rental_id = str(service.get("rental_id") or service.get("rentalId") or "").strip()
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    normalized = str(email or "").strip().lower()
    if not rental_id or not _is_gmail(normalized):
        return None
    account = BambooMmoAccount(
        email=normalized,
        rental_id=rental_id,
        server=int(service.get("server") or DEFAULT_SERVER),
        code_type_mail=str(service.get("code_type_mail") or DEFAULT_MAIL_TYPE).strip().upper(),
        code_service=str(service.get("code_service") or DEFAULT_SERVICE).strip().upper(),
        query_email=str(service.get("query_email") or normalized).strip().lower(),
        aliases=(normalized,),
        state="used",
        rental_status=str(service.get("rental_status") or "WAITING_OTP"),
        can_request_next_otp=bool(service.get("can_request_next_otp")),
    )
    _POOL.append(account)
    _CONTEXT_CACHE[_cache_key(normalized)] = account
    _persist_state()
    return account


def get_account_context(email: str) -> BambooMmoAccount | None:
    with _LOCK:
        _ensure_state_loaded()
        return _find_context(email)


def _rental_lock(rental_id: str) -> threading.RLock:
    with _LOCK:
        return _RENTAL_LOCKS.setdefault(str(rental_id), threading.RLock())


def registration_rental_lock(email: str) -> threading.RLock:
    account = get_account_context(email)
    if account is None:
        raise BambooMmoError(f"BambooMMO rental context not found: {email}")
    return _rental_lock(account.rental_id)


def _create_rental() -> list[BambooMmoAccount]:
    _, api_key, server, mail_type, service, _, _, _ = _config()
    payload = _request(
        "/api/mail/get-mail-apikey",
        body={"apiKey": api_key, "server": server, "codeTypeMail": mail_type, "codeService": service},
    )
    email = str(payload.get("responseData") or "").strip().lower()
    header = _header(payload)
    rental_id = str(header.get("rentalId") or "").strip()
    if not rental_id or not _is_gmail(email):
        raise BambooMmoError("BambooMMO không trả về Gmail hoặc rentalId hợp lệ")
    if _is_cancelled_rental_status(header.get("status")):
        raise BambooMmoError("BambooMMO rental đã bị hủy", resource_key="RENTAL_CANCELLED")
    try:
        aliases = tuple(generate_gmail_dual_domain_variants(email, 12))
    except (GmailAliasError, ValueError) as exc:
        raise BambooMmoError("BambooMMO không thể tạo 12 alias Gmail") from exc
    shared_codes: set[str] = set()
    return [
        BambooMmoAccount(
            email=alias,
            rental_id=rental_id,
            server=server,
            code_type_mail=mail_type,
            code_service=service,
            query_email=email,
            aliases=aliases,
            state="available",
            rental_status=str(header.get("status") or "WAITING_OTP"),
            can_request_next_otp=bool(header.get("canRequestNextOtp")),
            seen_codes=shared_codes,
        )
        for alias in aliases
    ]


def pick_account() -> BambooMmoAccount:
    with _LOCK:
        _ensure_state_loaded()
        for account in _POOL:
            if account.state != "available":
                continue
            if account.rental_id not in _PREFLIGHTED_RENTALS:
                try:
                    _get_code(account)
                    _PREFLIGHTED_RENTALS.add(account.rental_id)
                except BambooMmoError as exc:
                    if _is_cancelled_rental_error(exc):
                        _fail_rental(account.rental_id)
                        _persist_state()
                        logger.warning(
                            "[BambooMMO] bỏ qua rental hỏng %s (%s), cấp rental mới",
                            account.rental_id,
                            exc,
                        )
                        continue
                    raise
            account.state = "reserved"
            _persist_state()
            return _copy(account)

        last_cancelled: BambooMmoError | None = None
        for attempt in range(_MAX_RENTAL_RECOVERY_ATTEMPTS):
            try:
                accounts = _create_rental()
            except BambooMmoError as exc:
                if not _is_cancelled_rental_error(exc):
                    raise
                last_cancelled = exc
                logger.warning(
                    "[BambooMMO] rental mới bị hủy (%s), thử rental mới %s/%s",
                    exc,
                    attempt + 1,
                    _MAX_RENTAL_RECOVERY_ATTEMPTS,
                )
                continue
            _POOL.extend(accounts)
            for item in accounts:
                _CONTEXT_CACHE[_cache_key(item.email)] = item
            _PREFLIGHTED_RENTALS.add(accounts[0].rental_id)
            accounts[0].state = "reserved"
            _persist_state()
            return _copy(accounts[0])
        if last_cancelled is not None:
            raise BambooMmoError(
                f"BambooMMO liên tiếp trả rental đã hủy, đã thử {_MAX_RENTAL_RECOVERY_ATTEMPTS} rental mới",
                resource_key="RENTAL_CANCELLED",
            ) from last_cancelled
        raise BambooMmoError("BambooMMO không thể cấp rental mới")


def get_email() -> str:
    return pick_account().email


def _code_from_payload(payload: dict) -> str | None:
    value = payload.get("responseData")
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4,8}", text):
        return text
    return extract_otp({"text": text})


def _get_code(account: BambooMmoAccount) -> tuple[str | None, dict]:
    body: dict[str, object] = {
        "apiKey": _config()[1],
        "server": account.server,
        "rentalId": account.rental_id,
        "Mail": account.query_email or account.email,
    }
    try:
        payload = _request("/api/mail/get-code-apikey", body=body)
    except BambooMmoError as exc:
        if exc.resource_key == "NON_OTP":
            return None, {}
        raise
    header = _header(payload)
    _update_rental_state(account, header=header)
    if _is_cancelled_rental_status(header.get("status")):
        raise BambooMmoError("BambooMMO rental đã bị hủy", resource_key="RENTAL_CANCELLED")
    return _code_from_payload(payload), header


def request_next_otp(email: str) -> bool:
    account = get_account_context(email)
    if account is None:
        raise BambooMmoError(f"BambooMMO rental context not found: {email}")
    with _rental_lock(account.rental_id):
        if account.next_requested:
            return True
        if not account.can_request_next_otp:
            return False
        body: dict[str, object] = {"apiKey": _config()[1], "server": account.server}
        if account.server == 2:
            body["rentalId"] = account.rental_id
        else:
            body.update({
                "mail": account.query_email or account.email,
                "codeTypeMail": account.code_type_mail,
                "codeService": account.code_service,
            })
        try:
            payload = _request("/api/mail/get-mail-rent-again-apikey", body=body)
        except BambooMmoError as exc:
            if _is_cancelled_rental_error(exc):
                with _LOCK:
                    _fail_rental(account.rental_id)
                    _persist_state()
            raise
        resource_key = str(payload.get("resourceKey") or "").strip()
        if resource_key not in {"NEXT_OTP_REQUESTED", "SUCCESS"}:
            if _is_cancelled_rental_error(BambooMmoError(resource_key, resource_key=resource_key)):
                with _LOCK:
                    _fail_rental(account.rental_id)
                    _persist_state()
                raise BambooMmoError(
                    "BambooMMO rental đã bị hủy",
                    resource_key="RENTAL_CANCELLED",
                )
            raise BambooMmoError(f"BambooMMO không chấp nhận OTP tiếp theo: {resource_key or 'UNKNOWN'}")
        header = _header(payload)
        _update_rental_state(
            account,
            header=header,
            next_requested=True,
        )
        if _is_cancelled_rental_status(header.get("status")):
            with _LOCK:
                _fail_rental(account.rental_id)
                _persist_state()
            raise BambooMmoError("BambooMMO rental đã bị hủy", resource_key="RENTAL_CANCELLED")
        with _LOCK:
            _persist_state()
        return True


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    before_code: str | None = None,
) -> str:
    del after_ts, settle_seconds
    account = get_account_context(email)
    if account is None:
        raise BambooMmoError(f"BambooMMO rental context not found: {email}")
    _, _, _, _, _, _, configured_interval, configured_wait = _config()
    wait_seconds = max(0, int(max_wait if max_wait is not None else configured_wait))
    interval = max(1, int(poll_interval if poll_interval is not None else configured_interval))
    deadline = time.monotonic() + wait_seconds
    last_error = "NON_OTP"
    with _rental_lock(account.rental_id):
        if (before_code or account.seen_codes) and account.can_request_next_otp and not account.next_requested:
            request_next_otp(account.email)
        first_poll = True
        while first_poll or time.monotonic() <= deadline:
            first_poll = False
            try:
                code, _ = _get_code(account)
                if code and code != str(before_code or "").strip() and code not in account.seen_codes:
                    account.seen_codes.add(code)
                    _update_rental_state(account, next_requested=False)
                    with _LOCK:
                        _persist_state()
                    return code
            except BambooMmoError as exc:
                last_error = str(exc)
                if _is_cancelled_rental_error(exc):
                    with _LOCK:
                        _fail_rental(account.rental_id)
                        _persist_state()
                    raise
                if exc.resource_key not in {"NON_OTP"}:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    raise BambooMmoError(f"等待 BambooMMO OTP 超时: {email}; {last_error}")


def snapshot_verification_code(email: str) -> str | None:
    account = get_account_context(email)
    if account is None:
        return None
    with _rental_lock(account.rental_id):
        code, _ = _get_code(account)
        return code


def acknowledge_verification_code(email: str, otp: str) -> None:
    account = get_account_context(email)
    if account is None:
        return
    code = str(otp or "").strip()
    if code:
        account.seen_codes.add(code)
        with _LOCK:
            _persist_state()


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    with _LOCK:
        _ensure_state_loaded()
        account = _CONTEXT_CACHE.get(_cache_key(email))
        if account is None:
            return False
        normalized = str(status or "available").strip().lower()
        if note and _is_cancelled_rental_error(BambooMmoError(str(note))):
            _fail_rental(account.rental_id)
            _persist_state()
            return True
        account.state = (
            "used"
            if normalized == "used"
            else "failed"
            if normalized in {"failed", "disabled"}
            else "available"
        )
        _persist_state()
        return True


def mark_account_consumed(email: str) -> bool:
    return release_account(email, status="used")


def list_accounts(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        _ensure_state_loaded()
        wanted = str(status or "").strip().lower()
        rental_accounts: dict[str, list[BambooMmoAccount]] = {}
        for item in _POOL:
            rental_accounts.setdefault(item.rental_id, []).append(item)
        rows = []
        for account in _POOL:
            if wanted and account.state != wanted:
                continue
            aliases = rental_accounts.get(account.rental_id, [])
            rows.append(
                {
                    "email": account.email,
                    "source": "bamboommo",
                    "status": account.state,
                    "rental_id": account.rental_id,
                    "rental_status": account.rental_status,
                    "server": account.server,
                    "code_service": account.code_service,
                    "alias_total": len(account.aliases or aliases or (account.email,)),
                    "alias_available": sum(item.state == "available" for item in aliases),
                    "alias_reserved": sum(item.state == "reserved" for item in aliases),
                    "alias_used": sum(item.state == "used" for item in aliases),
                    "alias_failed": sum(item.state == "failed" for item in aliases),
                }
            )
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


def get_account_context_metadata(email: str) -> dict | None:
    account = get_account_context(email)
    if account is None:
        return None
    return {
        "source": "bamboommo",
        "email": account.email,
        "query_email": account.query_email or account.email,
        "rental_id": account.rental_id,
        "server": account.server,
        "code_type_mail": account.code_type_mail,
        "code_service": account.code_service,
        "rental_status": account.rental_status,
        "can_request_next_otp": account.can_request_next_otp,
    }


def reset_runtime_state(*, clear_persisted: bool = False) -> None:
    global _STATE_LOADED
    with _LOCK:
        _CONTEXT_CACHE.clear()
        _POOL.clear()
        _RENTAL_LOCKS.clear()
        _PREFLIGHTED_RENTALS.clear()
        _STATE_LOADED = False
        if clear_persisted:
            from core.app_state_db import set_named_document

            set_named_document(_STATE_KEY, [])
