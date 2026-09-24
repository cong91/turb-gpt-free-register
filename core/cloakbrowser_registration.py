"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core import db
from core.account_export import (
    BrowserPageTransport,
    post_register_dwell,
    save_account_data,
)
from core.browser_challenge import (
    browser_challenge_state as _browser_challenge_state,
)
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)

# 复用注册共享流程里已维护好的页面操作函数（P2/P3/P4 各 module）。
from core.browser_page_actions import (
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _email_otp_page_state,
    _page_snapshot,
    _safe_get,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.browser_traffic import SeleniumTrafficTracker
from core.cloakbrowser_driver import build_cloak_driver
from core.codex_login_credentials import CodexLoginCredentials
from core.email_provider import (
    acknowledge_verification_code,
    acquire_email_after_input,
    resolve_email_source,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay
from core.registration_flow import (
    _check_manual_stop,
    _click_if_enabled_submit,
    registration_failure_result,
    release_registration_email_on_failure,
    run_registration_page_flow,
)
from core.registration_profile_utils import (
    profile_submission_error as _profile_submission_error,
)
from core.registration_profile_utils import (
    profile_submission_failure_message as _profile_submission_failure_message,
)

logger = logging.getLogger(__name__)


def _wait_for_otp_inputs(driver, timeout: float = 45.0) -> dict:
    """Wait for an accessible OTP form, including browser challenge completion."""
    end = time.monotonic() + max(0.0, float(timeout))
    last_state: dict = {}
    while True:
        challenge_state = _browser_challenge_state(driver)
        if challenge_state.get("is_challenge"):
            remaining = end - time.monotonic()
            if remaining <= 0:
                last_state = _email_otp_page_state(driver)
                break
            _wait_for_browser_challenge(
                driver,
                timeout=min(45.0, remaining),
            )
            continue
        last_state = _email_otp_page_state(driver)
        inputs = last_state.get("inputs") or []
        for item in inputs:
            if not isinstance(item, dict):
                continue
            attrs = " ".join(
                str(item.get(key) or "")
                for key in ("type", "name", "id", "autocomplete", "inputmode")
            ).lower()
            if any(marker in attrs for marker in ("one-time", "otp", "code", "numeric", "tel")):
                return last_state
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
    raise RuntimeError(f"邮箱验证码页面未准备好，找不到 OTP 输入框: state={last_state}")


def _extract_signup_callback_url(driver) -> str | None:
    """Find a pending signup OAuth callback in the current page's navigation log."""
    try:
        entries = driver.execute_script(
            """return performance.getEntries().map(entry => ({name: entry.name}));"""
        ) or []
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(entries, list):
        return None

    for entry in entries:
        raw_url = entry.get("name") if isinstance(entry, dict) else entry
        if not isinstance(raw_url, str):
            continue
        try:
            parsed = urlsplit(raw_url)
            query = parse_qs(parsed.query)
        except ValueError:
            continue
        allowed_path = (
            parsed.hostname == "auth.openai.com"
            and parsed.path == "/authorize/continue"
        ) or (
            parsed.hostname == "chatgpt.com"
            and parsed.path == "/api/auth/callback/openai"
        )
        if allowed_path and query.get("code") and query.get("state"):
            return raw_url
    return None


def _follow_signup_callback_if_present(driver, timeout: float = 15.0) -> bool:
    """Complete a callback that was issued but not followed by the auth SPA."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        current_url = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" in current_url:
            return False
        callback_url = _extract_signup_callback_url(driver)
        if callback_url:
            parsed = urlsplit(callback_url)
            logger.info(
                "[Cloak注册] 检测到未跟随的 OAuth callback，主动完成回调：host=%s path=%s",
                parsed.hostname,
                parsed.path,
            )
            _safe_get(
                driver,
                callback_url,
                timeout=35,
                attempts=2,
                accept_hosts=("auth.openai.com", "chatgpt.com"),
            )
            return True
        time.sleep(0.5)
    return False


def _wait_for_profile_submit_transition(
    driver,
    timeout: float = 15.0,
    retry_interval: float = 4.0,
) -> bool:
    """Confirm that an about-you submit leaves the profile form before reading session."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    next_retry_at = time.monotonic() + max(0.0, float(retry_interval))
    last_snapshot: dict = {}
    profile_markers = ("about-you", "profile", "create-account/about", "signup/profile")

    while True:
        last_snapshot = _page_snapshot(driver)
        profile_error = _profile_submission_error(last_snapshot)
        if profile_error:
            raise RuntimeError(_profile_submission_failure_message(profile_error))

        current_url = str(
            last_snapshot.get("url") or getattr(driver, "current_url", "") or ""
        ).lower()
        if not any(marker in current_url for marker in profile_markers):
            logger.info(
                "[Cloak注册] 资料页提交后已确认离开 profile：url=%s",
                current_url or "-",
            )
            return True

        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_retry_at:
            if _click_if_enabled_submit(driver):
                logger.warning(
                    "[Cloak注册] 资料页提交后仍停留在 profile，重试提交：url=%s",
                    current_url or "-",
                )
            next_retry_at = now + max(0.5, float(retry_interval))

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))

    logger.warning(
        "[Cloak注册] 资料页提交后超时仍停留在 profile，继续进入 session 兜底：snapshot=%s",
        last_snapshot,
    )
    return False


def _complete_cloak_email_otp(
    driver,
    email: str,
    *,
    otp_after_ts: float,
    otp_code: str | None = None,
    otp_before_code: str | None | object = None,
) -> None:
    """Cloak lane OTP loop: chờ form OTP sẵn sàng trước khi lấy/submit mã."""
    current_otp = otp_code
    max_otp_attempts = 3
    otp_ready_timeout = max(
        45.0,
        min(90.0, float(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90)),
    )
    for otp_attempt in range(1, max_otp_attempts + 1):
        if current_otp is None:
            logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
            try:
                if driver.__class__.__name__ == "BrowserSeleniumDriver":
                    # Do not consume a mailbox OTP while Cloudflare or the
                    # verification form is still transitioning.
                    _wait_for_otp_inputs(driver, timeout=otp_ready_timeout)
                wait_kwargs = {
                    "after_ts": otp_after_ts,
                    "before_code": otp_before_code,
                    "stage": "registration_email_otp",
                }
                current_otp = wait_for_otp(email, **wait_kwargs)
            except Exception as exc:
                if otp_attempt >= max_otp_attempts:
                    raise
                logger.warning(
                    "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                    otp_attempt + 1,
                    max_otp_attempts,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                otp_after_ts = time.time()
                otp_before_code = snapshot_verification_code(
                    email,
                    stage="registration_email_resend",
                )
                _click_resend_email_otp(driver, timeout=25)
                human_delay("api")
                current_otp = None
                continue
        logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
        if driver.__class__.__name__ == "BrowserSeleniumDriver":
            _wait_for_otp_inputs(driver, timeout=otp_ready_timeout)
        _clear_otp_inputs(driver)
        _type_otp(driver, current_otp)
        human_delay("otp_input")
        try:
            _click_continue(driver)
        except Exception as exc:  # noqa: BLE001
            logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

        outcome = _wait_after_email_otp_submit(driver, timeout=10)
        if outcome == "accepted":
            acknowledge_verification_code(
                email,
                current_otp,
                stage="registration_email_otp",
            )
            return
        if otp_attempt >= max_otp_attempts:
            raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
        otp_after_ts = time.time()
        otp_before_code = snapshot_verification_code(
            email,
            stage="registration_email_resend",
        ) or current_otp
        _click_resend_email_otp(driver, timeout=25)
        human_delay("api")
        current_otp = None


def _after_cloak_profile_submit(driver) -> None:
    """Cloak lane: đợi rời profile page rồi follow signup callback nếu có."""
    _wait_for_profile_submit_transition(driver)
    _follow_signup_callback_if_present(driver)


def run_cloak_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    openai_password: str | None = None
    traffic_tracker: SeleniumTrafficTracker | None = None
    network_traffic: dict | None = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        traffic_tracker = SeleniumTrafficTracker(driver, label="Cloak")
        tunnel = getattr(proxy, "tunnel", None)
        if tunnel is not None:
            pool = getattr(tunnel, "pool", None)
            if pool is not None:
                pool.bind_profile(tunnel, opened.profile_id)
        registration_geo = (((opened.raw or {}).get("locale") or {}).get("geo") or {})
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        otp_before_code = snapshot_verification_code(email, stage="registration_email_request")
        flow_result = run_registration_page_flow(
            driver,
            email,
            name,
            birthday,
            otp_code=otp_code,
            otp_before_code=otp_before_code,
            email_supplier=_email_supplier_after_input,
            proxy=proxy,
            registration_driver="cloak",
            checkpoint_extras={
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
            },
            registration_ip=registration_geo.get("ip") or None,
            login_page_timeout=min(45, int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90)),
            login_page_attempts=max(1, int(getattr(_cfg, "CLOAK_NAVIGATION_RETRIES", 3) or 3)),
            session_auto_jump_wait=45,
            otp_completion=_complete_cloak_email_otp,
            after_profile_submit=_after_cloak_profile_submit,
            log_prefix="[Cloak注册]",
        )
        if not flow_result.get("success"):
            if traffic_tracker is not None:
                try:
                    network_traffic = traffic_tracker.stop()
                except Exception:  # noqa: BLE001, S110
                    pass
            flow_result["network_traffic"] = network_traffic
            return flow_result
        email = flow_result["email"]
        openai_password = flow_result["openai_password"]
        access_token = flow_result["access_token"]
        session_info = flow_result["session_info"]
        account_id = flow_result["account_id"]

        totp_secret = None
        twofa_status = "disabled"
        twofa_error = None
        if _twofa_cfg.ENABLE_2FA:
            from core.account_export import setup_2fa_for_registration
            try:
                # Let the fresh signup session stabilize before MFA enrollment.
                human_delay("post_auth", minimum=2.0, maximum=4.0)
                totp_secret = setup_2fa_for_registration(BrowserPageTransport(driver), email)
                twofa_status = "active"
                db.update_account_2fa(account_id, status="active", totp_secret=totp_secret)
            except Exception as exc:  # noqa: BLE001
                twofa_status = "failed"
                twofa_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                db.update_account_2fa(account_id, status="failed", error=twofa_error)
                logger.error(
                    "[Cloak注册] 2FA 设置失败，当前浏览器内 re-auth 已耗尽，准备关闭浏览器并重新执行 email OTP 登录：%s",
                    twofa_error,
                )
                try:
                    # setup_2fa() already exhausted its 3 in-session attempts.
                    # The recovery workflow owns a fresh browser for each of its
                    # three attempts and logs in with email + OTP before retrying MFA.
                    driver.quit()
                except Exception as close_exc:  # noqa: BLE001 - recovery must still be attempted.
                    logger.debug("[Cloak注册] 2FA recovery browser close failed: %s", close_exc)
                driver = None
                if traffic_tracker is not None:
                    try:
                        traffic_tracker.stop()
                    except Exception:  # noqa: BLE001, S110
                        pass
                    traffic_tracker = None
                if openai_password:
                    try:
                        from core.browser_twofa_retry import run_twofa_retry

                        recovery = run_twofa_retry(
                            {
                                "id": account_id,
                                "email": email,
                                "registration_password": openai_password,
                                "access_token": access_token,
                                "proxy_used": proxy,
                            },
                            max_attempts=3,
                            browser_restart_attempts=3,
                            proxy=proxy,
                        )
                    except Exception as recovery_exc:  # noqa: BLE001 - preserve the checkpointed account.
                        recovery = {
                            "ok": False,
                            "message": f"{type(recovery_exc).__name__}: {str(recovery_exc)[:300]}",
                        }
                    if recovery.get("ok"):
                        logger.info(
                            "[Cloak注册] 关闭浏览器后重新 email OTP 登录成功，2FA 已激活：account_id=%s",
                            recovery.get("account_id") or account_id,
                        )
                        return {
                            "success": True,
                            "email": recovery.get("email") or email,
                            "account_id": recovery.get("account_id") or account_id,
                            "access_token": recovery.get("access_token") or access_token,
                            "totp_secret": recovery.get("totp_secret"),
                            "twofa_status": "active",
                            "twofa_error": None,
                            "codex": recovery.get("codex"),
                            "error": None,
                        }
                    twofa_error = (
                        f"{twofa_error}; 浏览器重启 3 次后仍无法完成 email OTP/2FA: "
                        f"{str(recovery.get('message') or 'unknown error')[:300]}"
                    )
                try:
                    from core.registration_auto_pay153 import (
                        enqueue_registration_auto_pay153,
                    )

                    enqueue_registration_auto_pay153(
                        account_id=account_id,
                        email=email,
                        access_token=access_token,
                        proxy=proxy,
                    )
                except Exception as queue_exc:  # noqa: BLE001 - preserve the checkpointed account.
                    logger.warning(
                        "[PAY.153][Cloak注册] 2FA 失败后的自动任务未入队: %s: %s",
                        type(queue_exc).__name__,
                        str(queue_exc)[:180],
                    )
                logger.error("[Cloak注册] 2FA 设置失败，账号已保留待重试：%s", twofa_error)
                return {"success": False, "email": email, "account_id": account_id, "access_token": access_token, "twofa_status": twofa_status, "twofa_error": twofa_error, "error": f"2FA 设置失败，账号已保存：{twofa_error}"}

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        free_codex_auto_enabled = False
        try:
            from config import codex as _codex_cfg
            from config import register as _register_cfg
            from core.codex_oauth import run_codex_oauth
            codex_auto_enabled = bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False))
            free_codex_auto_enabled = bool(
                getattr(_register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
            )
            codex_credentials = None
            if openai_password and totp_secret:
                codex_credentials = CodexLoginCredentials(
                    email=email,
                    password=openai_password,
                    totp_secret=totp_secret,
                )

            def _run_codex_in_current_browser() -> dict:
                login_mode = (
                    "密码 + authenticator TOTP"
                    if codex_credentials
                    else "邮箱 OTP fallback（注册密码或 TOTP 不完整）"
                )
                logger.info(
                    "[Cloak注册][Codex] 复用当前 CloakBrowser 窗口执行 Codex 授权，登录方式=%s",
                    login_mode,
                )
                _check_manual_stop()
                return run_codex_oauth(
                    email,
                    oauth_driver="cloak",
                    force=True,
                    credentials=codex_credentials,
                    existing_driver=driver,
                    existing_opened=opened,
                )

            post_auth_automation_enabled = bool(
                getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
                or free_codex_auto_enabled
                or bool(getattr(_register_cfg, "AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", False))
                or codex_auto_enabled
            )
            if post_auth_automation_enabled:
                from core.registration_auto_codex import run_registration_auto_codex

                plan_proxy = ((opened.raw or {}).get("proxy") if opened else None) or proxy or None
                auto_codex = run_registration_auto_codex(
                    account_id=account_id,
                    email=email,
                    access_token=access_token,
                    proxy=plan_proxy,
                    browser_transport=BrowserPageTransport(driver),
                    run_codex=_run_codex_in_current_browser,
                    twofa_status=twofa_status,
                )
                codex_result = auto_codex["codex"]
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:  # noqa: BLE001
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Cloak注册")
        if traffic_tracker is not None:
            network_traffic = traffic_tracker.stop()
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            registration_ip=registration_geo.get("ip") or None,
            batch_dir=batch_dir,
            auto_plan_check=False,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "registration_driver": "cloak",
                "twofa_status": twofa_status,
                "twofa_error": twofa_error,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "twofa_status": twofa_status, "twofa_error": twofa_error, "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        # Flow dùng chung đã release email cho lỗi xảy ra bên trong nó; ở đây chỉ
        # cần release cho lỗi trước/sau flow (build driver, 2FA, Codex, save).
        release_registration_email_on_failure(
            exc,
            email,
            create_acknowledged=True,
            log_prefix="[Cloak注册]",
        )
        return registration_failure_result(exc, email, extras={"network_traffic": network_traffic})
    finally:
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:  # noqa: BLE001, S110
                pass
