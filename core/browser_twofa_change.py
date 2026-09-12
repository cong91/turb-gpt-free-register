"""Browser lifecycle for replacing an existing ChatGPT TOTP factor."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack

from core import db
from core.account_network import preferred_account_proxy
from core.account_security import (
    TwofaChangeInput,
    change_twofa_in_browser,
)
from core.browser_profile import open_browser_profile
from core.rotating_proxy_runtime import (
    TWOFA_CHANGE_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
)

logger = logging.getLogger(__name__)

_TWOFA_CHANGE_BROWSER_RESTART_ATTEMPTS = 1


def _notify_progress(
    callback: Callable[[int, dict[str, object]], None] | None,
    index: int | None,
    update: dict[str, object],
) -> None:
    if callback is None or index is None:
        return
    try:
        callback(index, dict(update))
    except Exception:
        logger.debug("2FA progress callback failed", exc_info=True)


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


def _queue_new_account_plan_check(
    account_id: int,
    item: TwofaChangeInput,
    access_token: str,
    result: dict[str, object],
) -> None:
    """Queue plan detection after a new account has a usable session token."""
    if not access_token:
        result["plan_check"] = {"accepted": False, "error": "access token is missing"}
        return
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=account_id,
            email=item.email,
            access_token=access_token,
            trigger="twofa_change",
            proxy=None,
            timezone_offset_min="-",
        )
        result["plan_check"] = {
            "accepted": bool(queued.get("accepted")),
            "status": str(queued.get("status") or "").strip() or None,
            "error": str(queued.get("error") or "").strip() or None,
        }
        if not queued.get("accepted"):
            result["warning"] = result.get("warning") or str(
                queued.get("error") or "account plan check was not queued"
            )
    except Exception as exc:  # noqa: BLE001 - plan lookup must not undo a saved MFA change.
        result["plan_check"] = {"accepted": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        result["warning"] = result.get("warning") or "account plan check could not be queued"


def run_twofa_change(
    item: TwofaChangeInput,
    *,
    proxy_lane_id: int | None = None,
    progress_callback: Callable[[int, dict[str, object]], None] | None = None,
    progress_index: int | None = None,
) -> dict[str, object]:
    """Replace one account's TOTP and upsert its local persistence row."""
    _notify_progress(
        progress_callback,
        progress_index,
        {"status": "running", "detail": "Đang chuẩn bị phiên xử lý"},
    )
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
    stored_access_token = str((account or {}).get("access_token") or "").strip()

    profile = None
    network_stack = ExitStack()
    try:
        active_proxy, network_mode = network_stack.enter_context(preferred_account_proxy(
            None,
            rotating_scope=TWOFA_CHANGE_PROXY_SCOPE,
            lane_id=proxy_lane_id,
            lease_owner_id=f"twofa-change:{account_id}",
        ))
        logger.info("[2FA] network=%s lane=%s", network_mode, proxy_lane_id or "thread")
        _notify_progress(
            progress_callback,
            progress_index,
            {"status": "running", "detail": "Đang đăng nhập và đổi 2FA"},
        )
        result: dict[str, object] | None = None
        browser_provider = ""
        last_error = ""
        last_access_token = ""
        for browser_attempt in range(1, _TWOFA_CHANGE_BROWSER_RESTART_ATTEMPTS + 1):
            profile = (
                open_browser_profile(proxy=active_proxy)
                if active_proxy is not None
                else open_browser_profile()
            )
            browser_provider = str(getattr(profile, "provider", "") or "")
            try:
                # Try one stored token, then one credential login in this same
                # profile. The security layer owns that fallback boundary.
                result = change_twofa_in_browser(
                    profile.driver,
                    item,
                    proxy=active_proxy,
                    access_token=stored_access_token,
                    allow_oauth_fallback=True,
                )
                last_error = str(result.get("error") or "")
                last_access_token = str(result.get("access_token") or "").strip() or last_access_token
            finally:
                try:
                    profile.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask result.
                    logger.debug("Browser driver cleanup failed")
                try:
                    profile.cleanup()
                except Exception:  # noqa: BLE001 - cleanup must not mask result.
                    logger.debug("Browser profile cleanup failed")
                profile = None

            if result.get("ok") or result.get("remote_disabled"):
                break
            if browser_attempt < _TWOFA_CHANGE_BROWSER_RESTART_ATTEMPTS:
                logger.warning(
                    "[2FA] browser login/change failed; restarting profile (%s/%s): %s",
                    browser_attempt + 1,
                    _TWOFA_CHANGE_BROWSER_RESTART_ATTEMPTS,
                    last_error[:240],
                )
        if result is None:
            result = {
                "ok": False,
                "persisted": False,
                "email": item.email,
                "error": "2FA browser flow did not return a result",
            }
        if last_access_token and not result.get("access_token"):
            result["access_token"] = last_access_token
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
        else:
            token_persisted = False
        result["account_id"] = account_id
        result["browser_provider"] = browser_provider
        if is_new_account and token_persisted:
            _queue_new_account_plan_check(account_id, item, access_token, result)
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
                registration_password=item.password,
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
        network_stack.close()


def run_twofa_change_batch(
    items: list[TwofaChangeInput],
    *,
    workers: int = 1,
    progress_callback: Callable[[int, dict[str, object]], None] | None = None,
) -> list[dict[str, object]]:
    """Run isolated configured browser sessions concurrently in input order."""
    max_workers = max(1, min(4, int(workers or 1), len(items) or 1))
    for index, item in enumerate(items):
        _notify_progress(
            progress_callback,
            index,
            {"status": "queued", "email": item.email, "detail": "Đang chờ luồng xử lý"},
        )
    if items:
        prepare_rotating_proxy_lanes(max_workers, scope=TWOFA_CHANGE_PROXY_SCOPE)
    results: list[dict[str, object] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="twofa-change") as executor:
        futures = {
            executor.submit(
                run_twofa_change,
                item,
                proxy_lane_id=index % max_workers,
                progress_callback=progress_callback,
                progress_index=index,
            ): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate unexpected worker failures.
                item = items[index]
                result = {
                    "ok": False,
                    "persisted": False,
                    "email": item.email,
                    "error": _redacted_error(f"{type(exc).__name__}: {exc}", item),
                }
            results[index] = result
            status = "success" if (
                result.get("ok")
                and result.get("persisted", True)
                and result.get("access_token_saved", True)
            ) else (
                "partial_failure" if result.get("remote_disabled") else "failed"
            )
            _notify_progress(
                progress_callback,
                index,
                {
                    "status": status,
                    "email": items[index].email,
                    "detail": result.get("error") or result.get("warning") or "Đã hoàn tất xử lý",
                    "result": result,
                },
            )
    return [result for result in results if result is not None]
