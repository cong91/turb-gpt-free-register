"""Browser lifecycle for credential-driven ChatGPT email changes."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import db
from core.browser_profile import open_browser_profile
from core.email_change import EmailChangeInput, change_email_in_browser
from core.rotating_proxy_runtime import (
    EMAIL_CHANGE_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
    release_rotating_proxy,
    resolve_rotating_proxy,
)

logger = logging.getLogger(__name__)
_EMAIL_CHANGE_RESTART_ATTEMPTS = 2
_EMAIL_CHANGE_RESTART_ATTEMPTS = 2


def _redacted_error(exc: Exception, item: EmailChangeInput) -> str:
    message = f"{type(exc).__name__}: {str(exc)[:300]}"
    for secret in (item.password, item.totp_secret, item.code_url):
        if secret:
            message = message.replace(secret, "[redacted]")
    return re.sub(r"\b\d{6,8}\b", "[redacted-code]", message)


def run_email_change(
    item: EmailChangeInput,
    *,
    proxy_lane_id: int | None = None,
) -> dict[str, object]:
    """Run one email change with bounded fresh-profile recovery."""
    last_result: dict[str, object] = {"ok": False}
    for attempt in range(_EMAIL_CHANGE_RESTART_ATTEMPTS):
        profile = None
        rotating_proxy: str | None = None
        try:
            active_proxy = resolve_rotating_proxy(None, scope=EMAIL_CHANGE_PROXY_SCOPE, lane_id=proxy_lane_id)
            rotating_proxy = active_proxy
            profile = open_browser_profile(proxy=active_proxy) if active_proxy is not None else open_browser_profile()
            result = change_email_in_browser(profile.driver, item)
            result["browser_provider"] = profile.provider
            last_result = result
            if not bool(result.get("ok")):
                result.setdefault("persisted", False)
                if not result.get("retryable", True) or attempt + 1 >= _EMAIL_CHANGE_RESTART_ATTEMPTS:
                    return result
                continue
            try:
                persisted = db.update_account_email(item.old_email, item.new_email)
            except Exception as exc:
                persisted = False
                result["warning"] = _redacted_error(exc, item)
            result["persisted"] = persisted
            if persisted:
                updated = db.get_account_by_email(item.new_email)
                if updated and updated.get("id") is not None:
                    result["account_id"] = int(updated["id"])
            source_email = item.gmail_source_email or item.new_email
            if db.get_gmail_api_url_email_by_email(source_email):
                db.release_gmail_api_url_email(source_email, "used", "account email changed")
            if not persisted:
                result["warning"] = result.get("warning") or "source account was not found in local persistence"
                result["retryable"] = False
            return result
        except Exception as exc:
            last_result = {"ok": False, "old_email": item.old_email, "new_email": item.new_email, "error": _redacted_error(exc, item), "retryable": True}
            if attempt + 1 >= _EMAIL_CHANGE_RESTART_ATTEMPTS:
                return last_result
        finally:
            if rotating_proxy is not None:
                release_rotating_proxy(scope=EMAIL_CHANGE_PROXY_SCOPE, lane_id=proxy_lane_id, proxy_url=rotating_proxy)
            if profile is not None:
                try: profile.close()
                except Exception: logger.debug("Browser driver cleanup failed")
                try: profile.cleanup()
                except Exception: logger.debug("Browser profile cleanup failed")
    return last_result


def run_email_change_batch(
    items: list[EmailChangeInput],
    *,
    workers: int = 1,
) -> list[dict[str, object]]:
    """Run different Gmail APIs concurrently while serializing each shared OTP URL."""
    grouped: dict[str, list[tuple[int, EmailChangeInput]]] = {}
    for index, item in enumerate(items):
        grouped.setdefault(item.code_url, []).append((index, item))

    def run_group(
        group: list[tuple[int, EmailChangeInput]],
        proxy_lane_id: int,
    ) -> list[tuple[int, dict[str, object]]]:
        from config import proxy as proxy_config

        rotating_enabled = bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False))
        return [
            (
                index,
                run_email_change(item, proxy_lane_id=proxy_lane_id)
                if rotating_enabled
                else run_email_change(item),
            )
            for index, item in group
        ]

    max_workers = max(1, min(4, int(workers or 1), len(grouped) or 1))
    if grouped:
        prepare_rotating_proxy_lanes(max_workers, scope=EMAIL_CHANGE_PROXY_SCOPE)
    results: list[dict[str, object] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="email-change") as executor:
        futures = [
            executor.submit(run_group, group, lane_id % max_workers)
            for lane_id, group in enumerate(grouped.values())
        ]
        for future in as_completed(futures):
            for index, result in future.result():
                results[index] = result
    return [result for result in results if result is not None]
