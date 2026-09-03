"""Batch plan checks for imported emails, including credential login when needed."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from core import db
from core.account_network import (
    normalize_account_network_mode,
    selected_account_proxy,
)
from core.free_plus_export import is_free_plus_account
from core.openai_auth import account_unusable_message, detect_account_unusable_text
from core.rotating_proxy_runtime import PLAN_IMPORT_LOGIN_PROXY_SCOPE

MAX_IMPORTED_PLAN_RECORDS = 500
logger = logging.getLogger(__name__)


def preflight_login_network(network_mode: str = "auto") -> str:
    """Verify that the selected login route can be acquired before importing."""
    with selected_account_proxy(
        network_mode,
        rotating_scope=PLAN_IMPORT_LOGIN_PROXY_SCOPE,
        lease_owner_id="plan-import-preflight",
    ) as (_active_proxy, resolved_mode):
        return resolved_mode


def parse_imported_emails(text: str) -> list[str]:
    """Parse one email per line; optional legacy fields after ``|`` are ignored."""
    emails: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        email = line.split("|", 1)[0].strip()
        if not email or "@" not in email or any(char.isspace() for char in email):
            continue
        emails.append(email)
    return emails


def parse_imported_credentials(text: str) -> dict[str, dict[str, str]]:
    """Return optional ``email | password | 2FA`` credentials keyed by email."""
    from core.codex_account_import import parse_credential_lines

    records: dict[str, dict[str, str]] = {}
    for record in parse_credential_lines(text):
        email = str(record.get("email") or "").strip()
        if email and email.casefold() not in records:
            records[email.casefold()] = {
                "email": email,
                "registration_password": str(record.get("registration_password") or ""),
                "totp_secret": str(record.get("totp_secret") or "").strip(),
            }
    return records


def _login_and_save_account(
    *,
    account_id: int,
    email: str,
    password: str,
    totp_secret: str,
    network_mode: str = "auto",
) -> dict:
    """Login with imported credentials, then persist the resulting access token."""
    from core.account_security import TwofaChangeInput, _login_and_get_access_token
    from core.browser_profile import open_browser_profile
    from core.openai_auth import (
        account_unusable_message,
        detect_account_unusable_text,
    )

    profile = None
    try:
        with selected_account_proxy(
            network_mode,
            rotating_scope=PLAN_IMPORT_LOGIN_PROXY_SCOPE,
            lease_owner_id=f"plan-import-login:{account_id}",
        ) as (active_proxy, resolved_mode):
            logger.info("[Plan import] login network=%s account_id=%s", resolved_mode, account_id)
            profile = open_browser_profile() if active_proxy is None else open_browser_profile(proxy=active_proxy)
            access_token = _login_and_get_access_token(
                profile.driver,
                TwofaChangeInput(email=email, password=password, current_totp_secret=totp_secret),
            )
            if not db.update_account_access_token(account_id, access_token=access_token):
                raise RuntimeError("无法把登录后的 accessToken 写入账号")
            return {"ok": True, "network_mode": resolved_mode}
    except Exception as exc:  # noqa: BLE001 - one failed login must not stop the batch.
        error_code = detect_account_unusable_text(str(exc))
        if error_code:
            return {
                "ok": False,
                "status": "deactivated",
                "error_code": error_code,
                "error": account_unusable_message(error_code),
            }
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    finally:
        if profile is not None:
            try:
                profile.close()
            except Exception:
                logger.debug("Plan-import browser close failed", exc_info=True)
            try:
                profile.cleanup()
            except Exception:
                logger.debug("Plan-import browser cleanup failed", exc_info=True)


def _mark_login_failed(
    account_id: int,
    error: str,
    *,
    stage: str = "login",
    account_status: str | None = None,
) -> None:
    try:
        updated = db.update_account_plan_check(
            acc_id=account_id,
            result={
                "ok": False,
                "error": str(error or "账号登录失败")[:240],
                "check_stage": stage,
                "account_status": account_status,
            },
        )
    except Exception:
        logger.exception("[Plan import] failed to persist account failure: account_id=%s", account_id)
        return
    if not updated:
        logger.error("[Plan import] account failure target not found: account_id=%s", account_id)


def _run_login_then_plan_check(
    *,
    account_id: int,
    email: str,
    password: str,
    totp_secret: str,
    network_mode: str,
    login_and_save: Callable[..., dict],
    enqueue: Callable[..., dict],
) -> None:
    try:
        login_result = login_and_save(
            account_id=account_id,
            email=email,
            password=password,
            totp_secret=totp_secret,
            network_mode=network_mode,
        )
        if not isinstance(login_result, dict) or not login_result.get("ok"):
            error = login_result.get("error") if isinstance(login_result, dict) else "账号登录失败"
            account_status = login_result.get("status") if isinstance(login_result, dict) else None
            _mark_login_failed(
                account_id,
                str(error or "账号登录失败"),
                stage="login",
                account_status=str(account_status or "").strip().lower() or None,
            )
            return

        account = db.get_account(account_id) or {}
        access_token = str(account.get("access_token") or "").strip()
        if not access_token:
            _mark_login_failed(account_id, "登录完成但没有保存 accessToken", stage="login")
            return
        queued = enqueue(
            account_id=account_id,
            email=str(account.get("email") or email),
            access_token=access_token,
            trigger="manual_import",
            proxy=None,
            timezone_offset_min="-",
        )
        if not queued.get("accepted"):
            _mark_login_failed(account_id, queued.get("error") or "账号无法进入套餐检查队列", stage="plan")
    except Exception as exc:  # noqa: BLE001 - isolate one account from the batch.
        error_text = str(exc)
        error_code = detect_account_unusable_text(error_text)
        _mark_login_failed(
            account_id,
            account_unusable_message(error_code) if error_code else f"{type(exc).__name__}: {error_text[:180]}",
            stage="login",
            account_status="deactivated" if error_code else None,
        )


def _schedule_login_tasks(
    tasks: list[dict],
    *,
    login_and_save: Callable[..., dict],
    enqueue: Callable[..., dict],
    workers: int,
) -> None:
    if not tasks:
        return
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(4, int(workers or 1), len(tasks))),
        thread_name_prefix="plan-import-login",
    )
    remaining = len(tasks)
    remaining_lock = threading.Lock()

    def done(_future) -> None:
        nonlocal remaining
        with remaining_lock:
            remaining -= 1
            finished = remaining == 0
        if finished:
            executor.shutdown(wait=False, cancel_futures=False)

    for task in tasks:
        future = executor.submit(
            _run_login_then_plan_check,
            **task,
            login_and_save=login_and_save,
            enqueue=enqueue,
        )
        future.add_done_callback(done)


def queue_imported_plan_checks(
    text: str,
    *,
    get_account_by_email: Callable[[str], dict | None],
    enqueue: Callable[..., dict],
    insert_account: Callable[..., int] | None = None,
    login_and_save: Callable[..., dict] | None = None,
    login_network_mode: str = "auto",
    login_workers: int = 1,
    preflight_login_network: Callable[[str], str] | None = None,
    force_login_emails: Iterable[str] | None = None,
    max_records: int = MAX_IMPORTED_PLAN_RECORDS,
) -> dict:
    """Queue plan checks, logging in and persisting accounts missing a token."""
    if insert_account is None:
        insert_account = db.insert_account
    if login_and_save is None:
        login_and_save = _login_and_save_account
    login_network_mode = normalize_account_network_mode(login_network_mode)
    emails = parse_imported_emails(text)
    credentials = parse_imported_credentials(text)
    force_login_keys = {
        str(email or "").strip().casefold()
        for email in (force_login_emails or ())
        if str(email or "").strip()
    }
    if len(emails) > max_records:
        raise ValueError(f"Mỗi lần chỉ được nhập tối đa {max_records} tài khoản")

    login_network_error: str | None = None
    if credentials and preflight_login_network is not None:
        try:
            preflight_login_network(login_network_mode)
        except Exception as exc:  # noqa: BLE001 - report route failure per account.
            login_network_error = (
                f"Không lấy được route đăng nhập ({login_network_mode}): "
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
            logger.warning("[Plan import] login route preflight failed: %s", login_network_error)

    started: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    login_tasks: list[dict] = []
    seen_emails: set[str] = set()

    for email in emails:
        email = str(email or "").strip()
        email_key = email.casefold()
        if not email_key or email_key in seen_emails:
            skipped.append({"email": email, "reason": "duplicate"})
            continue
        seen_emails.add(email_key)

        account = get_account_by_email(email)
        account_was_missing = account is None
        if not account:
            credential = credentials.get(email_key)
            if not credential:
                skipped.append({"email": email, "reason": "account_not_found"})
                continue
            try:
                account_id = int(insert_account(
                    email=email,
                    access_token="",
                    registration_password=credential["registration_password"],
                    totp_secret=credential["totp_secret"],
                    twofa_status="active",
                    email_source="credentials",
                    extra={"imported_credentials": True},
                ))
            except Exception as exc:  # noqa: BLE001 - isolate one import row.
                failed.append({"email": email, "reason": f"account_save_failed: {type(exc).__name__}: {exc}"})
                continue
            account = {"id": account_id, "email": email, "access_token": ""}

        if str(account.get("live_check_status") or "").strip().lower() == "deactivated":
            skipped.append({
                "id": account.get("id"),
                "email": str(account.get("email") or email),
                "reason": "account_deactivated",
            })
            continue

        try:
            account_id = int(account.get("id"))
        except (TypeError, ValueError):
            failed.append({"email": email, "reason": "account_id_invalid"})
            continue

        access_token = "" if email_key in force_login_keys else str(account.get("access_token") or "").strip()
        if not access_token:
            credential = credentials.get(email_key)
            if not credential:
                password = str(account.get("registration_password") or "").strip()
                totp_secret = str(account.get("totp_secret") or "").strip()
                if password and totp_secret:
                    credential = {
                        "email": str(account.get("email") or email),
                        "registration_password": password,
                        "totp_secret": totp_secret,
                    }
            if not credential:
                skipped.append({
                    "id": account_id,
                    "email": str(account.get("email") or email),
                    "reason": "missing_access_token",
                })
                continue
            if login_network_error:
                failed.append({
                    "id": account.get("id"),
                    "email": str(account.get("email") or email),
                    "reason": login_network_error,
                })
                continue
            if not account_was_missing:
                db.update_account_login_credentials(
                    account_id,
                    password=str(credential.get("registration_password") or ""),
                    totp_secret=str(credential.get("totp_secret") or ""),
                )
            if not db.mark_account_plan_login_pending(account_id):
                failed.append({"id": account_id, "email": email, "reason": "account_login_pending_failed"})
                continue
            login_tasks.append({
                "account_id": account_id,
                "email": str(credential.get("email") or account.get("email") or email),
                "password": str(credential.get("registration_password") or ""),
                "totp_secret": str(credential.get("totp_secret") or ""),
                "network_mode": login_network_mode,
            })
            started.append({
                "id": account_id,
                "email": str(account.get("email") or email),
                "status": "login_queued",
            })
            continue

        queued = enqueue(
            account_id=account_id,
            email=str(account.get("email") or email),
            access_token=access_token,
            trigger="manual_import",
            proxy=None,
            timezone_offset_min="-",
        )
        item = {
            "id": account_id,
            "email": str(account.get("email") or email),
        }
        if queued.get("accepted"):
            started.append({**item, "status": queued.get("status") or "queued"})
        elif queued.get("busy"):
            skipped.append({**item, "reason": "busy"})
        else:
            failed.append({**item, "reason": queued.get("error") or "queue_failed"})

    non_empty_lines = [
        line for line in str(text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    _schedule_login_tasks(
        login_tasks,
        login_and_save=login_and_save,
        enqueue=enqueue,
        workers=login_workers,
    )
    return {
        "ok": True,
        "parsed_count": len(emails),
        "ignored_count": max(0, len(non_empty_lines) - len(emails)),
        "started": started,
        "started_count": len(started),
            "login_started_count": len(login_tasks),
        "login_network_mode": login_network_mode,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "failed": failed,
        "failed_count": len(failed),
    }


def _classify_plan_status(row: dict) -> str:
    if str(row.get("live_check_status") or "").strip().lower() == "deactivated":
        return "deactivated"
    status = str(row.get("plan_check_status") or "").strip().lower()
    if status in {"login_pending", "queued", "running"}:
        return "pending"
    if status != "success" or row.get("plan_check_ok") is False:
        error = str(row.get("plan_check_error") or "").lower()
        stage = str(row.get("plan_check_stage") or "").strip().lower()
        if stage == "login" or "credential login" in error or "accesstoken" in error:
            return "login_failed"
        if row.get("needs_live_check") or "过期" in error or "expired" in error or "失效" in error:
            return "needs_live_check"
        return "check_failed"
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if plan == "free":
        return "free_plus_trial" if is_free_plus_account(row) else "free_without_trial"
    return "not_free_plan"


def build_import_plan_status(rows: Iterable[dict]) -> dict:
    """Build a token-free report from current local plan-check rows."""
    items: list[dict] = []
    for row in rows:
        classification = _classify_plan_status(row)
        item = {
            "id": row.get("id"),
            "email": row.get("email"),
            "status": row.get("plan_check_status"),
            "ok": row.get("plan_check_ok"),
            "plan_type": row.get("current_plan_type") or row.get("plan_type"),
            "plus_trial_eligible": row.get("plus_trial_eligible"),
            "checked_at": row.get("plan_checked_at"),
            "error": (
                row.get("live_check_error") or account_unusable_message("account_deactivated")
                if classification == "deactivated"
                else row.get("plan_check_error")
            ),
            "stage": row.get("plan_check_stage"),
            "live_check_status": row.get("live_check_status"),
            "classification": classification,
        }
        items.append(item)

    free_plan_accounts = [
        item for item in items
        if item["classification"] in {"free_without_trial", "free_plus_trial"}
    ]
    free_without_trial_accounts = [
        item for item in free_plan_accounts if item["classification"] == "free_without_trial"
    ]
    free_plus_trial_accounts = [
        item for item in free_plan_accounts if item["classification"] == "free_plus_trial"
    ]
    pending_count = sum(item["classification"] == "pending" for item in items)
    not_free_plan_count = sum(item["classification"] == "not_free_plan" for item in items)
    needs_live_check_count = sum(
        item["classification"] == "needs_live_check" for item in items
    )
    check_failed_count = sum(item["classification"] == "check_failed" for item in items)
    login_failed_count = sum(item["classification"] == "login_failed" for item in items)
    deactivated_count = sum(item["classification"] == "deactivated" for item in items)
    return {
        "ok": True,
        "items": items,
        "total": len(items),
        "pending_count": pending_count,
        "free_plan_count": len(free_plan_accounts),
        "free_plan_accounts": free_plan_accounts,
        "free_without_trial_count": len(free_without_trial_accounts),
        "free_without_trial_accounts": free_without_trial_accounts,
        "free_plus_trial_count": len(free_plus_trial_accounts),
        "free_plus_trial_accounts": free_plus_trial_accounts,
        "not_free_plan_count": not_free_plan_count,
        "needs_live_check_count": needs_live_check_count,
        "check_failed_count": check_failed_count,
        "login_failed_count": login_failed_count,
        "deactivated_count": deactivated_count,
    }
