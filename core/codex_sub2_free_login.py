"""Select confirmed Free accounts without Plus trial for sub2 Codex login."""
from __future__ import annotations


def is_confirmed_free_without_trial(row: dict) -> bool:
    """Only accept an explicit successful Free/no-trial plan result."""
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    return (
        plan == "free"
        and row.get("plan_check_status") == "success"
        and row.get("plan_check_ok") is True
        and row.get("plus_trial_eligible") is False
    )


def is_codex_authenticated(row: dict, authenticated_emails: set[str]) -> bool:
    """Treat a successful status or an existing Codex credential as authenticated."""
    if str(row.get("codex_status") or "").strip().lower() == "success":
        return True
    email = str(row.get("email") or "").strip().lower()
    return bool(email and email in authenticated_emails)


def select_target_ids(
    rows: list[dict],
    *,
    authenticated_emails: set[str] | None = None,
    retrying_emails: set[str] | None = None,
) -> dict:
    """Return runnable account IDs plus explicit skip counts."""
    authenticated_emails = {
        str(email or "").strip().lower()
        for email in (authenticated_emails or set())
        if str(email or "").strip()
    }
    retrying_emails = {
        str(email or "").strip().lower()
        for email in (retrying_emails or set())
        if str(email or "").strip()
    }
    account_ids: list[int] = []
    skipped_authenticated = 0
    skipped_deactivated = 0
    skipped_retrying = 0
    for row in rows:
        if not is_confirmed_free_without_trial(row):
            continue
        if is_codex_authenticated(row, authenticated_emails):
            skipped_authenticated += 1
            continue
        status = str(row.get("codex_status") or "").strip().lower()
        if status == "deactivated":
            skipped_deactivated += 1
            continue
        email = str(row.get("email") or "").strip().lower()
        if status == "retrying" and email in retrying_emails:
            skipped_retrying += 1
            continue
        try:
            account_ids.append(int(row["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "account_ids": account_ids,
        "count": len(account_ids),
        "skipped_authenticated": skipped_authenticated,
        "skipped_deactivated": skipped_deactivated,
        "skipped_retrying": skipped_retrying,
    }
