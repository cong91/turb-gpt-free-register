# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests


DEFAULT_API_BASE = "https://sms.paymesh.cn"
_HEADERS_JSON = {"Accept": "application/json", "Content-Type": "application/json"}
_HEADERS_ACCEPT = {"Accept": "application/json"}
MAX_ACCOUNTS_PER_CDK = 6


class PaymeshMailError(RuntimeError):
    """Paymesh MAIL card 请求、响应或取码失败。"""


@dataclass(repr=False)
class PaymeshMailAccount:
    email: str
    cdk: str
    remaining_uses: int
    job_id: str = ""
    expires_at: str | None = None
    status: str = "active"
    seen_codes: set[str] = field(default_factory=set)
    inventory_id: str | None = None
    reservation_id: str | None = None
    owner_token: str | None = None

    def __repr__(self) -> str:
        return (
            f"PaymeshMailAccount(email={self.email!r}, remaining_uses={self.remaining_uses!r}, "
            f"expires_at={self.expires_at!r}, status={self.status!r})"
        )


def _endpoint(api_base: str, path: str) -> str:
    base = str(api_base or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymeshMailError("Địa chỉ Paymesh API không hợp lệ")
    return f"{base}/{path.lstrip('/')}"


def _request_json(session, method: str, url: str, *, timeout: int, json_payload: dict | None = None) -> dict:
    try:
        if method == "POST":
            response = session.post(url, json=json_payload or {}, headers=_HEADERS_JSON, timeout=timeout)
        else:
            response = session.get(url, headers=_HEADERS_ACCEPT, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise PaymeshMailError(f"Không thể gọi API Paymesh ({method})") from exc
    if not isinstance(data, dict):
        raise PaymeshMailError("API Paymesh trả dữ liệu không hợp lệ")
    return data


def _biz_code(data: dict) -> int:
    try:
        return int(data.get("code", -1))
    except (TypeError, ValueError):
        return -1


def _biz_msg(data: dict) -> str:
    return str(data.get("msg") or data.get("message") or "unknown error").strip()


def _mapped_error(code: int, msg: str) -> PaymeshMailError:
    if code == 2001:
        return PaymeshMailError("MAIL card không tồn tại")
    if code == 2002:
        return PaymeshMailError("MAIL card đã hết quota")
    if code == 2003:
        return PaymeshMailError("MAIL card đã hết hạn")
    if code == 2005:
        return PaymeshMailError("MAIL card không ở trạng thái hợp lệ")
    return PaymeshMailError(f"API Paymesh trả lỗi nghiệp vụ: code={code}")


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _extract_email_info(data: dict) -> tuple[str, str | None, str]:
    payload = _as_dict(data.get("data"))
    email_obj = _as_dict(payload.get("email"))
    session = _as_dict(email_obj.get("session")) or _as_dict(payload.get("session")) or payload
    email = str(
        session.get("emailAddress")
        or session.get("email")
        or payload.get("emailAddress")
        or payload.get("email")
        or ""
    ).strip()
    if not email or "@" not in email:
        raise PaymeshMailError("API Paymesh không trả địa chỉ email hợp lệ")
    expires_at = str(session.get("expiresAt") or "").strip() or None
    status = str(session.get("status") or "active").strip() or "active"
    return email, expires_at, status


def _extract_email_info_if_present(data: dict) -> tuple[str, str | None, str] | None:
    try:
        return _extract_email_info(data)
    except PaymeshMailError:
        return None


def _extract_codes(data: dict) -> list[tuple[str, str | None, int | None]]:
    payload = _as_dict(data.get("data"))
    email_obj = _as_dict(payload.get("email"))
    arrays = [email_obj.get("codes"), payload.get("codes"), data.get("codes")]
    out: list[tuple[str, str | None, int | None]] = []
    for array in arrays:
        if not isinstance(array, list):
            continue
        for item in array:
            if isinstance(item, dict):
                code = str(item.get("code") or "").strip()
                received_at = str(item.get("receivedAt") or "").strip() or None
                raw_id = item.get("id")
                try:
                    message_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    message_id = None
            else:
                code = str(item or "").strip()
                received_at = None
                message_id = None
            if code:
                out.append((code, received_at, message_id))
    return out


def _code_identity(code: tuple[str, str | None, int | None]) -> str:
    value, received_at, message_id = code
    if message_id is not None:
        return f"id:{message_id}:{value}"
    if received_at:
        return f"received:{received_at}:{value}"
    return f"code:{value}"


def _remember_codes(seen_codes: set[str], codes: list[tuple[str, str | None, int | None]]) -> None:
    seen_codes.update(_code_identity(code) for code in codes)


def _code_candidates(
    seen_codes: set[str],
    codes: list[tuple[str, str | None, int | None]],
    after_ts: float | None = None,
) -> list[tuple[str, str | None, int | None]]:
    candidates = []
    for code in codes:
        received_at = _received_at_timestamp(code[1])
        if after_ts is not None and received_at is not None and received_at <= after_ts:
            continue
        if _code_identity(code) in seen_codes:
            continue
        candidates.append(code)
    return sorted(
        candidates,
        key=lambda item: (item[1] or "", item[2] if item[2] is not None else -1),
        reverse=True,
    )


def _received_at_timestamp(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        return numeric / 1000 if numeric > 10_000_000_000 else numeric
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _alias_variants(email: str, limit: int) -> list[str]:
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        raise PaymeshMailError("Địa chỉ Paymesh email không hợp lệ")
    local, domain = value.split("@", 1)
    if not local or not domain:
        raise PaymeshMailError("Địa chỉ Paymesh email không hợp lệ")
    count = max(0, min(MAX_ACCOUNTS_PER_CDK, int(limit)))
    if count == 0:
        return []
    from core.paymesh_aliases import alias_suffix

    return [f"{local}+{alias_suffix(value, index)}@{domain}" for index in range(count)]


def _account_from_payload(cdk: str, data: dict) -> PaymeshMailAccount:
    email, expires_at, status = _extract_email_info(data)
    account = PaymeshMailAccount(
        email=email.lower(),
        cdk=cdk,
        remaining_uses=MAX_ACCOUNTS_PER_CDK,
        expires_at=expires_at,
        status=status,
    )
    codes = _extract_codes(data)
    _remember_codes(account.seen_codes, codes)
    _remember_durable_codes(cdk, codes)
    return account


def redeem_cdk(
    cdk: str,
    *,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
) -> PaymeshMailAccount:
    value = str(cdk or "").strip()
    if not value:
        raise PaymeshMailError("MAIL card không được để trống")
    client = session or requests
    url = _endpoint(api_base, "/api/v1/redeem")
    data = _request_json(client, "POST", url, json_payload={"code": value}, timeout=max(1, int(timeout)))
    code = _biz_code(data)
    looked_up = False
    if code in {2002, 2004}:
        data = lookup_order(value, session=client, api_base=api_base, timeout=timeout)
        looked_up = True
    elif code != 0:
        raise _mapped_error(code, _biz_msg(data))

    parsed = _extract_email_info_if_present(data)
    codes = _extract_codes(data)
    if not looked_up and (parsed is None or not codes):
        data = lookup_order(value, session=client, api_base=api_base, timeout=timeout)
        looked_up = True
        parsed = _extract_email_info(data)
        codes = _extract_codes(data)
    elif parsed is None:
        parsed = _extract_email_info(data)
    email, expires_at, status = parsed
    account = PaymeshMailAccount(
        email=email.lower(),
        cdk=value,
        remaining_uses=MAX_ACCOUNTS_PER_CDK,
        expires_at=expires_at,
        status=status,
    )
    _remember_codes(account.seen_codes, codes)
    _remember_durable_codes(value, codes)
    return account


def lookup_order(
    cdk: str,
    *,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
) -> dict:
    value = str(cdk or "").strip()
    if not value:
        raise PaymeshMailError("MAIL card không được để trống")
    url = _endpoint(api_base, f"/api/v1/order/lookup?code={quote(value, safe='')}&poll=true")
    data = _request_json(session or requests, "GET", url, timeout=max(1, int(timeout)))
    code = _biz_code(data)
    if code == 2002 and _extract_email_info_if_present(data) is not None:
        return data
    if code != 0:
        raise _mapped_error(code, _biz_msg(data))
    return data


def poll_verification_code(
    account: PaymeshMailAccount,
    *,
    max_wait: int,
    poll_interval: int = 3,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
    after_ts: float | None = None,
) -> str:
    deadline = time.monotonic() + max(0, int(max_wait))
    client = session or requests
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        try:
            data = lookup_order(account.cdk, session=client, api_base=api_base, timeout=timeout)
            parsed = _extract_email_info_if_present(data)
            if parsed is not None:
                _, account.expires_at, account.status = parsed
            codes = _extract_codes(data)
            candidates = _code_candidates(account.seen_codes, codes, after_ts=after_ts)
            if candidates:
                candidate = candidates[0]
                if _claim_durable_code(account.cdk, candidate, codes):
                    _remember_codes(account.seen_codes, codes)
                    return candidate[0]
                _remember_codes(account.seen_codes, codes)
            elif codes:
                _remember_codes(account.seen_codes, codes)
                _remember_durable_codes(account.cdk, codes)
        except PaymeshMailError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0, int(poll_interval)))
    if last_error is not None:
        raise PaymeshMailError(f"Chờ OTP Paymesh quá thời gian: {account.email}; last={last_error}") from last_error
    raise PaymeshMailError(f"Chờ OTP Paymesh quá thời gian: {account.email}")


_CONTEXT_CACHE: dict[str, PaymeshMailAccount] = {}
_SEEN_CODES_BY_CDK: dict[str, set[str]] = {}
_LEDGER: object | None = None
_INVENTORY_STORE: object | None = None
_OTP_STORE: object | None = None
_CDK_LOCKS: dict[str, threading.Lock] = {}
_CDK_LOCKS_GUARD = threading.Lock()
_CDK_LOCK_WAIT = 600


def _otp_store():
    global _OTP_STORE
    if _OTP_STORE is None:
        from pathlib import Path

        from core.otp_identity_store import OtpIdentityStore

        _OTP_STORE = OtpIdentityStore(
            Path(__file__).resolve().parent.parent / "otp_identity.sqlite3"
        )
    return _OTP_STORE


def _cdk_fingerprint(cdk: str) -> str:
    return _otp_store().fingerprint("paymesh", cdk)


def _remember_durable_codes(cdk: str, codes: list[tuple[str, str | None, int | None]]) -> None:
    if not codes:
        return
    identities: list[str] = []
    for code in codes:
        identities.extend(_identity_claims(code))
    _otp_store().remember_many("paymesh", _cdk_fingerprint(cdk), identities)


def _identity_claims(code: tuple[str, str | None, int | None]) -> list[str]:
    identities = [_code_identity(code)]
    if code[0]:
        identities.append(f"value:{code[0]}")
    return identities


def _claim_durable_code(
    cdk: str,
    code: tuple[str, str | None, int | None],
    observed: list[tuple[str, str | None, int | None]],
) -> bool:
    observed_identities: list[str] = []
    for item in observed:
        observed_identities.extend(_identity_claims(item))
    return bool(
        _otp_store().claim_with_snapshot(
            "paymesh",
            _cdk_fingerprint(cdk),
            claim_identities=_identity_claims(code),
            observed_identities=observed_identities,
        )
    )


def _cdk_lock_key(cdk: str) -> str:
    return str(cdk or "").strip().upper()


def _get_cdk_lock(cdk: str) -> threading.Lock:
    key = _cdk_lock_key(cdk)
    with _CDK_LOCKS_GUARD:
        if key not in _CDK_LOCKS:
            _CDK_LOCKS[key] = threading.Lock()
        return _CDK_LOCKS[key]


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _historical_emails() -> set[str]:
    try:
        from core import db

        rows = list(db.list_accounts(limit=100_000, archived="all")) + list(db.list_jobs(limit=100_000))
    except Exception:
        return set()
    return {
        _cache_key(str(row.get("email") or ""))
        for row in rows
        if isinstance(row, dict) and str(row.get("email") or "").strip()
    }


def _unused_alias_variants(
    email: str,
    limit: int,
    routed_domains: Sequence = (),
) -> list[str]:
    from core.paymesh_aliases import build_paymesh_alias_plan

    used = _historical_emails()
    try:
        plan = build_paymesh_alias_plan(email, limit=limit, routed_domains=routed_domains)
    except PaymeshMailError:
        # Fallback về original-only khi routed domain không hợp lệ tại runtime.
        plan = build_paymesh_alias_plan(email, limit=limit, routed_domains=())
    return [c.email for c in plan.candidates if _cache_key(c.email) not in used]


def _config_values() -> tuple[str, int, int]:
    from config import email as _email_cfg

    api_base = str(getattr(_email_cfg, "PAYMESH_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE).strip()
    timeout = max(1, int(getattr(_email_cfg, "PAYMESH_REQUEST_TIMEOUT", 30) or 30))
    limit = max(1, min(MAX_ACCOUNTS_PER_CDK, int(getattr(_email_cfg, "PAYMESH_ACCOUNTS_PER_CDK", 6) or 6)))
    return api_base, timeout, limit


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        from pathlib import Path

        from core.provider_card_ledger import ProviderCardLedger

        _LEDGER = ProviderCardLedger(
            Path(__file__).resolve().parent.parent / "paymesh_card_ledger.json",
            provider_name="paymesh",
        )
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


def pick_account(job_id: str, cdks: list[str], *, routed_domains: Sequence = ()) -> PaymeshMailAccount:
    from core.paymesh_aliases import normalize_paymesh_routed_domains
    from core.provider_card_ledger import ProviderCardQuotaError

    owner = str(job_id or "").strip()
    api_base, timeout, limit = _config_values()
    cdks = [str(cdk).strip() for cdk in (cdks or []) if str(cdk).strip()]
    if not cdks:
        raise PaymeshMailError("Chưa nhập Paymesh MAIL card cho batch đăng ký")
    deadline = time.monotonic() + _CDK_LOCK_WAIT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        busy_cdk = False
        for cdk in cdks:
            lock = _get_cdk_lock(cdk)
            if not lock.acquire(blocking=False):
                busy_cdk = True
                continue
            lock_owned = True
            try:
                ledger = _ledger()
                try:
                    redeemed = redeem_cdk(cdk, api_base=api_base, timeout=timeout)
                except PaymeshMailError as exc:
                    if "hết quota" not in str(exc) or not ledger.has_card(cdk):
                        raise
                    lookup_data = lookup_order(cdk, api_base=api_base, timeout=timeout)
                    redeemed = _account_from_payload(cdk, lookup_data)
                source_domain = redeemed.email.split("@", 1)[1] if "@" in redeemed.email else None
                domains = normalize_paymesh_routed_domains(routed_domains, source_domain=source_domain)
                variants = _unused_alias_variants(redeemed.email, limit=limit, routed_domains=domains)
                cap = limit * (1 + len(domains))
                slot = ledger.reserve(
                    cdk,
                    variants,
                    owner,
                    remote_remaining=cap,
                    configured_limit=cap,
                    max_capacity=cap,
                )
                seen_codes = _SEEN_CODES_BY_CDK.setdefault(ledger.card_key(cdk), set())
                seen_codes.update(redeemed.seen_codes)
                account = PaymeshMailAccount(
                    email=slot.email,
                    cdk=cdk,
                    remaining_uses=redeemed.remaining_uses,
                    expires_at=redeemed.expires_at,
                    status=redeemed.status,
                    job_id=owner,
                    seen_codes=seen_codes,
                )
                _CONTEXT_CACHE[_cache_key(account.email)] = account
                lock_owned = False
                return account
            except (PaymeshMailError, ProviderCardQuotaError) as exc:
                last_error = exc
                continue
            finally:
                if lock_owned:
                    lock.release()
        if not busy_cdk:
            break
        time.sleep(0.5)
    if last_error is not None:
        raise PaymeshMailError(str(last_error)) from last_error
    raise PaymeshMailError("Không còn Paymesh MAIL card khả dụng")


def _inventory_store(store_path=None):
    global _INVENTORY_STORE
    if _INVENTORY_STORE is None:
        from pathlib import Path

        from core.cdk_inventory_store import CdkInventoryStore

        path = Path(store_path) if store_path else Path(__file__).resolve().parent.parent / "cdk_inventory.sqlite3"
        _INVENTORY_STORE = CdkInventoryStore(path)
    return _INVENTORY_STORE


def pick_account_for_inventory(
    job_id: str,
    inventory_id: str,
    *,
    routed_domains: Sequence = (),
) -> PaymeshMailAccount:
    """Resolve one durable job assignment and reuse the existing Paymesh flow."""
    value = str(inventory_id or "").strip()
    if not value:
        raise PaymeshMailError("Thiếu Paymesh inventory ID đã gán cho job")
    store = _inventory_store()
    inventory = store.get_inventory(value)
    if inventory is None or inventory.provider != "paymesh" or inventory.state != "active":
        raise PaymeshMailError("Paymesh inventory đã gán không còn khả dụng")
    raw_cdk = store.resolve_raw_cdk(value)
    return pick_account(job_id, [raw_cdk], routed_domains=routed_domains)


def pick_account_by_inventory(
    job_id: str,
    inventory_ids: list[str],
    *,
    store_path: str | None = None,
    ttl_seconds: float = 600,
    routed_domains: Sequence = (),
) -> PaymeshMailAccount:
    """Acquire a Paymesh alias using a managed inventory ID with fencing lease.

    Acquires a database lease, redeems remotely, reserves a local alias slot,
    and retains the lease until explicitly released via release_lease().
    """
    import uuid

    from core.cdk_inventory_store import CdkInventoryConflict
    from core.paymesh_aliases import normalize_paymesh_routed_domains

    owner = str(job_id or "").strip()
    if not owner:
        raise PaymeshMailError("Reservation cần job ID")
    ids = [str(inv_id or "").strip() for inv_id in (inventory_ids or []) if str(inv_id or "").strip()]
    if not ids:
        raise PaymeshMailError("Chưa nhập Paymesh inventory ID cho batch đăng ký")
    api_base, timeout, limit = _config_values()
    store = _inventory_store(store_path)
    last_error: Exception | None = None
    for inventory_id in ids:
        inventory = store.get_inventory(inventory_id)
        if not inventory or inventory.provider != "paymesh":
            continue
        if inventory.state not in ("active",):
            continue
        try:
            store.acquire_lease(inventory_id, owner_token=owner, ttl_seconds=ttl_seconds)
        except CdkInventoryConflict:
            continue
        raw_cdk = store.resolve_raw_cdk(inventory_id)
        operation_id = uuid.uuid4().hex
        owner_token = uuid.uuid4().hex
        try:
            redeemed = redeem_cdk(raw_cdk, api_base=api_base, timeout=timeout)
            source_domain = redeemed.email.split("@", 1)[1] if "@" in redeemed.email else None
            domains = normalize_paymesh_routed_domains(routed_domains, source_domain=source_domain)
            variants = _unused_alias_variants(redeemed.email, limit=limit, routed_domains=domains)
            cap = limit * (1 + len(domains))
            if inventory.configured_limit < cap:
                try:
                    store.update_configured_limit(inventory_id, cap)
                except Exception:
                    pass
            reservation = store.reserve_first_available_slot(
                inventory_id, variants, owner,
                operation_id=operation_id, owner_token=owner_token,
            )
            seen_codes = _SEEN_CODES_BY_CDK.setdefault(
                inventory.fingerprint, set(),
            )
            seen_codes.update(redeemed.seen_codes)
            account = PaymeshMailAccount(
                email=reservation.email,
                cdk=raw_cdk,
                remaining_uses=redeemed.remaining_uses,
                expires_at=redeemed.expires_at,
                status=redeemed.status,
                job_id=owner,
                seen_codes=seen_codes,
                inventory_id=inventory_id,
                reservation_id=reservation.reservation_id,
                owner_token=owner_token,
            )
            _CONTEXT_CACHE[_cache_key(account.email)] = account
            return account
        except (PaymeshMailError, Exception) as exc:
            last_error = exc
            try:
                store.release_lease(inventory_id, owner_token=owner, fencing_token="")
            except Exception:
                pass
            continue
    if last_error is not None:
        raise PaymeshMailError(str(last_error)) from last_error
    raise PaymeshMailError("Không còn Paymesh inventory khả dụng")


def release_lease(inventory_id: str) -> bool:
    """Convenience: release any active lease on an inventory ID."""
    store = _inventory_store()
    try:
        from contextlib import closing
        with closing(store._connect()) as connection:
            row = connection.execute(
                "SELECT lease_id, owner_token, fencing_token FROM cdk_leases "
                "WHERE inventory_id = ? AND state = 'active'",
                (inventory_id,),
            ).fetchone()
            if row:
                return store.release_lease(
                    row["lease_id"],
                    owner_token=row["owner_token"],
                    fencing_token=row["fencing_token"],
                )
    except Exception:
        pass
    return False


def get_account_context(email: str) -> PaymeshMailAccount | None:
    cached = _CONTEXT_CACHE.get(_cache_key(email))
    if cached is not None:
        return cached
    # Durable fallback: resolve from SQLite inventory reservation by email.
    try:
        from contextlib import closing

        store = _inventory_store()
        with closing(store._connect()) as connection:
            row = connection.execute(
                "SELECT s.*, i.raw_cdk, i.inventory_id FROM cdk_slots s "
                "JOIN cdk_inventory i ON i.inventory_id = s.inventory_id "
                "WHERE s.email = ? AND s.state = 'reserved' AND i.provider = 'paymesh'",
                (_cache_key(email),),
            ).fetchone()
            if row:
                account = PaymeshMailAccount(
                    email=row["email"],
                    cdk=row["raw_cdk"],
                    remaining_uses=0,
                    job_id=row["job_id"],
                    inventory_id=row["inventory_id"],
                    reservation_id=row["slot_id"],
                    owner_token=row["owner_token"],
                )
                _CONTEXT_CACHE[_cache_key(email)] = account
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
    del settle_seconds
    from config import email as _email_cfg

    account = get_account_context(email)
    if account is None:
        raise PaymeshMailError("Không tìm thấy context Paymesh để lấy OTP")
    api_base, timeout, _ = _config_values()
    return poll_verification_code(
        account,
        max_wait=int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT),
        poll_interval=int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL),
        api_base=api_base,
        timeout=timeout,
        after_ts=after_ts,
    )


def block_account_card(email: str, reason: str = "provider_rejected") -> bool:
    """Block the raw Paymesh card after a provider-level account rejection."""
    account = get_account_context(email)
    if account is None or account.reservation_id or account.inventory_id:
        return False
    return bool(_ledger().block_card(account.email, account.job_id, reason))


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    del note
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
    target = str(status or "available").strip().lower()
    if target in {"", "available", "reserved"}:
        changed = bool(_ledger().release(account.email, account.job_id))
    else:
        changed = bool(_ledger().mark(account.email, account.job_id, target))
    _get_cdk_lock(account.cdk).release()
    return changed


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
        if changed:
            _CONTEXT_CACHE.pop(_cache_key(email), None)
        return changed
    try:
        changed = bool(_ledger().consume(account.email, account.job_id))
    finally:
        # Raw CDK reservations retain the per-CDK lock until the account is
        # consumed or released; always clean up even when ledger transition fails.
        _CONTEXT_CACHE.pop(_cache_key(email), None)
        try:
            _get_cdk_lock(account.cdk).release()
        except RuntimeError:
            pass
    return changed
