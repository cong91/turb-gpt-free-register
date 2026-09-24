"""Browser lifecycle for changing an account's ChatGPT password."""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack

from core import db
from core.account_network import preferred_account_proxy
from core.browser_profile import open_browser_profile
from core.password_change import (
    PasswordChangeInput,
    _redacted_error,
    change_password_in_browser,
    resolve_password_change_input,
)
from core.rotating_proxy_runtime import (
    PASSWORD_CHANGE_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
)

logger = logging.getLogger(__name__)

# Same bound as the 2FA change lane: every attempt may consume a mailbox OTP
# and a proxy lease, so a browser/session failure only earns a few restarts.
_PASSWORD_CHANGE_BROWSER_RESTART_ATTEMPTS = 3


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
        logger.debug("Password change progress callback failed", exc_info=True)


def run_password_change(
    item: PasswordChangeInput,
    *,
    proxy_lane_id: int | None = None,
    progress_callback: Callable[[int, dict[str, object]], None] | None = None,
    progress_index: int | None = None,
) -> dict[str, object]:
    """Change one account's ChatGPT password and persist the new value locally."""
    _notify_progress(
        progress_callback,
        progress_index,
        {"status": "running", "detail": "Đang chuẩn bị phiên xử lý"},
    )
    try:
        item = resolve_password_change_input(item)
    except Exception as exc:  # noqa: BLE001 - isolate each account in a batch.
        return {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "error": _redacted_error(f"{type(exc).__name__}: {exc}", item),
        }
    account = db.get_account_by_email(item.email)
    if account is None:
        # Keep the row visible before the browser flow runs; the new password is
        # only persisted after the remote change succeeds.
        try:
            account_id = db.insert_account(
                email=item.email,
                access_token="",
                registration_password="",
                extra={"personal_info_change": "password_change"},
            )
        except Exception as exc:  # noqa: BLE001 - isolate each account in a batch.
            return {
                "ok": False,
                "persisted": False,
                "email": item.email,
                "error": _redacted_error(f"account save failed: {exc}", item),
            }
    else:
        account_id_value = account.get("id")
        if account_id_value is None:
            return {"ok": False, "persisted": False, "email": item.email, "error": "account id missing"}
        account_id = int(account_id_value)
    stored_access_token = str((account or {}).get("access_token") or "").strip()

    profile = None
    try:
        _notify_progress(
            progress_callback,
            progress_index,
            {"status": "running", "detail": "Đang đăng nhập và đổi mật khẩu"},
        )
        result: dict[str, object] | None = None
        browser_provider = ""
        last_error = ""
        last_access_token = ""
        for browser_attempt in range(1, _PASSWORD_CHANGE_BROWSER_RESTART_ATTEMPTS + 1):
            profile = None
            attempt_network = ExitStack()
            try:
                active_proxy, network_mode = attempt_network.enter_context(preferred_account_proxy(
                    None,
                    rotating_scope=PASSWORD_CHANGE_PROXY_SCOPE,
                    lane_id=proxy_lane_id,
                    lease_owner_id=f"password-change:{account_id}",
                ))
                logger.info("[PassChange] network=%s lane=%s", network_mode, proxy_lane_id or "thread")
                profile = (
                    open_browser_profile(proxy=active_proxy)
                    if active_proxy is not None
                    else open_browser_profile()
                )
                browser_provider = str(getattr(profile, "provider", "") or "")
                result = change_password_in_browser(
                    profile.driver,
                    item,
                    access_token=last_access_token or stored_access_token,
                    allow_oauth_fallback=True,
                )
                last_error = str(result.get("error") or "")
                last_access_token = str(result.get("access_token") or "").strip() or last_access_token
            except Exception as exc:  # noqa: BLE001 - retry browser startup/flow failures.
                last_error = _redacted_error(f"{type(exc).__name__}: {exc}", item)
                result = {
                    "ok": False,
                    "email": item.email,
                    "mode": item.mode,
                    "error": last_error,
                }
            finally:
                if profile is not None:
                    try:
                        profile.close()
                    except Exception:  # noqa: BLE001 - cleanup must not mask result.
                        logger.debug("Browser driver cleanup failed")
                    try:
                        profile.cleanup()
                    except Exception:  # noqa: BLE001 - cleanup must not mask result.
                        logger.debug("Browser profile cleanup failed")
                profile = None
                try:
                    attempt_network.close()
                except Exception:
                    logger.debug("Password change proxy lease cleanup failed", exc_info=True)

            if result.get("ok"):
                break
            if browser_attempt < _PASSWORD_CHANGE_BROWSER_RESTART_ATTEMPTS:
                logger.warning(
                    "[PassChange] password change failed; restarting profile (%s/%s): %s",
                    browser_attempt + 1,
                    _PASSWORD_CHANGE_BROWSER_RESTART_ATTEMPTS,
                    last_error[:240],
                )
        if result is None:
            result = {
                "ok": False,
                "persisted": False,
                "email": item.email,
                "error": "password change flow did not return a result",
            }
        access_token = str(result.pop("access_token", "") or "").strip()
        if access_token:
            try:
                token_persisted = db.update_account_access_token(
                    account_id,
                    access_token=access_token,
                )
            except Exception as exc:  # noqa: BLE001 - keep the change result visible.
                token_persisted = False
                result["warning"] = _redacted_error(exc, item)
            result["access_token_saved"] = token_persisted
            if not token_persisted:
                result["warning"] = result.get("warning") or "access token persistence was not updated"
                if result.get("ok"):
                    result["retryable"] = False
        else:
            token_persisted = False
        result["account_id"] = account_id
        result["browser_provider"] = browser_provider
        if not bool(result.get("ok")):
            result["persisted"] = False
            result.pop("new_password", None)
            return result

        # already_set means OpenAI rejected the reset (a password already
        # exists); persisting our generated value would record a wrong secret.
        persisted = False
        if not result.get("already_set"):
            try:
                persisted = db.update_account_password(
                    account_id,
                    password=item.new_password,
                )
            except Exception as exc:  # noqa: BLE001 - remote result must remain visible.
                persisted = False
                result["warning"] = _redacted_error(exc, item)
            if not persisted:
                result["warning"] = result.get("warning") or "local password persistence was not updated"
                result["retryable"] = False
        result["persisted"] = persisted
        result.pop("new_password", None)
        return result
    except Exception as exc:  # noqa: BLE001 - isolate each account in a batch.
        return {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "account_id": account_id,
            "error": _redacted_error(exc, item),
        }
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


def run_password_change_batch(
    items: list[PasswordChangeInput],
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
        prepare_rotating_proxy_lanes(max_workers, scope=PASSWORD_CHANGE_PROXY_SCOPE)
    results: list[dict[str, object] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="password-change") as executor:
        futures = {
            executor.submit(
                run_password_change,
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
            ) else "failed"
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
