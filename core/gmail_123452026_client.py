from __future__ import annotations

import logging
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_aliases import (
    GmailAliasError,
    build_gmail_alias_plan,
    normalize_routed_domains,
)

DEFAULT_API_BASE = "http://gmail.123452026.xyz/api"
_HEADERS = {"Accept": "*/*", "Content-Type": "application/json"}
logger = logging.getLogger(__name__)


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
    assignment_id: str | None = None
    batch_id: str | None = None
    alias_phase: str | None = None
    alias_domain: str | None = None

    def __repr__(self) -> str:
        return (
            f"Gmail123452026Account(email={self.email!r}, "
            f"remaining_uses={self.remaining_uses!r}, expires_at={self.expires_at!r})"
        )


def _endpoint(api_base: str, path: str, allow_insecure_http: bool) -> str:
    del allow_insecure_http
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
    raw_email = str(data.get("emailAddress") or "").strip()
    try:
        source_plan = build_gmail_alias_plan(raw_email, limit=1)
        email = source_plan.original_candidates[0].email
    except (GmailAliasError, IndexError) as exc:
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


def _otp_store():
    global _OTP_STORE
    if _OTP_STORE is None:

        from core.otp_identity_store import OtpIdentityStore

        _OTP_STORE = OtpIdentityStore(APP_STATE_DB_PATH)
    return _OTP_STORE


def _claim_otp(cdk: str, code: str) -> bool:
    store = _otp_store()
    fingerprint = store.fingerprint("gmail_123452026", cdk)
    return bool(
        store.claim_if_unseen(
            "gmail_123452026",
            fingerprint,
            f"value:{code}",
        )
    )


def poll_verification_code(
    account: Gmail123452026Account,
    *,
    max_wait: int,
    poll_interval: int = 2,
    session=None,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
    allow_insecure_http: bool = False,
) -> str:
    url = _endpoint(api_base, "/mailbox/code", allow_insecure_http)
    deadline = time.monotonic() + max(0, int(max_wait))
    client = session or requests
    while time.monotonic() <= deadline:
        data = _post_json(
            client,
            url,
            {"cdk": account.cdk, "locktime": 5},
            max(1, int(timeout)),
        )
        status = str(data.get("status") or "").strip().lower()
        code = str(data.get("code") or "").strip()
        if status == "success" and code and code not in account.seen_codes:
            if not _claim_otp(account.cdk, code):
                account.seen_codes.add(code)
                continue
            account.seen_codes.add(code)
            return code
        if status == "email_invalid":
            message = str(data.get("message") or "email invalid").strip()
            raise Gmail123452026Error(message or "email invalid")
        if status not in {"success", "processing"}:
            message = str(data.get("message") or "").strip()
            detail = f": {message}" if message else ""
            raise Gmail123452026Error(
                f"API lấy OTP trả trạng thái không hợp lệ: {status or 'unknown'}{detail}"
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0, int(poll_interval)))
    raise Gmail123452026Error(f"Chờ OTP Gmail CDK quá thời gian: {account.email}")


_CONTEXT_CACHE: dict[str, Gmail123452026Account] = {}
_SEEN_CODES_BY_CDK: dict[str, set[str]] = {}
_LEDGER: object | None = None
_INVENTORY_STORE: object | None = None
_BATCH_STORE: object | None = None
_OTP_STORE: object | None = None
_CDK_LOCKS: dict[str, threading.Lock] = {}
_CDK_LOCKS_GUARD = threading.Lock()
_CDK_LOCK_WAIT = 600


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _cdk_lock_key(cdk: str) -> str:
    return str(cdk or "").strip().upper()


def _get_cdk_lock(cdk: str) -> threading.Lock:
    key = _cdk_lock_key(cdk)
    with _CDK_LOCKS_GUARD:
        if key not in _CDK_LOCKS:
            _CDK_LOCKS[key] = threading.Lock()
        return _CDK_LOCKS[key]


def _release_cdk_lock(account: Gmail123452026Account) -> None:
    try:
        _get_cdk_lock(account.cdk).release()
    except RuntimeError:
        pass


def _config_values() -> tuple[str, int, int, bool]:
    from config import email as _email_cfg

    api_base = str(
        getattr(_email_cfg, "GMAIL_123452026_API_BASE", DEFAULT_API_BASE)
        or DEFAULT_API_BASE
    ).strip()
    timeout = max(1, int(getattr(_email_cfg, "GMAIL_123452026_REQUEST_TIMEOUT", 30) or 30))
    limit = max(
        1,
        min(6, int(getattr(_email_cfg, "GMAIL_123452026_ACCOUNTS_PER_CDK", 6) or 6)),
    )
    allow_http = bool(getattr(_email_cfg, "GMAIL_123452026_ALLOW_INSECURE_HTTP", False))
    return api_base, timeout, limit, allow_http


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        from core.gmail_cdk_ledger import GmailCdkLedger

        _LEDGER = GmailCdkLedger(APP_STATE_DB_PATH)
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


def _inventory_store(store_path=None):
    global _INVENTORY_STORE
    if _INVENTORY_STORE is None:
        from pathlib import Path

        from core.cdk_inventory_store import CdkInventoryStore

        path = (
            Path(store_path)
            if store_path
            else APP_STATE_DB_PATH
        )
        _INVENTORY_STORE = CdkInventoryStore(path)
    return _INVENTORY_STORE


def _batch_store():
    global _BATCH_STORE
    if _BATCH_STORE is None:

        from core.gmail_cdk_batch_store import GmailCdkBatchStore

        _BATCH_STORE = GmailCdkBatchStore(
            APP_STATE_DB_PATH
        )
    return _BATCH_STORE


def create_registration_batch(cdks: list[str], *, routed_domains=()) -> str:
    raw_cdks = list(dict.fromkeys(
        str(cdk or "").strip() for cdk in (cdks or []) if str(cdk or "").strip()
    ))
    if not raw_cdks:
        raise Gmail123452026Error("Chưa nhập Gmail CDK cho batch đăng ký")
    domains = normalize_routed_domains(routed_domains)
    _, _, limit, _ = _config_values()
    store = _inventory_store()
    inventory_ids = [
        store.import_cdk("gmail", cdk, configured_limit=limit)[0].inventory_id
        for cdk in raw_cdks
    ]
    return _batch_store().create_batch(
        inventory_ids,
        capacity=limit * (2 if domains else 1),
        routed_domains=domains,
    )


def pick_account_by_batch(
    job_id: str,
    batch_id: str,
    *,
    routed_domains=(),
) -> Gmail123452026Account:
    import uuid

    from core.cdk_inventory_store import CdkInventoryConflict

    owner = str(job_id or "").strip()
    if not owner:
        raise Gmail123452026Error("Reservation cần job ID")
    domains = normalize_routed_domains(routed_domains)
    api_base, timeout, limit, allow_http = _config_values()
    assignment = _batch_store().claim(batch_id, owner)
    store = _inventory_store()
    raw_cdk = store.resolve_raw_cdk(assignment.inventory_id)
    lock = _get_cdk_lock(raw_cdk)
    deadline = time.monotonic() + _CDK_LOCK_WAIT
    while not lock.acquire(blocking=False):
        if time.monotonic() >= deadline:
            _batch_store().release(assignment.assignment_id, reason="CDK lock timeout")
            raise Gmail123452026Error("Gmail CDK đang được job khác sử dụng")
        time.sleep(0.1)
    owner_token = uuid.uuid4().hex
    try:
        redeemed = redeem_cdk(
            raw_cdk,
            api_base=api_base,
            timeout=timeout,
            allow_insecure_http=allow_http,
        )
        plan = build_gmail_alias_plan(
            redeemed.email,
            limit=limit,
            routed_domains=domains,
        )
        reservation = store.reserve_gmail_alias(
            assignment.inventory_id,
            plan.candidates,
            owner,
            operation_id=uuid.uuid4().hex,
            owner_token=owner_token,
            routed_domains=plan.routed_domains,
        )
        store.update_provider_quota(assignment.inventory_id, redeemed.remaining_uses)
    except (Gmail123452026Error, CdkInventoryConflict, Exception) as exc:
        _batch_store().fail(assignment.assignment_id, reason=str(exc))
        _release_cdk_lock(Gmail123452026Account("", raw_cdk, 0))
        raise Gmail123452026Error(str(exc)) from exc
    from core.gmail_cdk_ledger import GmailCdkLedger

    account = Gmail123452026Account(
        email=reservation.email,
        cdk=raw_cdk,
        remaining_uses=redeemed.remaining_uses,
        expires_at=redeemed.expires_at,
        job_id=owner,
        seen_codes=_SEEN_CODES_BY_CDK.setdefault(
            GmailCdkLedger.cdk_key(raw_cdk), set()
        ),
        inventory_id=assignment.inventory_id,
        reservation_id=reservation.reservation_id,
        owner_token=owner_token,
        assignment_id=assignment.assignment_id,
        batch_id=batch_id,
        alias_phase=reservation.alias_phase,
        alias_domain=reservation.alias_domain,
    )
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    return account


def pick_account(
    job_id: str,
    cdks: list[str],
    *,
    routed_domains=(),
) -> Gmail123452026Account:
    from core.gmail_aliases import generate_gmail_variants
    from core.gmail_cdk_ledger import GmailCdkQuotaError

    owner = str(job_id or "").strip()
    api_base, timeout, limit, allow_http = _config_values()
    cdks = [str(cdk).strip() for cdk in (cdks or []) if str(cdk).strip()]
    domains = normalize_routed_domains(routed_domains)
    if not cdks:
        raise Gmail123452026Error("Chưa nhập Gmail CDK cho batch đăng ký")
    last_error: Exception | None = None
    for cdk in cdks:
        lock = _get_cdk_lock(cdk) if domains else None
        lock_owned = False
        try:
            if lock is not None:
                if not lock.acquire(blocking=False):
                    continue
                lock_owned = True
            redeemed = redeem_cdk(
                cdk,
                api_base=api_base,
                timeout=timeout,
                allow_insecure_http=allow_http,
            )
            if domains:
                plan = build_gmail_alias_plan(
                    redeemed.email,
                    limit=limit,
                    routed_domains=domains,
                )
                slot = _ledger().reserve_plan(cdk, plan, owner)
            else:
                slot = _ledger().reserve(
                    cdk,
                    generate_gmail_variants(redeemed.email, limit=limit),
                    owner,
                    remote_remaining=limit,
                    configured_limit=limit,
                )
            from core.gmail_cdk_ledger import GmailCdkLedger

            account = Gmail123452026Account(
                email=slot.email,
                cdk=cdk,
                remaining_uses=redeemed.remaining_uses,
                expires_at=redeemed.expires_at,
                job_id=owner,
                seen_codes=_SEEN_CODES_BY_CDK.setdefault(
                    GmailCdkLedger.cdk_key(cdk), set()
                ),
                alias_phase=slot.phase,
                alias_domain=slot.domain,
            )
            _CONTEXT_CACHE[_cache_key(account.email)] = account
            lock_owned = False
            return account
        except (Gmail123452026Error, GmailCdkQuotaError) as exc:
            last_error = exc
        finally:
            if lock_owned:
                lock.release()
    if last_error is not None:
        raise Gmail123452026Error(str(last_error)) from last_error
    raise Gmail123452026Error("Không còn Gmail CDK khả dụng")


def pick_account_by_inventory(
    job_id: str,
    inventory_ids: list[str],
    *,
    store_path: str | None = None,
    routed_domains=(),
) -> Gmail123452026Account:
    import uuid

    owner = str(job_id or "").strip()
    if not owner:
        raise Gmail123452026Error("Reservation cần job ID")
    ids = [str(inv_id or "").strip() for inv_id in (inventory_ids or []) if str(inv_id or "").strip()]
    if not ids:
        raise Gmail123452026Error("Chưa nhập Gmail CDK inventory ID cho batch đăng ký")
    domains = normalize_routed_domains(routed_domains)
    api_base, timeout, limit, allow_http = _config_values()
    store = _inventory_store(store_path)
    last_error: Exception | None = None
    for inventory_id in ids:
        inventory = store.get_inventory(inventory_id)
        if not inventory or inventory.provider != "gmail" or inventory.state not in ("active",):
            continue
        raw_cdk = store.resolve_raw_cdk(inventory_id)
        owner_token = uuid.uuid4().hex
        try:
            redeemed = redeem_cdk(
                raw_cdk,
                api_base=api_base,
                timeout=timeout,
                allow_insecure_http=allow_http,
            )
            if domains:
                plan = build_gmail_alias_plan(
                    redeemed.email,
                    limit=limit,
                    routed_domains=domains,
                )
                reservation = store.reserve_gmail_alias(
                    inventory_id,
                    plan.candidates,
                    owner,
                    operation_id=uuid.uuid4().hex,
                    owner_token=owner_token,
                    routed_domains=plan.routed_domains,
                )
            else:
                from core.gmail_aliases import generate_gmail_variants

                reservation = store.reserve_first_available_slot(
                    inventory_id,
                    generate_gmail_variants(redeemed.email, limit=limit),
                    owner,
                    operation_id=uuid.uuid4().hex,
                    owner_token=owner_token,
                )
            store.update_provider_quota(inventory_id, redeemed.remaining_uses)
            from core.gmail_cdk_ledger import GmailCdkLedger

            account = Gmail123452026Account(
                email=reservation.email,
                cdk=raw_cdk,
                remaining_uses=redeemed.remaining_uses,
                expires_at=redeemed.expires_at,
                job_id=owner,
                seen_codes=_SEEN_CODES_BY_CDK.setdefault(
                    GmailCdkLedger.cdk_key(raw_cdk), set()
                ),
                inventory_id=inventory_id,
                reservation_id=reservation.reservation_id,
                owner_token=owner_token,
                alias_phase=getattr(reservation, "alias_phase", None),
                alias_domain=getattr(reservation, "alias_domain", None),
            )
            _CONTEXT_CACHE[_cache_key(account.email)] = account
            return account
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if last_error is not None:
        raise Gmail123452026Error(str(last_error)) from last_error
    raise Gmail123452026Error("Không còn Gmail CDK inventory khả dụng")


def get_account_context(email: str) -> Gmail123452026Account | None:
    key = _cache_key(email)
    cached = _CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
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

                assignment_id = None
                batch_id = None
                try:
                    assignment = _batch_store().find_active_assignment(
                        row["inventory_id"], row["job_id"]
                    )
                    if assignment is not None:
                        assignment_id = assignment.assignment_id
                        batch_id = assignment.batch_id
                except Exception:
                    logger.debug("[Gmail CDK] active assignment recovery skipped", exc_info=True)
                account = Gmail123452026Account(
                    email=row["email"],
                    cdk=row["raw_cdk"],
                    remaining_uses=0,
                    job_id=row["job_id"],
                    assignment_id=assignment_id,
                    batch_id=batch_id,
                    seen_codes=_SEEN_CODES_BY_CDK.setdefault(
                        GmailCdkLedger.cdk_key(row["raw_cdk"]), set()
                    ),
                    inventory_id=row["inventory_id"],
                    reservation_id=row["slot_id"],
                    owner_token=row["owner_token"],
                    alias_phase=(row["alias_phase"] if "alias_phase" in row else None),  # noqa: SIM401 - sqlite3.Row has no get().
                    alias_domain=(row["alias_domain"] if "alias_domain" in row else None),  # noqa: SIM401 - sqlite3.Row has no get().
                )
                _CONTEXT_CACHE[key] = account
                return account
    except Exception:  # noqa: BLE001, S110
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
        poll_interval=int(
            poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL
        ),
        api_base=api_base,
        timeout=timeout,
        allow_insecure_http=allow_http,
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> bool:
    account = _CONTEXT_CACHE.pop(_cache_key(email), None)
    if account is None:
        return False
    try:
        if account.reservation_id and account.inventory_id:
            import uuid

            changed = bool(
                _inventory_store().release_reservation(
                    account.reservation_id,
                    operation_id=uuid.uuid4().hex,
                    owner_token=account.owner_token or "",
                )
            )
        else:
            changed = bool(_ledger().release(account.email, account.job_id))
        if account.assignment_id:
            text = str(note or "")
            stopped = "停止" in text or "stop" in text.lower() or "cancel" in text.lower()
            if str(status or "available").strip().lower() in {"", "available", "reserved"} and stopped:
                _batch_store().release(account.assignment_id, reason=text)
            else:
                _batch_store().fail(account.assignment_id, reason=text or str(status))
        return changed
    finally:
        _release_cdk_lock(account)


def mark_account_consumed(email: str) -> bool:
    account = get_account_context(email)
    if account is None:
        return False
    try:
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
        if changed and account.assignment_id:
            _batch_store().complete(account.assignment_id)
        return changed
    finally:
        _CONTEXT_CACHE.pop(_cache_key(email), None)
        _release_cdk_lock(account)
