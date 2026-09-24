"""Client for the Automated Email API Gmail mailbox provider.

The service allocates one Gmail mailbox per API order.  Gmail's dot/plus and
``googlemail.com`` spellings are local registration aliases that all resolve
back to the address returned by the service when reading mail.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from core.gmail_aliases import GmailAliasError, generate_gmail_dual_domain_variants
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = ""
DEFAULT_REQUEST_TIMEOUT = 20
ALIASES_PER_MAILBOX = 12
_RETRYABLE_STATUS_CODES = frozenset({404, 429, 500, 502, 503})


class AutomatedEmailApiError(RuntimeError):
    """Automated Email API request or mailbox error."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class AutomatedEmailMessage:
    """Normalized latest-mail response."""

    sender: str = ""
    recipient: str = ""
    subject: str = ""
    text: str = ""
    code: str | None = None


@dataclass
class AutomatedEmailAccount:
    """A claimed alias and its API mailbox identity."""

    email: str
    query_email: str
    aliases: tuple[str, ...] = ()
    share_token: str | None = None
    state: str = "available"
    seen_codes: set[str] = field(default_factory=set)


_LOCK = threading.RLock()
_CONTEXT_CACHE: dict[str, AutomatedEmailAccount] = {}
_POOL: list[AutomatedEmailAccount] = []
_MAILBOX_LOCKS: dict[str, threading.RLock] = {}
_PREFLIGHTED_MAILBOXES: set[str] = set()
_STATE_KEY = "automated_email_api.mailboxes.v1"
_STATE_LOADED = False


def _cache_key(email: str) -> str:
    return str(email or "").strip().casefold()


def _config() -> tuple[str, str, int, int]:
    from config import email as email_config

    base_url = str(getattr(email_config, "EMAIL_API_BASE_URL", DEFAULT_API_BASE_URL) or "").strip()
    api_key = str(getattr(email_config, "EMAIL_API_KEY", "") or "").strip()
    timeout = max(1, int(getattr(email_config, "EMAIL_API_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT) or DEFAULT_REQUEST_TIMEOUT))
    interval = max(1, int(getattr(email_config, "EMAIL_API_POLL_INTERVAL", 3) or 3))
    return base_url, api_key, timeout, interval


def _ensure_state_loaded() -> None:
    """Restore mailbox aliases from the canonical runtime state database."""
    global _STATE_LOADED
    if _STATE_LOADED:
        return
    from core.app_state_db import get_named_document

    raw = get_named_document(_STATE_KEY, default=[])
    if not isinstance(raw, list):
        raw = []
    for mailbox in raw:
        if not isinstance(mailbox, dict):
            continue
        query_email = str(mailbox.get("query_email") or "").strip().lower()
        aliases = mailbox.get("aliases")
        if not query_email or not isinstance(aliases, list):
            continue
        share_token = str(mailbox.get("share_token") or "").strip() or None
        shared_codes: set[str] = set()
        normalized_aliases = tuple(str(alias or "").strip().lower() for alias in aliases if str(alias or "").strip())
        for item in mailbox.get("seen_codes") or []:
            code = str(item or "").strip()
            if code:
                shared_codes.add(code)
        states = mailbox.get("states") if isinstance(mailbox.get("states"), dict) else {}
        for alias in normalized_aliases:
            state = str(states.get(alias) or "available")
            if state == "reserved":
                state = "available"
            account = AutomatedEmailAccount(
                email=alias,
                query_email=query_email,
                aliases=normalized_aliases,
                share_token=share_token,
                state=state,
                seen_codes=shared_codes,
            )
            _POOL.append(account)
            _CONTEXT_CACHE[_cache_key(alias)] = account
    _STATE_LOADED = True


def _persist_state() -> None:
    from core.app_state_db import set_named_document

    mailboxes: dict[str, dict] = {}
    for account in _POOL:
        key = _cache_key(account.query_email)
        mailbox = mailboxes.setdefault(
            key,
            {
                "query_email": account.query_email,
                "aliases": list(account.aliases),
                "share_token": account.share_token,
                "seen_codes": sorted(account.seen_codes),
                "states": {},
            },
        )
        mailbox["states"][_cache_key(account.email)] = account.state
    set_named_document(_STATE_KEY, list(mailboxes.values()))


def _endpoint(base_url: str, path: str) -> str:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutomatedEmailApiError(
            "Automated Email API 地址无效，请配置 EMAIL_API_BASE_URL"
        )
    return f"{str(base_url).strip().rstrip('/')}/{path.lstrip('/')}"


def _parse_json(response: requests.Response, path: str) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise AutomatedEmailApiError(
            f"Automated Email API 响应不是 JSON: {path} (HTTP {response.status_code})",
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
        ) from exc
    if not isinstance(payload, dict):
        raise AutomatedEmailApiError(
            f"Automated Email API 响应格式无效: {path}",
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
        )
    return payload


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
) -> dict:
    base_url, api_key, timeout, _ = _config()
    if not api_key:
        raise AutomatedEmailApiError(
            "Automated Email API Key 未配置，请填写 EMAIL_API_KEY"
        )
    query = dict(params or {})
    query["apikey"] = api_key
    url = _endpoint(base_url, path)
    try:
        response = requests.request(
            method,
            url,
            params=query,
            json=json_body,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AutomatedEmailApiError(
            f"Automated Email API 请求失败: {path}", retryable=True
        ) from exc

    payload = _parse_json(response, path)
    raw_code = payload.get("code")
    try:
        api_code = int(raw_code) if raw_code is not None else response.status_code
    except (TypeError, ValueError):
        api_code = response.status_code
    if response.status_code != 200 or api_code != 0:
        status_code = response.status_code if response.status_code != 200 else api_code
        message = str(payload.get("message") or "请求失败").strip()
        raise AutomatedEmailApiError(
            f"Automated Email API 请求失败: HTTP {status_code}; {message}",
            status_code=status_code,
            retryable=status_code in _RETRYABLE_STATUS_CODES,
        )
    return payload


def _email_from_payload(payload: dict) -> tuple[str, str | None]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AutomatedEmailApiError("Automated Email API 响应缺少 data")
    email = str(data.get("email") or "").strip().lower()
    if email.count("@") != 1 or email.rsplit("@", 1)[1] not in {"gmail.com", "googlemail.com"}:
        raise AutomatedEmailApiError("Automated Email API 未返回有效 Gmail 地址")
    return email, str(data.get("share_token") or "").strip() or None


def _new_mailbox() -> list[AutomatedEmailAccount]:
    return _new_mailbox_with_options(share=False)


def create_email(email_type: str = "gmail", *, share: bool = False) -> AutomatedEmailAccount:
    """Create one service mailbox and return its first claimed alias.

    Registration always uses ``gmail``. Other API types are intentionally not
    exposed through the provider dispatcher because their domains do not meet
    this integration's Gmail-only contract.
    """
    if str(email_type or "").strip().lower() != "gmail":
        raise AutomatedEmailApiError("Automated Email API provider 仅支持 type=gmail")
    with _LOCK:
        _ensure_state_loaded()
        accounts = _new_mailbox_with_options(share=share)
        _POOL.extend(accounts)
        account = accounts[0]
        account.state = "reserved"
        _persist_state()
        try:
            _preflight_mailbox_once(account)
        except AutomatedEmailApiError as exc:
            release_account(
                account.email,
                status="failed" if exc.status_code in {410, 411} else "available",
                note=str(exc),
            )
            raise
        return _claimed_account(account)


def _new_mailbox_with_options(*, share: bool = False) -> list[AutomatedEmailAccount]:
    params: dict[str, object] = {"type": "gmail"}
    if share:
        params["share"] = 1
    _, _, _, interval = _config()
    payload = None
    for attempt in range(3):
        try:
            payload = _request("GET", "/api/user/email", params=params)
            break
        except AutomatedEmailApiError as exc:
            if not exc.retryable or attempt == 2:
                raise
            logger.warning(
                "[AutomatedEmailAPI] Gmail mailbox creation retry %s/3 for HTTP %s",
                attempt + 1,
                exc.status_code or "request",
            )
            time.sleep(interval)
    if payload is None:
        raise AutomatedEmailApiError("Automated Email API 未返回 Gmail mailbox")
    email, share_token = _email_from_payload(payload)
    try:
        aliases = tuple(generate_gmail_dual_domain_variants(email, ALIASES_PER_MAILBOX))
    except (GmailAliasError, ValueError) as exc:
        raise AutomatedEmailApiError("Automated Email API Gmail 地址无法生成 alias") from exc
    # The generator already includes the canonical address and deliberately
    # balances both Gmail spellings. Keep that order even when the service
    # returns a googlemail.com address so every mailbox remains 6 + 6 aliases.
    ordered = aliases[:ALIASES_PER_MAILBOX]
    shared_codes: set[str] = set()
    accounts = [
        AutomatedEmailAccount(
            email=alias,
            query_email=email,
            aliases=ordered,
            share_token=share_token,
            seen_codes=shared_codes,
        )
        for alias in ordered
    ]
    for account in accounts:
        _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[AutomatedEmailAPI] 已创建 Gmail mailbox，alias_count=%s", len(ordered))
    return accounts


def pick_account() -> AutomatedEmailAccount:
    """Claim the next alias, creating one Gmail mailbox when needed."""
    with _LOCK:
        _ensure_state_loaded()
        while True:
            for account in _POOL:
                if account.state == "available":
                    account.state = "reserved"
                    _persist_state()
                    try:
                        _preflight_mailbox_once(account)
                    except AutomatedEmailApiError as exc:
                        if exc.status_code in {410, 411}:
                            release_account(account.email, status="failed", note=str(exc))
                            continue
                        release_account(account.email, status="available")
                        raise
                    return _claimed_account(account)
            accounts = _new_mailbox()
            _POOL.extend(accounts)
            _persist_state()


def get_account_context(email: str) -> AutomatedEmailAccount | None:
    with _LOCK:
        _ensure_state_loaded()
        return _CONTEXT_CACHE.get(_cache_key(email))


def _claimed_account(account: AutomatedEmailAccount) -> AutomatedEmailAccount:
    """Return a caller-owned view while retaining shared mailbox state locally."""
    return AutomatedEmailAccount(
        email=account.email,
        query_email=account.query_email,
        aliases=account.aliases,
        share_token=account.share_token,
        state=account.state,
        seen_codes=account.seen_codes,
    )


def _mailbox_lock(query_email: str) -> threading.RLock:
    with _LOCK:
        return _MAILBOX_LOCKS.setdefault(_cache_key(query_email), threading.RLock())


def registration_mailbox_lock(email: str) -> threading.RLock:
    """Return the shared reentrant lock for an alias's source mailbox.

    The API exposes only the latest mailbox message, so aliases of the same
    mailbox must complete a registration one at a time to keep OTP ownership
    unambiguous. The lock is reentrant because OTP polling runs inside the
    registration operation that already owns it.
    """
    account = get_account_context(email)
    query_email = account.query_email if account else str(email or "").strip()
    if not query_email:
        raise AutomatedEmailApiError("Automated Email API 注册锁缺少邮箱地址")
    return _mailbox_lock(query_email)


def _message_from_data(data: dict, query_email: str) -> AutomatedEmailMessage:
    raw_code = data.get("code")
    code = str(raw_code).strip() if raw_code is not None else None
    if code and len(code) != 6:
        code = extract_otp({"text": code})
    text = str(data.get("text") or data.get("content") or "")
    if not code:
        code = extract_otp({"text": text, "subject": data.get("subject") or ""})
    return AutomatedEmailMessage(
        sender=str(data.get("from") or data.get("sender") or "").strip(),
        recipient=str(data.get("to") or query_email).strip(),
        subject=str(data.get("subject") or "").strip(),
        text=text,
        code=code,
    )


def get_latest_mail(email: str) -> AutomatedEmailMessage | None:
    """Return the latest message, or ``None`` when the mailbox is still empty."""
    account = get_account_context(email)
    query_email = account.query_email if account else str(email or "").strip()
    if not query_email:
        raise AutomatedEmailApiError("Automated Email API 取码缺少邮箱地址")
    payload = _request("GET", "/api/user/mail", params={"email": query_email})
    data = payload.get("data")
    return _message_from_data(data, query_email) if isinstance(data, dict) else None


def _prepare_account_for_use(account: AutomatedEmailAccount) -> None:
    """Touch the mailbox before registration and record a stale OTP baseline."""
    _, _, _, interval = _config()
    for attempt in range(3):
        try:
            message = get_latest_mail(account.email)
            if message and message.code:
                account.seen_codes.add(message.code)
                _persist_state()
            return
        except AutomatedEmailApiError as exc:
            if not exc.retryable or attempt == 2:
                raise
            logger.warning(
                "[AutomatedEmailAPI] mailbox preflight retry %s/3 for HTTP %s",
                attempt + 1,
                exc.status_code or "request",
            )
            time.sleep(interval)


def _preflight_mailbox_once(account: AutomatedEmailAccount) -> None:
    """Record one stale-code baseline without racing aliases of the same mailbox."""
    mailbox_key = _cache_key(account.query_email)
    if mailbox_key in _PREFLIGHTED_MAILBOXES:
        return
    _prepare_account_for_use(account)
    _PREFLIGHTED_MAILBOXES.add(mailbox_key)


def activate_email(email: str) -> bool:
    """Fire-and-forget mailbox activation; the API returns no mail content."""
    account = get_account_context(email)
    target = account.query_email if account else str(email or "").strip()
    if not target:
        raise AutomatedEmailApiError("Automated Email API 激活缺少邮箱地址")
    base_url, api_key, timeout, _ = _config()
    if not api_key:
        raise AutomatedEmailApiError("Automated Email API Key 未配置，请填写 EMAIL_API_KEY")
    try:
        response = requests.post(
            _endpoint(base_url, "/api/user/activate"),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            json={"email": target},
            timeout=timeout,
        )
        payload = _parse_json(response, "/api/user/activate")
    except requests.RequestException as exc:
        logger.debug("[AutomatedEmailAPI] 激活请求失败: %s", type(exc).__name__)
        return False
    if response.status_code != 200 or payload.get("code") not in (0, "0"):
        logger.debug("[AutomatedEmailAPI] 激活未成功: HTTP %s", response.status_code)
        return False
    return True


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """Poll latest mail and return its six-digit verification code."""
    del after_ts  # The API exposes only the latest message; stale-code checks are local.
    from config import email as email_config

    wait_seconds = max(0, int(max_wait if max_wait is not None else getattr(email_config, "OTP_MAX_WAIT", 60) or 60))
    _, _, _, configured_interval = _config()
    interval = max(1, int(poll_interval if poll_interval is not None else configured_interval))
    settle = max(0, int(settle_seconds if settle_seconds is not None else getattr(email_config, "OTP_SETTLE_SECONDS", 5) or 5))
    deadline = time.monotonic() + wait_seconds
    last_error = "收件箱为空或尚未出现验证码"
    best_code: str | None = None
    settle_until: float | None = None

    context = get_account_context(email)
    query_email = context.query_email if context else str(email or "").strip()
    with _mailbox_lock(query_email):
        while time.monotonic() <= deadline:
            try:
                message = get_latest_mail(email)
                if message and message.code:
                    context = get_account_context(email)
                    seen_codes = context.seen_codes if context else set()
                    if message.code not in seen_codes:
                        best_code = message.code
                        if context:
                            context.seen_codes.add(message.code)
                            with _LOCK:
                                _persist_state()
                        settle_until = time.monotonic() + settle
                if best_code and settle_until is not None and time.monotonic() >= settle_until:
                    return best_code
            except AutomatedEmailApiError as exc:
                if exc.status_code in {410, 411}:
                    release_account(email, status="failed", note=str(exc))
                if not exc.retryable:
                    raise
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    if best_code:
        return best_code
    raise AutomatedEmailApiError(f"等待 Automated Email API 验证码超时: {email}; {last_error}")


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    """Release one alias reservation without logging API credentials."""
    with _LOCK:
        _ensure_state_loaded()
        account = _CONTEXT_CACHE.get(_cache_key(email))
        if account is None:
            return False
        normalized = str(status or "available").strip().lower()
        if normalized in {"failed", "disabled"} or any(code in str(note or "") for code in ("410", "411")):
            for item in _POOL:
                if _cache_key(item.query_email) == _cache_key(account.query_email):
                    item.state = "failed"
        else:
            account.state = "used" if normalized == "used" else "available"
        _persist_state()
        return True


def list_accounts(status: str | None = None, limit: int = 500) -> list[dict]:
    """Return the local alias ledger for WebUI diagnostics and status actions."""
    with _LOCK:
        _ensure_state_loaded()
        rows = []
        for account in _POOL:
            if status and account.state != str(status).strip().lower():
                continue
            rows.append(
                {
                    "email": account.email,
                    "source": "automated_email_api",
                    "status": account.state,
                    "query_email": account.query_email,
                    "alias_total": len(account.aliases),
                }
            )
        return rows[: max(1, int(limit or 1))]


def pool_summary() -> dict[str, int]:
    """Return local alias/mailbox counts without contacting the API."""
    with _LOCK:
        _ensure_state_loaded()
        rows = list(_POOL)
        available_roots = {
            _cache_key(account.query_email)
            for account in rows
            if account.state == "available"
        }
        available = sum(account.state == "available" for account in rows)
        return {
            "total": len(rows),
            "available": available,
            "used": sum(account.state == "used" for account in rows),
            "failed": sum(account.state == "failed" for account in rows),
            "alias_total": len(rows),
            "alias_available": available,
            "alias_source_available": len(available_roots),
        }


def delete_account(email: str) -> bool:
    """Delete one local alias reservation from the runtime ledger."""
    with _LOCK:
        _ensure_state_loaded()
        account = _CONTEXT_CACHE.get(_cache_key(email))
        if account is None:
            return False
        target_key = _cache_key(account.email)
        _CONTEXT_CACHE.pop(target_key, None)
        remaining = []
        for item in _POOL:
            if item is account:
                continue
            if _cache_key(item.query_email) == _cache_key(account.query_email):
                item.aliases = tuple(alias for alias in item.aliases if _cache_key(alias) != target_key)
            remaining.append(item)
        _POOL[:] = remaining
        _persist_state()
        return True


def mark_account_consumed(email: str) -> bool:
    return release_account(email, status="used")


def get_share_url(share_token: str) -> str:
    base_url, _, _, _ = _config()
    return f"{_endpoint(base_url, '/api/share')}/{str(share_token or '').strip()}"


def get_quota() -> dict:
    payload = _request("GET", "/api/user/quota")
    data = payload.get("data")
    return dict(data) if isinstance(data, dict) else {}


def reset_runtime_state(*, clear_persisted: bool = False) -> None:
    """Clear in-process state; optionally clear the local runtime ledger."""
    global _STATE_LOADED
    with _LOCK:
        _CONTEXT_CACHE.clear()
        _POOL.clear()
        _MAILBOX_LOCKS.clear()
        _PREFLIGHTED_MAILBOXES.clear()
        _STATE_LOADED = False
        if clear_persisted:
            from core.app_state_db import set_named_document

            set_named_document(_STATE_KEY, [])
