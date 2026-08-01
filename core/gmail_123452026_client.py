# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from core.gmail_aliases import GmailAliasError, canonical_gmail


DEFAULT_API_BASE = "http://gmail.123452026.xyz/api"
_HEADERS = {"Accept": "*/*", "Content-Type": "application/json"}


class Gmail123452026Error(RuntimeError):
    """CDK Gmail 请求、响应或取码失败。"""


@dataclass(repr=False)
class Gmail123452026Account:
    email: str
    cdk: str
    remaining_uses: int
    job_id: str = ""
    expires_at: str | None = None
    seen_codes: set[str] = field(default_factory=set)
    inventory_id: str | None = None
    reservation_id: str | None = None
    owner_token: str | None = None

    def __repr__(self) -> str:
        return (
            f"Gmail123452026Account(email={self.email!r}, "
            f"remaining_uses={self.remaining_uses!r}, expires_at={self.expires_at!r})"
        )


def _endpoint(api_base: str, path: str, allow_insecure_http: bool) -> str:
    base = str(api_base or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise Gmail123452026Error("Địa chỉ API Gmail CDK không hợp lệ")
    return f"{base}/{path.lstrip('/')}"


def _post_json(session, url: str, payload: dict, timeout: int) -> dict:
    try:
        response = session.post(url, json=payload, headers=_HEADERS, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise Gmail123452026Error("Không thể gọi API Gmail CDK") from exc
    if not isinstance(data, dict):
        raise Gmail123452026Error("API Gmail CDK trả dữ liệu không hợp lệ")
    return data


def redeem_cdk(
    cdk: str,
    *,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
    allow_insecure_http: bool = False,
) -> Gmail123452026Account:
    value = str(cdk or "").strip()
    if not value:
        raise Gmail123452026Error("CDK không được để trống")
    url = _endpoint(api_base, "/mailbox/redeem", allow_insecure_http)
    data = _post_json(session or requests, url, {"cdk": value}, max(1, int(timeout)))
    status = str(data.get("status") or "").strip().lower()
    if status != "active":
        raise Gmail123452026Error("CDK không hoạt động hoặc đã hết hạn")
    try:
        email = canonical_gmail(str(data.get("emailAddress") or ""))
    except GmailAliasError as exc:
        raise Gmail123452026Error("API không trả địa chỉ Gmail hợp lệ") from exc
    raw_remaining = data.get("remainingUses", 6)
    try:
        remaining = max(0, min(6, int(raw_remaining)))
    except (TypeError, ValueError) as exc:
        raise Gmail123452026Error("remainingUses của API không hợp lệ") from exc
    return Gmail123452026Account(
        email=email,
        cdk=value,
        remaining_uses=remaining,
        expires_at=str(data.get("expiresAt") or "").strip() or None,
    )


def poll_verification_code(
    account: Gmail123452026Account,
    *,
    max_wait: int,
    poll_interval: int = 3,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
    allow_insecure_http: bool = False,
) -> str:
    url = _endpoint(api_base, "/mailbox/code", allow_insecure_http)
    deadline = time.monotonic() + max(0, int(max_wait))
    client = session or requests
    while time.monotonic() <= deadline:
        data = _post_json(client, url, {"cdk": account.cdk, "locktime": 5}, max(1, int(timeout)))
        status = str(data.get("status") or "").strip().lower()
        code = str(data.get("code") or "").strip()
        if status == "success" and code and code not in account.seen_codes:
            account.seen_codes.add(code)
            return code
        if status == "email_invalid":
            message = str(data.get("message") or "email invalid").strip()
            raise Gmail123452026Error(message or "email invalid")
        if status not in {"success", "processing"}:
            message = str(data.get("message") or "").strip()
            detail = f": {message}" if message else ""
            raise Gmail123452026Error(f"API lấy OTP trả trạng thái không hợp lệ: {status or 'unknown'}{detail}")
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0, int(poll_interval)))
    raise Gmail123452026Error(f"Chờ OTP Gmail CDK quá thời gian: {account.email}")


_CONTEXT_CACHE: dict[str, Gmail123452026Account] = {}
_SEEN_CODES_BY_CDK: dict[str, set[str]] = {}
_LEDGER: object | None = None
_INVENTORY_STORE: object | None = None


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _config_values() -> tuple[str, int, int, bool]:
    from config import email as _email_cfg

    api_base = str(getattr(_email_cfg, "GMAIL_123452026_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE).strip()
    timeout = max(1, int(getattr(_email_cfg, "GMAIL_123452026_REQUEST_TIMEOUT", 30) or 30))
    limit = max(1, min(6, int(getattr(_email_cfg, "GMAIL_123452026_ACCOUNTS_PER_CDK", 6) or 6)))
    allow_http = bool(getattr(_email_cfg, "GMAIL_123452026_ALLOW_INSECURE_HTTP", False))
    return api_base, timeout, limit, allow_http


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        from pathlib import Path

        from core.gmail_cdk_ledger import GmailCdkLedger

        _LEDGER = GmailCdkLedger(Path(__file__).resolve().parent.parent / "gmail_cdk_ledger.json")
        from core import db

        def account_exists(email: str) -> bool:
            return db.get_account_by_email(email) is not None

        def job_is_active(job_id: str) -> bool:
            try:
                job = db.get_job(int(job_id))
            except (TypeError, ValueError):
                return False
            return bool(job and job.get("status") in {"pending", "running", "stopping"})

        _LEDGER.reconcile(account_exists=account_exists, job_is_active=job_is_active)
    return _LEDGER


def pick_account(job_id: str, cdks: list[str]) -> Gmail123452026Account:
    from core.gmail_aliases import generate_gmail_variants
    from core.gmail_cdk_ledger import GmailCdkQuotaError

    owner = str(job_id or "").strip()
    api_base, timeout, limit, allow_http = _config_values()
    cdks = [str(cdk).strip() for cdk in (cdks or []) if str(cdk).strip()]
    if not cdks:
        raise Gmail123452026Error("Chưa nhập Gmail CDK cho batch đăng ký")
    last_error: Exception | None = None
    for cdk in cdks:
        try:
            redeemed = redeem_cdk(
                cdk,
                api_base=api_base,
                timeout=timeout,
                allow_insecure_http=allow_http,
            )
            variants = generate_gmail_variants(redeemed.email, limit=limit)
            slot = _ledger().reserve(
                cdk,
                variants,
                owner,
                remote_remaining=limit,
                configured_limit=limit,
            )
            from core.gmail_cdk_ledger import GmailCdkLedger
            seen_codes = _SEEN_CODES_BY_CDK.setdefault(GmailCdkLedger.cdk_key(cdk), set())
            account = Gmail123452026Account(
                email=slot.email,
                cdk=cdk,
                remaining_uses=redeemed.remaining_uses,
                expires_at=redeemed.expires_at,
                job_id=owner,
                seen_codes=seen_codes,
            )
            _CONTEXT_CACHE[_cache_key(account.email)] = account
            return account
        except (Gmail123452026Error, GmailCdkQuotaError) as exc:
            last_error = exc
    if last_error is not None:
        raise Gmail123452026Error(str(last_error)) from last_error
    raise Gmail123452026Error("Không còn Gmail CDK khả dụng")


def pick_account_by_inventory(
    job_id: str,
    inventory_ids: list[str],
    *,
    store_path: str | None = None,
) -> Gmail123452026Account:
    """Acquire a Gmail alias using a managed inventory ID instead of raw CDK.

    Resolves the raw CDK through CdkInventoryStore, redeems with the provider,
    records provider quota observation, reserves a local alias slot, and caches
    the full account context for OTP polling.
    """
    import uuid


    owner = str(job_id or "").strip()
    if not owner:
        raise Gmail123452026Error("Reservation cần job ID")
    ids = [str(inv_id or "").strip() for inv_id in (inventory_ids or []) if str(inv_id or "").strip()]
    if not ids:
        raise Gmail123452026Error("Chưa nhập Gmail CDK inventory ID cho batch đăng ký")
    api_base, timeout, limit, allow_http = _config_values()
    from core.gmail_aliases import generate_gmail_variants

    store = _inventory_store(store_path)
    last_error: Exception | None = None
    for inventory_id in ids:
        inventory = store.get_inventory(inventory_id)
        if not inventory or inventory.provider != "gmail":
            continue
        if inventory.state not in ("active",):
            continue
        raw_cdk = store.resolve_raw_cdk(inventory_id)
        operation_id = uuid.uuid4().hex
        owner_token = uuid.uuid4().hex
        try:
            redeemed = redeem_cdk(
                raw_cdk,
                api_base=api_base,
                timeout=timeout,
                allow_insecure_http=allow_http,
            )
            variants = generate_gmail_variants(redeemed.email, limit=limit)
            reservation = store.reserve_first_available_slot(
                inventory_id, variants, owner,
                operation_id=operation_id, owner_token=owner_token,
            )
            store.update_provider_quota(inventory_id, redeemed.remaining_uses)
            from core.gmail_cdk_ledger import GmailCdkLedger
            seen_codes = _SEEN_CODES_BY_CDK.setdefault(
                GmailCdkLedger.cdk_key(raw_cdk), set(),
            )
            account = Gmail123452026Account(
                email=reservation.email,
                cdk=raw_cdk,
                remaining_uses=redeemed.remaining_uses,
                expires_at=redeemed.expires_at,
                job_id=owner,
                seen_codes=seen_codes,
                inventory_id=inventory_id,
                reservation_id=reservation.reservation_id,
                owner_token=owner_token,
            )
            _CONTEXT_CACHE[_cache_key(account.email)] = account
            return account
        except (Gmail123452026Error, Exception) as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise Gmail123452026Error(str(last_error)) from last_error
    raise Gmail123452026Error("Không còn Gmail CDK inventory khả dụng")


def _inventory_store(store_path=None):
    global _INVENTORY_STORE
    if _INVENTORY_STORE is None:
        from pathlib import Path

        from core.cdk_inventory_store import CdkInventoryStore

        path = Path(store_path) if store_path else Path(__file__).resolve().parent.parent / "cdk_inventory.sqlite3"
        _INVENTORY_STORE = CdkInventoryStore(path)
    return _INVENTORY_STORE


def get_account_context(email: str) -> Gmail123452026Account | None:
    key = _cache_key(email)
    cached = _CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
    # Durable fallback: resolve from SQLite inventory reservation by email.
    try:
        store = _inventory_store()
        with closing(store._connect()) as connection:
            row = connection.execute(
                "SELECT s.*, i.raw_cdk, i.inventory_id FROM cdk_slots s "
                "JOIN cdk_inventory i ON i.inventory_id = s.inventory_id "
                "WHERE s.email = ? AND s.state = 'reserved' AND i.provider = 'gmail'",
                (key,),
            ).fetchone()
            if row:
                from core.gmail_cdk_ledger import GmailCdkLedger
                seen_codes = _SEEN_CODES_BY_CDK.setdefault(GmailCdkLedger.cdk_key(row["raw_cdk"]), set())
                account = Gmail123452026Account(
                    email=row["email"],
                    cdk=row["raw_cdk"],
                    remaining_uses=0,
                    job_id=row["job_id"],
                    seen_codes=seen_codes,
                    inventory_id=row["inventory_id"],
                    reservation_id=row["slot_id"],
                    owner_token=row["owner_token"],
                )
                _CONTEXT_CACHE[key] = account
                return account
    except Exception:
        pass
    return None


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    del after_ts, settle_seconds
    from config import email as _email_cfg

    account = get_account_context(email)
    if account is None:
        raise Gmail123452026Error("Không tìm thấy context Gmail CDK để lấy OTP")
    api_base, timeout, _, allow_http = _config_values()
    return poll_verification_code(
        account,
        max_wait=int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT),
        poll_interval=int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL),
        api_base=api_base,
        timeout=timeout,
        allow_insecure_http=allow_http,
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    del status, note
    account = _CONTEXT_CACHE.pop(_cache_key(email), None)
    if account is None:
        return False
    if account.reservation_id and account.inventory_id:
        import uuid
        return bool(
            _inventory_store().release_reservation(
                account.reservation_id,
                operation_id=uuid.uuid4().hex,
                owner_token=account.owner_token or "",
            )
        )
    return bool(_ledger().release(account.email, account.job_id))


def mark_account_consumed(email: str) -> bool:
    account = get_account_context(email)
    if account is None:
        return False
    if account.reservation_id and account.inventory_id:
        import uuid
        changed = bool(
            _inventory_store().consume_reservation(
                account.reservation_id,
                operation_id=uuid.uuid4().hex,
                owner_token=account.owner_token or "",
            )
        )
    else:
        changed = bool(_ledger().consume(account.email, account.job_id))
    if changed:
        _CONTEXT_CACHE.pop(_cache_key(email), None)
    return changed
