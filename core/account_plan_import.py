"""Batch plan checks for email lists that identify existing accounts."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from core.free_plus_export import is_free_plus_account

MAX_IMPORTED_PLAN_RECORDS = 500


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


def queue_imported_plan_checks(
    text: str,
    *,
    get_account_by_email: Callable[[str], dict | None],
    enqueue: Callable[..., dict],
    max_records: int = MAX_IMPORTED_PLAN_RECORDS,
) -> dict:
    """Match imported emails to local accounts and queue checks using stored tokens."""
    emails = parse_imported_emails(text)
    if len(emails) > max_records:
        raise ValueError(f"Mỗi lần chỉ được nhập tối đa {max_records} tài khoản")

    started: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    seen_emails: set[str] = set()

    for email in emails:
        email = str(email or "").strip()
        email_key = email.casefold()
        if not email_key or email_key in seen_emails:
            skipped.append({"email": email, "reason": "duplicate"})
            continue
        seen_emails.add(email_key)

        account = get_account_by_email(email)
        if not account:
            skipped.append({"email": email, "reason": "account_not_found"})
            continue

        try:
            account_id = int(account.get("id"))
        except (TypeError, ValueError):
            failed.append({"email": email, "reason": "account_id_invalid"})
            continue

        access_token = str(account.get("access_token") or "").strip()
        if not access_token:
            skipped.append({
                "id": account_id,
                "email": str(account.get("email") or email),
                "reason": "missing_access_token",
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
    return {
        "ok": True,
        "parsed_count": len(emails),
        "ignored_count": max(0, len(non_empty_lines) - len(emails)),
        "started": started,
        "started_count": len(started),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "failed": failed,
        "failed_count": len(failed),
    }


def _classify_plan_status(row: dict) -> str:
    status = str(row.get("plan_check_status") or "").strip().lower()
    if status in {"queued", "running"}:
        return "pending"
    if status != "success" or row.get("plan_check_ok") is False:
        error = str(row.get("plan_check_error") or "").lower()
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
            "error": row.get("plan_check_error"),
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
    }
