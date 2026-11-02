"""Browser lifecycle for changing an account's ChatGPT password."""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack

from core import db, password_change_recovery
from core.account_export import BrowserPageTransport
from core.account_network import preferred_account_proxy
from core.browser_profile import configured_browser_profile_provider, open_browser_profile
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


def _run_plan_check_with_live_browser(
    account_id: int,
    item: PasswordChangeInput,
    access_token: str,
    profile,
    result: dict[str, object],
) -> None:
    """Check the plan inside the browser that just finished the password change.

    The profile is still open here: the same-origin transport reuses the
    logged-in session, so no proxy lease and no background worker are needed.
    A failure only lands in result["plan_check"]; it never undoes the password
    change that already succeeded.
    """
    if not access_token:
        result["plan_check"] = {"accepted": False, "error": "access token is missing"}
        return
    try:
        from core.plan_check_service import run_sync_plan_check

        plan_result = run_sync_plan_check(
            account_id=account_id,
            email=item.email,
            access_token=access_token,
            trigger="password_change",
            browser_transport=BrowserPageTransport(profile.driver),
        )
    except Exception as exc:  # noqa: BLE001 - plan lookup must not undo the password change.
        plan_result = {"accepted": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    result["plan_check"] = {
        "accepted": bool(plan_result.get("accepted")),
        "ok": bool(plan_result.get("ok")),
        "status": "success" if plan_result.get("ok") else "failed",
        "current_plan_type": plan_result.get("current_plan_type"),
        "plus_trial_eligible": plan_result.get("plus_trial_eligible"),
        "error": str(plan_result.get("error") or "").strip() or None,
    }
    if not plan_result.get("ok"):
        result["warning"] = result.get("warning") or str(
            plan_result.get("error") or "account plan check failed"
        )


def run_password_change(
    item: PasswordChangeInput,
    *,
    proxy_lane_id: int | None = None,
    progress_callback: Callable[[int, dict[str, object]], None] | None = None,
    progress_index: int | None = None,
    login_codex_after_change: bool = False,
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
    try:
        password_change_recovery.stage(account_id, item.new_password)
    except Exception:
        return {
            "ok": False,
            "persisted": False,
            "email": item.email,
            "account_id": account_id,
            "retryable": False,
            "error": "Không thể lưu ứng viên mật khẩu; hãy đối soát dữ liệu khôi phục cục bộ",
        }

    profile = None
    attempt_network: ExitStack | None = None
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
            mutation_started = False
            try:
                active_proxy, network_mode = attempt_network.enter_context(preferred_account_proxy(
                    None,
                    rotating_scope=PASSWORD_CHANGE_PROXY_SCOPE,
                    lane_id=proxy_lane_id,
                    lease_owner_id=f"password-change:{account_id}",
                ))
                logger.info("[PassChange] network=%s lane=%s", network_mode, proxy_lane_id or "thread")
                if active_proxy is not None and configured_browser_profile_provider() == "skyvern":
                    raise RuntimeError(
                        "Skyvern không hỗ trợ route proxy hiện tại; chưa bắt đầu đổi mật khẩu. Hãy chọn route direct rõ ràng hoặc provider hỗ trợ proxy."
                    )
                profile = (
                    open_browser_profile(proxy=active_proxy)
                    if active_proxy is not None
                    else open_browser_profile()
                )
                browser_provider = str(getattr(profile, "provider", "") or "")
                mutation_started = True
                result = change_password_in_browser(
                    profile.driver,
                    item,
                    access_token=last_access_token or stored_access_token,
                    allow_oauth_fallback=True,
                    # Keep the authenticated session alive so the plan check can
                    # reuse this browser after the password change completes.
                    keep_session=True,
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
                    **({"retryable": False, "remote_state_uncertain": True} if mutation_started else {}),
                }
            finally:
                if not bool(result.get("ok")):
                    # Failed attempts tear down immediately; a successful attempt
                    # keeps the browser and proxy lease open for the plan check.
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
                        if attempt_network is not None:
                            attempt_network.close()
                    except Exception:
                        logger.debug("Password change proxy lease cleanup failed", exc_info=True)
                    attempt_network = None

            if result.get("ok") or result.get("retryable") is False:
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
                result["warning"] = _redacted_error(str(exc).replace(access_token, "[redacted]"), item)
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
            if login_codex_after_change:
                from core.personal_info_codex import skipped_personal_info_codex

                result["codex"] = skipped_personal_info_codex(
                    item.email,
                    "Bỏ qua: thao tác đổi mật khẩu chưa thành công; trạng thái đổi mật khẩu được giữ nguyên.",
                )
            result["persisted"] = False
            result.pop("new_password", None)
            if result.get("remote_state_uncertain"):
                result["warning"] = "Cần đối soát mật khẩu bằng công cụ khôi phục cục bộ"
            else:
                password_change_recovery.discard(account_id)
            return result

        # already_set means OpenAI rejected the reset (a password already
        # exists); persisting our generated value would record a wrong secret.
        persisted = False
        if not result.get("already_set"):
            try:
                persisted = db.update_account_password(
                    account_id,
                    password=item.new_password,
                    extra_updates={"registration_password": item.new_password},
                )
            except Exception as exc:  # noqa: BLE001 - remote result must remain visible.
                persisted = False
                result["warning"] = _redacted_error(exc, item)
            if not persisted:
                result["warning"] = result.get("warning") or "local password persistence was not updated"
                result["retryable"] = False
        result["persisted"] = persisted
        result.pop("new_password", None)
        if persisted or result.get("already_set"):
            try:
                password_change_recovery.discard(account_id)
            except Exception:
                result["warning"] = "Cần dọn bản ghi khôi phục mật khẩu cục bộ"
        else:
            result["warning"] = "Cần đối soát mật khẩu bằng công cụ khôi phục cục bộ"

        # The browser is still open (keep_session=True). Check the plan in the
        # same authenticated session before closing it, so no proxy lease or
        # background worker is needed.
        if access_token and profile is not None and profile.driver is not None:
            _run_plan_check_with_live_browser(
                account_id,
                item,
                access_token,
                profile,
                result,
            )
        if login_codex_after_change and persisted and not result.get("already_set") and profile is not None:
            _notify_progress(
                progress_callback,
                progress_index,
                {"status": "running", "email": item.email, "codex": {"status": "running", "ok": False}},
            )
            try:
                from core.personal_info_codex import run_personal_info_codex_and_record

                result["codex"] = run_personal_info_codex_and_record(
                    email=item.email,
                    profile=profile,
                    active_proxy=active_proxy,
                    password=item.new_password,
                    totp_secret=item.totp_secret or "",
                )
            except Exception:
                result["retryable"] = False
                result["codex"] = {
                    "status": "failed",
                    "ok": False,
                    "reason": "Đăng nhập Codex thất bại; thao tác đổi mật khẩu vẫn thành công.",
                }
                try:
                    from core.personal_info_codex import record_personal_info_codex

                    record_personal_info_codex(item.email, result["codex"])
                except Exception:
                    result["codex"]["status_persisted"] = False
        elif login_codex_after_change:
            result["codex"] = {
                "status": "skipped",
                "ok": False,
                "reason": "Bỏ qua: mật khẩu mới chưa được áp dụng và lưu thành công",
            }
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
        if attempt_network is not None:
            try:
                attempt_network.close()
            except Exception:
                logger.debug("Password change proxy lease cleanup failed", exc_info=True)


def run_password_change_batch(
    items: list[PasswordChangeInput],
    *,
    workers: int = 1,
    progress_callback: Callable[[int, dict[str, object]], None] | None = None,
    login_codex_after_change: bool = False,
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
                login_codex_after_change=login_codex_after_change,
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
