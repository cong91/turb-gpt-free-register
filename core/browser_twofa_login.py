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
    _human_click,
    _human_type_text,
    _is_email_verification_page,
    _is_login_password_page,
    _maybe_accept,
    _raise_if_account_unusable,
    _submit_email_and_wait_next,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.email_provider import (
    acknowledge_verification_code,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)


def _find_login_password_controls(
    driver,
    *,
    require_enabled_submit: bool = False,
) -> dict:
    """Find the live password input and its submit control."""
    return driver.execute_script(
        """
        const displayed = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const visible = el => displayed(el) && !el.disabled && !el.readOnly;
        const enabled = el => visible(el) && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const input = [...document.querySelectorAll('input[type="password"], input[name*="password" i], input[autocomplete="current-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const candidates = [...scope.querySelectorAll('button[type="submit"], input[type="submit"], button, [role="button"]')]
          .filter(el => displayed(el) && (!arguments[0] || enabled(el)))
          .filter(el => {
            const attrs = [el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('aria-label'),
              el.getAttribute('data-dd-action-name'), el.textContent].join(' ').toLowerCase();
            return !/back|cancel|forgot|help|\u8fd4\u56de|\u53d6\u6d88|\u5fd8\u8bb0|\u5e2e助/.test(attrs);
          });
        if (!candidates.length) return {ok:false, reason:'missing_enabled_submit'};
        const scored = candidates.map((el, index) => {
          const attrs = [
            el.getAttribute('type'), el.getAttribute('name'), el.getAttribute('value'),
            el.getAttribute('aria-label'), el.getAttribute('data-dd-action-name'), el.textContent
          ].join(' ').toLowerCase();
          let score = index;
          if ((el.getAttribute('type') || '').toLowerCase() === 'submit') score -= 100;
          if (/continue|next|sign.?in|login|submit|\u7ee7\u7eed|\u767b\u5f55/.test(attrs)) score -= 50;
          if (/back|cancel|forgot|\u8fd4\u56de|\u53d6\u6d88/.test(attrs)) score += 100;
          return {el, score};
        }).sort((a, b) => a.score - b.score);
        const target = scored[0].el;
        target.scrollIntoView({block:'center'});
        return {
          ok:true,
          input,
          button:target,
          type:target.getAttribute('type') || '',
          text:(target.textContent || target.getAttribute('value') || '').trim().slice(0, 80)
        };
        """,
        require_enabled_submit,
    ) or {}


def _request_login_password_submit(driver) -> bool:
    """Trigger the form's native submit path after an unresponsive click."""
    result = driver.execute_script(
        """
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"], input[name*="password" i], input[autocomplete="current-password"]')]
          .find(visible);
        const form = input?.closest('form');
        if (!form) return false;
        const submit = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .find(el => visible(el) && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true');
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit(submit);
          return true;
        }
        if (submit) {
          submit.click();
          return true;
        }
        return false;
        """
    )
    return bool(result)


def _password_submit_state(driver) -> str:
    """Classify the page after submitting an existing-account password."""
    if _is_email_verification_page(driver):
        return "otp"
    if _has_access_token(driver, timeout_ms=1000):
        return "logged_in"
    if _is_login_password_page(driver):
        return "login_password"
    return "next"


def _wait_for_password_submit_state(driver, timeout: float = 10.0) -> str:
    """Wait until password submission leaves the password page."""
    end = time.monotonic() + max(0.0, float(timeout))
    state = "login_password"
    while time.monotonic() < end:
        state = _password_submit_state(driver)
        if state != "login_password":
            return state
        time.sleep(0.25)
    return state


def _login_password(driver, password: str, timeout: int = 30) -> str:
    """Fill and submit the existing-account password page."""
    end = time.time() + timeout
    last_detail = None
    while time.time() < end:
        _raise_if_account_unusable(driver)
        result = _find_login_password_controls(driver)
        last_detail = result
        if result.get("ok"):
            _human_type_text(driver, result["input"], password, clear=True)
            # Let React commit the controlled input and enable Continue before clicking.
            human_delay("form", minimum=2.0, maximum=3.6)
            submit_result = _find_login_password_controls(
                driver,
                require_enabled_submit=True,
            )
            last_detail = submit_result
            if not submit_result.get("ok"):
                time.sleep(0.5)
                continue
            initial_url = str(getattr(driver, "current_url", "") or "")
            _human_click(driver, submit_result["button"], label="password_submit")
            logger.info(
                "[Browser 2FA] 已点击已有账号密码 Continue：detail=%s",
                {k: v for k, v in submit_result.items() if k not in {"input", "button"}},
            )
            state = _wait_for_password_submit_state(driver, timeout=min(10.0, max(0.0, end - time.time())))
            if state != "login_password":
                return state

            logger.warning(
                "[Browser 2FA] 密码 Continue 点击后仍停留密码页，改用原生 form submit：url=%s",
                initial_url,
            )
            if _request_login_password_submit(driver):
                state = _wait_for_password_submit_state(driver, timeout=min(8.0, max(0.0, end - time.time())))
                if state != "login_password":
                    return state
            raise RuntimeError(
                "登录密码提交后仍停留在密码页，未进入邮箱验证码页"
                f"：url={getattr(driver, 'current_url', '') or initial_url} detail={last_detail}"
            )
        time.sleep(0.5)
    raise RuntimeError(
        "登录密码页处理超时："
        f"url={getattr(driver, 'current_url', '')} detail={last_detail}"
    )


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
        password_state = _login_password(driver, password)
        if password_state == "logged_in":
            session_info = _fetch_chatgpt_session(driver, timeout=timeout)
            if not session_info.get("accessToken"):
                raise RuntimeError("已有账号登录成功但未拿到 accessToken")
            return session_info
        if password_state != "otp" and not _is_email_verification_page(driver):
            raise RuntimeError(
                "登录密码提交后未进入邮箱验证码页"
                f"：state={password_state} url={getattr(driver, 'current_url', '')}"
            )
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
