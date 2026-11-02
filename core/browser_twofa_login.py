"""Shared Selenium-compatible login helpers for existing ChatGPT accounts."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from config import twofa as _twofa_cfg
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
from core.browser_failure_policy import (
    raise_if_account_unusable as _raise_if_account_unusable,
)
from core.browser_page_actions import (
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _has_access_token,
    _human_click,
    _human_type_text,
    _is_email_verification_page,
    _is_login_password_page,
    _maybe_accept,
    _page_warmup,
    _safe_get,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.email_provider import (
    acknowledge_verification_code,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay
from core.registration_flow import (
    _fetch_chatgpt_session,
    _submit_email_and_wait_next,
)

logger = logging.getLogger(__name__)

_WORKSPACE_CHOOSER_PATH = "/workspace"
_WORKSPACE_CHOOSER_TIMEOUT = 30
# The chooser entry for the personal workspace is labelled in the account's own
# language.  The account's existing MFA state belongs to that workspace, so the
# team entries must not be picked when a personal one exists.
_PERSONAL_WORKSPACE_MARKERS = (
    "個人アカウント",
    "个人账户",
    "个人帐户",
    "个人账号",
    "個人帳戶",
    "個人帳號",
    "personal account",
    "personal workspace",
    "personal",
)


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
        session_info = _fetch_session_after_workspace(driver, timeout=timeout)
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


def _on_about_you_page(driver) -> bool:
    """Whether the auth flow currently sits on the about-you/profile page."""
    url = str(getattr(driver, "current_url", "") or "").lower()
    return any(
        marker in url
        for marker in ("about-you", "profile", "signup/profile", "create-account/about")
    )


def _on_workspace_chooser(driver) -> bool:
    """Whether the auth flow currently sits on the workspace chooser."""
    try:
        url = str(getattr(driver, "current_url", "") or "").lower()
    except Exception:  # noqa: BLE001 - page probe must not mask the login result.
        url = ""
    path = urlsplit(url).path.rstrip("/")
    return path == _WORKSPACE_CHOOSER_PATH


def _select_personal_workspace(driver) -> bool:
    """Pick the personal workspace entry so the session can be issued.

    Multi-workspace accounts stop at ``/workspace`` after a successful password
    and TOTP login.  The chooser lists one button per workspace; the MFA factor
    being changed belongs to the personal workspace, so team entries are only
    used when no personal entry exists.  Returns whether a workspace was picked.
    """
    try:
        result = driver.execute_script(
            r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const markers = arguments[0] || [];
            const buttons = [...document.querySelectorAll('button[name="workspace_id"], form[action$="/workspace"] button')]
              .filter(visible);
            if (!buttons.length) return {ok: false, reason: 'no_workspace_entries'};
            const label = el => norm([
              el.textContent,
              el.getAttribute('aria-label'),
              el.querySelector('[title]')?.getAttribute('title'),
            ].join(' '));
            let target = buttons.find(el => markers.some(marker => label(el).includes(marker)));
            if (!target) {
              if (buttons.length !== 1) return {ok: false, reason: 'no_personal_entry', count: buttons.length};
              target = buttons[0];
            }
            target.scrollIntoView({block: 'center'});
            return {ok: true, value: target.getAttribute('value') || '', text: norm(target.textContent).slice(0, 80)};
            """,
            list(_PERSONAL_WORKSPACE_MARKERS),
        ) or {}
    except Exception as exc:  # noqa: BLE001 - the caller retries the whole login.
        logger.warning("[Browser 2FA] 工作区选择页探测失败: %s: %s", type(exc).__name__, exc)
        return False
    if not result.get("ok"):
        logger.warning(
            "[Browser 2FA] 工作区选择页未找到个人工作区: %s",
            {k: v for k, v in result.items() if k != "ok"},
        )
        return False
    try:
        from selenium.webdriver.common.by import By

        for element in driver.find_elements(By.CSS_SELECTOR, "button[name='workspace_id']"):
            value = str(element.get_attribute("value") or "")
            if value and value == str(result.get("value") or ""):
                _human_click(driver, element, label="workspace_select")
                logger.info("[Browser 2FA] 已选择个人工作区: %s", result.get("text"))
                return True
    except Exception as exc:  # noqa: BLE001 - fall back to the JS click below.
        logger.debug("[Browser 2FA] 工作区按钮人工化点击失败，回退脚本点击: %s", exc)
    try:
        clicked = driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const value = String(arguments[0] || '');
            const target = [...document.querySelectorAll('button[name="workspace_id"]')]
              .filter(visible)
              .find(el => (el.getAttribute('value') || '') === value);
            if (!target) return false;
            target.click();
            return true;
            """,
            result.get("value"),
        )
    except Exception as exc:  # noqa: BLE001 - the caller retries the whole login.
        logger.warning("[Browser 2FA] 工作区按钮脚本点击失败: %s: %s", type(exc).__name__, exc)
        return False
    if clicked:
        logger.info("[Browser 2FA] 已选择个人工作区: %s", result.get("text"))
    return bool(clicked)


def _complete_workspace_chooser(driver, timeout: int = _WORKSPACE_CHOOSER_TIMEOUT) -> bool:
    """Leave the workspace chooser by selecting the personal workspace."""
    end = time.monotonic() + max(1.0, float(timeout))
    attempted = False
    while time.monotonic() < end:
        if not _on_workspace_chooser(driver):
            return attempted
        if not attempted:
            attempted = _select_personal_workspace(driver)
        time.sleep(0.5)
    logger.warning("[Browser 2FA] 工作区选择页等待超时，仍未离开: url=%s", getattr(driver, "current_url", ""))
    return attempted


def _fetch_session_after_workspace(driver, *, timeout: int) -> dict:
    """Read the ChatGPT session, resolving the workspace chooser first.

    Multi-workspace accounts stop at ``/workspace`` right after the password and
    authenticator login; until a workspace is selected ``/api/auth/session``
    returns no accessToken, so the session read must not run before the chooser
    is answered.
    """
    if _on_workspace_chooser(driver):
        _complete_workspace_chooser(driver)
    return _fetch_chatgpt_session(driver, timeout=timeout)


def _login_existing_account(
    driver,
    email: str,
    password: str,
    timeout: int = 120,
    *,
    totp_secret: str | None = None,
    profile: tuple[str, str] | None = None,
) -> dict:
    """Use email, password, and the supplied authenticator TOTP to log in.

    ``profile`` là (name, birthday) dùng khi OpenAI đưa account đã đăng ký
    quay lại trang about-you để hoàn tất profile trước khi cấp session.
    """
    current_totp_secret = str(totp_secret or "").strip()
    # Auth pages may render slowly after navigation; warm the page, wait for
    # challenge completion, then locate the email control through the shared port.
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
        # A reused browser may restore an authenticated application page after
        # navigating to login; clear that stale session before entering credentials.
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
        session_info = _fetch_session_after_workspace(driver, timeout=timeout)
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
            session_info = _fetch_session_after_workspace(driver, timeout=timeout)
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
        if password_state == "next" and _on_workspace_chooser(driver):
            # Multi-workspace accounts can land on the chooser straight after
            # the password step; answer it before reading the session.
            session_info = _fetch_session_after_workspace(driver, timeout=timeout)
            if not session_info.get("accessToken"):
                raise RuntimeError("已有账号登录成功但未拿到 accessToken")
            return session_info
        if password_state == "next" and profile and _on_about_you_page(driver):
            # Account đã qua about-you ở lần đăng ký nhưng OpenAI chưa finalize
            # profile phía server: login lại sẽ bị đẩy về about-you — điền nốt
            # form (dùng name/birthday đã checkpoint) rồi mới lấy được session.
            from core.registration_flow import _complete_profile_page

            logger.info(
                "[Browser 2FA] login bị đưa về about-you; điền nốt profile (name=%s)",
                profile[0],
            )
            if _complete_profile_page(driver, profile[0], profile[1], timeout=60):
                session_info = _fetch_session_after_workspace(driver, timeout=timeout)
                if not session_info.get("accessToken"):
                    raise RuntimeError("已有账号登录成功但未拿到 accessToken")
                return session_info
        if password_state != "otp" and not _is_email_verification_page(driver):
            raise RuntimeError(
                "登录密码提交后未进入邮箱验证码页"
                f"：state={password_state} url={getattr(driver, 'current_url', '')}"
            )
    if next_state == "logged_in":
        session_info = _fetch_session_after_workspace(driver, timeout=timeout)
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

    session_info = _fetch_session_after_workspace(driver, timeout=timeout)
    if not session_info.get("accessToken"):
        raise RuntimeError("已有账号登录成功但未拿到 accessToken")
    return session_info
