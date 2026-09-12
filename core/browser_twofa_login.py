"""Shared Selenium-compatible login helpers for existing ChatGPT accounts."""
from __future__ import annotations

import logging
import time

from config import twofa as _twofa_cfg
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
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
    _page_warmup,
    _raise_if_account_unusable,
    _safe_get,
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


def _clear_stale_browser_auth_state(driver) -> None:
    """Clear provider-owned auth storage before reusing a stale browser shell."""
    delete_cookies = getattr(driver, "delete_all_cookies", None)
    if callable(delete_cookies):
        try:
            delete_cookies()
        except Exception as exc:  # noqa: BLE001 - storage cleanup is best effort.
            logger.debug("[Browser 2FA] cookie cleanup failed: %s", exc)
    context = getattr(driver, "context", None)
    clear_cookies = getattr(context, "clear_cookies", None)
    if callable(clear_cookies):
        try:
            clear_cookies()
        except Exception as exc:  # noqa: BLE001 - storage cleanup is best effort.
            logger.debug("[Browser 2FA] Playwright cookie cleanup failed: %s", exc)
    execute_script = getattr(driver, "execute_script", None)
    if callable(execute_script):
        try:
            execute_script("window.localStorage?.clear(); window.sessionStorage?.clear();")
        except Exception as exc:  # noqa: BLE001 - storage cleanup is best effort.
            logger.debug("[Browser 2FA] web storage cleanup failed: %s", exc)


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
    if _has_access_token(driver, timeout_ms=1000):
        return "logged_in"
    try:
        from core.browser_credential_login import classify_login_state

        authenticator_state = classify_login_state(driver)
        if authenticator_state in {"totp", "totp_invalid"}:
            return authenticator_state
    except Exception:
        logger.debug("[Browser 2FA] authenticator state probe failed", exc_info=True)
    if _is_email_verification_page(driver):
        return "otp"
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


def _submit_existing_account_totp(driver, totp_secret: str, timeout: int = 20) -> str:
    """Submit the current authenticator code when account login asks for it."""
    from core.browser_credential_login import classify_login_state
    from core.codex_login_credentials import generate_totp_code

    secret = str(totp_secret or "").strip()
    if not secret:
        raise RuntimeError("已有账号登录需要 current TOTP secret")

    previous_code = None
    end = time.monotonic() + max(1.0, float(timeout))
    for attempt in range(2):
        code = generate_totp_code(secret, previous_code=previous_code)
        previous_code = code
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        human_delay("otp_input")
        _click_continue(driver)
        while time.monotonic() < end:
            if _has_access_token(driver, timeout_ms=1000):
                return "logged_in"
            state = classify_login_state(driver)
            if state == "totp_invalid":
                if attempt == 0:
                    break
                return state
            if state == "totp" or (
                state in {"email_otp", "unknown"}
                and _is_email_verification_page(driver)
            ):
                # OpenAI can render the authenticator form with only a generic
                # one-time-code input and no `/mfa` marker. Keep waiting for
                # the supplied TOTP submission instead of classifying it as
                # a mailbox OTP flow.
                time.sleep(0.25)
                continue
            if state != "totp":
                return state
            time.sleep(0.25)
        if attempt == 0:
            end = time.monotonic() + max(1.0, float(timeout))
    return "totp_invalid"


def _finish_existing_account_totp(
    driver,
    email: str,
    timeout: int,
    totp_secret: str,
) -> dict:
    """Submit the supplied factor and fetch the authenticated browser session."""
    state = _submit_existing_account_totp(driver, totp_secret)
    if state in {"logged_in", "accepted", "unknown"}:
        session_info = _fetch_chatgpt_session(driver, timeout=timeout)
        if not session_info.get("accessToken"):
            if state == "unknown":
                raise RuntimeError("已有账号登录 authenticator TOTP 后未建立 session")
            raise RuntimeError("已有账号登录成功但未拿到 accessToken")
        return session_info
    if state == "totp_invalid":
        raise RuntimeError("已有账号登录 authenticator TOTP 连续失败")
    raise RuntimeError(
        "已有账号登录 authenticator TOTP 未完成"
        f"：state={state} email={email}"
    )


def _login_existing_account(
    driver,
    email: str,
    password: str,
    timeout: int = 120,
    *,
    totp_secret: str | None = None,
) -> dict:
    """Use email, password, and the supplied authenticator TOTP to log in."""
    current_totp_secret = str(totp_secret or "").strip()
    # Match the registration flow: tolerate renderer/navigation hiccups, give
    # the auth SPA a short warm-up, and wait for any browser challenge before
    # looking for the email control.  A direct one-shot ``driver.get`` often
    # leaves Cloak with an empty DOM, which then incorrectly falls through to
    # the HTTP OAuth path and its proxy-sensitive CSRF request.
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=min(45, max(1, int(timeout))),
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    human_delay("navigate")
    _page_warmup(driver, reason="twofa_login_page")
    _maybe_accept(driver)
    _wait_for_browser_challenge(driver, timeout=min(45, max(1, int(timeout))))
    current_url = str(getattr(driver, "current_url", "") or "").lower()
    if "chatgpt.com" in current_url and "/auth/login" not in current_url:
        # Cloak profiles can restore a previous ChatGPT page even after a
        # navigation to /auth/login.  Do not try to type the new account into
        # the application shell; clear that stale browser session first.
        logger.info("[Browser 2FA] detected stale ChatGPT page; logging out before credential login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/logout",
            timeout=min(30, max(1, int(timeout))),
            attempts=1,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        _clear_stale_browser_auth_state(driver)
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, max(1, int(timeout))),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="twofa_login_after_logout")
        _maybe_accept(driver)
        _wait_for_browser_challenge(driver, timeout=min(45, max(1, int(timeout))))
    if _has_access_token(driver):
        logger.info("[Browser 2FA] profile vẫn còn session đăng nhập, bỏ qua email login OTP")
        session_info = _fetch_chatgpt_session(driver, timeout=timeout)
        if not session_info.get("accessToken"):
            raise RuntimeError("已有账号登录成功但未拿到 accessToken")
        return session_info
    otp_before_code = None
    if not current_totp_secret:
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
    if current_totp_secret and next_state == "otp":
        # Existing accounts with a supplied factor must never consume a
        # mailbox code.  Auth pages sometimes expose only a generic
        # `one-time-code` input, so use the caller-provided TOTP directly.
        next_state = "totp"
    if next_state in {"totp", "totp_invalid"}:
        return _finish_existing_account_totp(
            driver,
            email,
            timeout,
            current_totp_secret,
        )
    if next_state in ("login_password", "password"):
        password_state = _login_password(driver, password)
        if password_state == "logged_in":
            session_info = _fetch_chatgpt_session(driver, timeout=timeout)
            if not session_info.get("accessToken"):
                raise RuntimeError("已有账号登录成功但未拿到 accessToken")
            return session_info
        if current_totp_secret and password_state == "otp":
            password_state = "totp"
        if current_totp_secret and password_state == "next" and _is_email_verification_page(driver):
            password_state = "totp"
        if password_state in {"totp", "totp_invalid"}:
            return _finish_existing_account_totp(
                driver,
                email,
                timeout,
                current_totp_secret,
            )
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

    if current_totp_secret:
        raise RuntimeError(
            "已有账号登录未进入 authenticator TOTP 页面；拒绝改用邮箱 OTP"
        )

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
