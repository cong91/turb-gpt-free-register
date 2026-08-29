"""Browser lifecycle for replacing an existing ChatGPT TOTP factor."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import db
from core.account_security import TwofaChangeInput, change_twofa_in_browser
from core.browser_profile import open_browser_profile
from core.rotating_proxy_runtime import TWOFA_CHANGE_PROXY_SCOPE, resolve_rotating_proxy

logger = logging.getLogger(__name__)


def _redacted_error(message: object, item: TwofaChangeInput) -> str:
    output = str(message or "")[:400]
    for secret in (item.password, item.current_totp_secret):
        if secret:
            output = output.replace(secret, "[redacted]")
    return re.sub(r"\b\d{6,8}\b", "[redacted-code]", output)


def _record_remote_disable_failure(
    account_id: int,
    item: TwofaChangeInput,
    result: dict[str, object],
) -> dict[str, object]:
    """Clear the local factor after a remote disable cannot be completed."""
    result["persisted"] = False
    error = _redacted_error(result.get("error"), item)
    try:
        persisted = db.update_account_2fa(
            account_id,
            status="failed",
            totp_secret=None,
            error=f"remote 2FA disabled; replacement failed: {error}",
        )
    except Exception as exc:  # noqa: BLE001 - preserve the remote failure result.
        result["warning"] = _redacted_error(exc, item)
        return result
    if not persisted:
        result["warning"] = "local 2FA failure state was not updated"
    return result


def _record_local_failure(
    account_id: int,
    item: TwofaChangeInput,
    result: dict[str, object],
) -> dict[str, object]:
    """Keep a newly captured account visible without retaining an unusable secret."""
    result["persisted"] = False
    error = _redacted_error(result.get("error"), item)
    try:
        persisted = db.update_account_2fa(
            account_id,
            status="failed",
            totp_secret=None,
            error=error or "2FA change failed",
        )
    except Exception as exc:  # noqa: BLE001 - preserve the workflow result.
        result["warning"] = _redacted_error(exc, item)
        return result
    if not persisted:
        result["warning"] = "local 2FA failure state was not updated"
    return result


def run_twofa_change(
    item: TwofaChangeInput,
    *,
    proxy_lane_id: int | None = None,
) -> dict[str, object]:
    """Replace one account's TOTP and upsert its local persistence row."""
    account = db.get_account_by_email(item.email)
    if account is None:
        is_new_account = True
        try:
            account_id = db.insert_account(
                email=item.email,
                access_token="",
                registration_password=item.password,
                totp_secret=item.current_totp_secret,
                twofa_status="pending",
                twofa_error=None,
                extra={"registration_password": item.password, "personal_info_change": "twofa"},
            )
        except Exception as exc:  # noqa: BLE001 - isolate each account in a batch.
            return {
                "ok": False,
                "persisted": False,
                "email": item.email,
                "error": _redacted_error(f"account save failed: {exc}", item),
            }
    else:
        is_new_account = False
        account_id_value = account.get("id")
        if account_id_value is None:
            return {"ok": False, "persisted": False, "email": item.email, "error": "account id missing"}
        account_id = int(account_id_value)

    profile = None
    try:
        active_proxy = resolve_rotating_proxy(
            None,
            scope=TWOFA_CHANGE_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        profile = (
            open_browser_profile(proxy=active_proxy)
            if active_proxy is not None
            else open_browser_profile()
        )
        result = change_twofa_in_browser(profile.driver, item)
        access_token = str(result.pop("access_token", "") or "").strip()
        if access_token:
            try:
                token_persisted = db.update_account_access_token(
                    account_id,
                    access_token=access_token,
                )
            except Exception as exc:  # noqa: BLE001 - keep the MFA result visible.
                token_persisted = False
                result["warning"] = _redacted_error(exc, item)
            result["access_token_saved"] = token_persisted
            if not token_persisted:
                result["warning"] = result.get("warning") or "access token persistence was not updated"
        result["account_id"] = account_id
        result["browser_provider"] = profile.provider
        if not bool(result.get("ok")):
            if result.get("remote_disabled"):
                return _record_remote_disable_failure(account_id, item, result)
            if is_new_account:
                return _record_local_failure(account_id, item, result)
            result["persisted"] = False
            return result

        new_secret = str(result.get("new_totp_secret") or "").strip()
        if not new_secret:
            result.update({"ok": False, "persisted": False, "error": "new TOTP secret was empty"})
            return _record_remote_disable_failure(account_id, item, result)
        try:
            persisted = db.update_account_2fa(
                account_id,
                status="active",
                totp_secret=new_secret,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - remote result must remain visible.
            persisted = False
            result["warning"] = _redacted_error(exc, item)
        result["persisted"] = persisted
        if not persisted:
            result["warning"] = result.get("warning") or "local 2FA persistence was not updated"
        return result
    except Exception as exc:  # noqa: BLE001 - isolate each account in a batch.
        result = {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "account_id": account_id,
            "error": _redacted_error(exc, item),
        }
        if is_new_account:
            return _record_local_failure(account_id, item, result)
        return result
    finally:
        if profile is not None:
            try:
                profile.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result.
                logger.debug("Browser driver cleanup failed")
            try:
                profile.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result.
                logger.debug("Browser profile cleanup failed")


def run_twofa_change_batch(
    items: list[TwofaChangeInput],
    *,
    workers: int = 1,
) -> list[dict[str, object]]:
    """Run isolated configured browser sessions concurrently in input order."""
    max_workers = max(1, min(4, int(workers or 1), len(items) or 1))
    results: list[dict[str, object] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="twofa-change") as executor:
        futures = {
            executor.submit(
                run_twofa_change,
                item,
                proxy_lane_id=index % max_workers,
            ): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [result for result in results if result is not None]
