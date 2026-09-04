"""Shared Selenium-compatible login helpers for existing ChatGPT accounts."""
from __future__ import annotations

import logging
import time

from config import twofa as _twofa_cfg
from core.browser_registration import (
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _fetch_chatgpt_session,
    _has_access_token,
    _is_email_verification_page,
    _maybe_accept,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
    _wait_after_password_submit,
)
from core.email_provider import (
    acknowledge_verification_code,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)


def _login_password(driver, password: str, timeout: int = 30) -> None:
    """Fill and submit the existing-account password page."""
    end = time.time() + timeout
    last_state = None
    while time.time() < end:
        last_state = str(getattr(driver, "current_url", "") or "")
        result = driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            const input = [...document.querySelectorAll('input[type="password"], input[name*="password" i]')].find(visible);
            if (!input) return {ok:false, reason:'missing_password_input'};
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(input, String(arguments[0])); else input.value = String(arguments[0]);
            input.dispatchEvent(new Event('input', {bubbles:true}));
            input.dispatchEvent(new Event('change', {bubbles:true}));
            const scope = input.closest('form') || document;
            const submit = [...scope.querySelectorAll('button[type="submit"], input[type="submit"], button')]
              .find(el => visible(el));
            if (!submit) return {ok:false, reason:'missing_submit'};
            submit.click();
            return {ok:true};
            """,
            password,
        ) or {}
        if result.get("ok"):
            logger.info("[Browser 2FA] 已提交已有账号密码")
            _wait_after_password_submit(driver, last_state, timeout=min(timeout, 5))
            return
        time.sleep(0.5)
    raise RuntimeError(f"登录密码页处理超时：url={last_state}")


def _login_existing_account(driver, email: str, password: str, timeout: int = 120) -> dict:
    """Use email, password, and email OTP to establish an existing login session."""
    driver.get("https://chatgpt.com/auth/login")
    human_delay("navigate")
    _maybe_accept(driver)
    if _has_access_token(driver):
        logger.info("[Browser 2FA] profile vẫn còn session đăng nhập, bỏ qua email login OTP")
        session_info = _fetch_chatgpt_session(driver, timeout=timeout)
        if not session_info.get("accessToken"):
            raise RuntimeError("已有账号登录成功但未拿到 accessToken")
        return session_info
    otp_before_code = snapshot_verification_code(
        email,
        stage="twofa_login_email_request",
    )
    next_state = _submit_email_and_wait_next(
        driver,
        email,
        attempts=3,
        allow_login_password=True,
    )
    if next_state in ("login_password", "password"):
        _login_password(driver, password)
    if next_state == "logged_in":
        session_info = _fetch_chatgpt_session(driver, timeout=timeout)
        if not session_info.get("accessToken"):
            raise RuntimeError("已有账号登录成功但未拿到 accessToken")
        return session_info

    otp_after_ts = time.time()
    current_otp = None
    previous_submitted_otp = None
    for attempt in range(1, 4):
        if current_otp is None:
            wait_kwargs = {
                "after_ts": otp_after_ts,
                "before_code": otp_before_code,
                "max_wait": int(getattr(_twofa_cfg, "TWOFA_OTP_MAX_WAIT", 90) or 90),
                "stage": "twofa_login_email_otp",
            }
            if previous_submitted_otp:
                wait_kwargs["before_code"] = previous_submitted_otp
            current_otp = wait_for_otp(email, **wait_kwargs)
        _clear_otp_inputs(driver)
        _type_otp(driver, current_otp)
        try:
            _click_continue(driver)
        except Exception as exc:  # noqa: BLE001 - page state polling is authoritative.
            logger.debug("[Browser 2FA] email OTP submit click unavailable: %s", exc)
        outcome = _wait_after_email_otp_submit(driver, timeout=15)
        if outcome != "accepted" and not _is_email_verification_page(driver):
            logger.info(
                "[Browser 2FA] OTP 提交后页面已离开验证码页，跳过 resend 并继续读取登录态"
            )
            outcome = "accepted"
        if outcome == "accepted":
            acknowledge_verification_code(
                email,
                current_otp,
                stage="twofa_login_email_otp",
            )
            break
        if attempt >= 3:
            raise RuntimeError("已有账号登录邮箱验证码连续失败")
        otp_before_code = snapshot_verification_code(
            email,
            stage="twofa_login_email_resend",
        ) or current_otp
        _click_resend_email_otp(driver, timeout=25)
        human_delay("api")
        otp_after_ts = time.time()
        previous_submitted_otp = current_otp
        current_otp = None
        logger.warning("[Browser 2FA] 已有账号登录 OTP 失败，准备重新获取：%s/3", attempt + 1)

    session_info = _fetch_chatgpt_session(driver, timeout=timeout)
    if not session_info.get("accessToken"):
        raise RuntimeError("已有账号登录成功但未拿到 accessToken")
    return session_info
