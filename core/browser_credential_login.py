"""Local Selenium password/TOTP login adapter for Codex OAuth."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from core.browser_registration import (
    _clear_otp_inputs,
    _click_continue,
    _human_type_text,
    _maybe_accept,
    _submit_email_step,
    _type_email_address,
    _type_otp,
    _visible,
)
from core.codex_login_credentials import CodexLoginCredentials, generate_totp_code
from core.humanize import delay as human_delay
from core.openai_auth import (
    AccountUnusableError,
    account_unusable_error_message,
    detect_account_unusable_text,
)

logger = logging.getLogger(__name__)


def _page_state(driver) -> dict:
    from selenium.common.exceptions import WebDriverException

    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '',
          name: el.getAttribute('name') || '',
          id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '',
          inputmode: el.getAttribute('inputmode') || '',
          ariaLabel: el.getAttribute('aria-label') || ''
        })).slice(0, 30);
        return {
          url: location.href,
          body: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 1600),
          inputs
        };
        """) or {}
    except WebDriverException:
        return {"url": str(getattr(driver, "current_url", "") or ""), "body": "", "inputs": []}


def classify_login_state(driver) -> str:
    state = _page_state(driver)
    url = str(state.get("url") or getattr(driver, "current_url", "") or "").lower()
    body = str(state.get("body") or "").lower()
    dead_code = detect_account_unusable_text(body)
    if dead_code:
        return f"deactivated:{dead_code}"
    inputs = state.get("inputs") or []
    input_text = " ".join(
        " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete", "inputmode", "ariaLabel"))
        for item in inputs
        if isinstance(item, dict)
    ).lower()

    account_chooser_markers = (
        "account-chooser",
        "choose-account",
        "select-account",
        "choose an account",
        "select an account",
        "pick an account",
        "continue as",
        "use another account",
        "选择账号",
        "选择一个账号",
        "选择帐户",
        "选择账户",
        "chọn tài khoản",
    )
    if any(marker in url or marker in body for marker in account_chooser_markers):
        return "account_chooser"

    if any(marker in url for marker in ("localhost:1455/auth/callback", "/add-phone", "/phone-verification", "/workspace", "/consent")):
        return "accepted"

    email_markers = (
        "email-verification",
        "email_otp",
        "sent a code to your email",
        "code to your email",
        "check your inbox",
        "邮箱验证码",
        "電子メールに送信",
    )
    if any(marker in url or marker in body for marker in email_markers):
        return "email_otp"

    totp_markers = (
        "/mfa",
        "/totp",
        "authenticator",
        "two-factor",
        "two factor",
        "2fa",
        "动态验证码",
        "身份验证器",
        "認証アプリ",
    )
    is_totp = any(marker in url or marker in body for marker in totp_markers)
    rejection_markers = ("incorrect", "invalid", "expired", "try again", "错误", "无效", "过期")
    if is_totp and any(marker in body for marker in rejection_markers):
        return "totp_invalid"
    if is_totp and any(marker in input_text for marker in ("one-time-code", "code", "otp", "numeric", "tel")):
        return "totp"

    if "password" in url and any(marker in body for marker in rejection_markers):
        return "password_invalid"
    if any(str(item.get("type") or "").lower() == "password" for item in inputs if isinstance(item, dict)):
        return "password"
    if any(marker in body for marker in ("allow access", "authorize codex", "continue to codex")):
        return "accepted"
    return "unknown"


def _wait_for_login_state(
    driver,
    timeout: float = 35.0,
    *,
    ignored_states: set[str] | None = None,
) -> str:
    ignored_states = {str(state).strip().lower() for state in (ignored_states or set())}
    end = time.time() + max(0.0, float(timeout))
    last_ignored_state = ""
    while time.time() < end:
        state = classify_login_state(driver)
        if state != "unknown" and state not in ignored_states:
            return state
        if state in ignored_states:
            last_ignored_state = state
        time.sleep(0.4)
    return last_ignored_state or "unknown"


def _log_state_snapshot(driver, reason: str) -> None:
    if not callable(getattr(driver, "execute_script", None)):
        logger.warning(
            "[Codex][Credential] 登录状态诊断失败：reason=%s error=driver_snapshot_unavailable",
            reason,
        )
        return
    snapshot = _page_state(driver)
    raw_url = str(snapshot.get("url") or "")
    parsed = urlsplit(raw_url)
    safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else raw_url.split("?", 1)[0]
    body = " ".join(str(snapshot.get("body") or "").split())[:400]
    inputs = snapshot.get("inputs") or []
    logger.warning(
        "[Codex][Credential] 登录状态诊断：reason=%s url=%s inputs=%s body=%s",
        reason,
        safe_url,
        inputs,
        body,
    )


def _select_matching_account(driver, email: str, timeout: float = 12.0) -> bool:
    """Select an account chooser entry only when its visible text matches email."""
    from selenium.webdriver.common.by import By

    target = str(email or "").strip().lower()
    if not target:
        return False
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        try:
            elements = driver.find_elements(
                By.CSS_SELECTOR,
                "button, [role='button'], a, [role='option'], [role='listitem'], [data-email]",
            )
            for element in elements:
                if not _visible(element):
                    continue
                text = " ".join(
                    str(value or "")
                    for value in (
                        getattr(element, "text", ""),
                        element.get_attribute("aria-label"),
                        element.get_attribute("data-email"),
                    )
                ).lower()
                if target in text:
                    element.click()
                    logger.info("[Codex][Credential] 已选择匹配的 OAuth account chooser 项")
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Codex][Credential] account chooser 选择失败，继续等待：%s", str(exc)[:120])
        time.sleep(0.4)
    return False


def _open_and_submit_email(driver, email: str, auth_url: str) -> str:
    driver.get(auth_url)
    human_delay("navigate")
    _maybe_accept(driver)
    initial_state = classify_login_state(driver)
    if initial_state in {"accepted", "account_chooser"}:
        logger.info("[Codex][Credential] Auth 页无需输入邮箱：state=%s", initial_state)
        return initial_state
    _type_email_address(driver, email, timeout=20)
    human_delay("form")
    _submit_email_step(driver, email)
    logger.info("[Codex][Credential] 已提交账号邮箱")
    return "email_submitted"


def _resolve_account_chooser(driver, email: str, state: str) -> str:
    """Select the requested account and wait for the next auth state."""
    if state != "account_chooser":
        return state
    if not _select_matching_account(driver, email):
        raise _state_error("account_chooser", driver)
    next_state = _wait_for_login_state(driver, ignored_states={"account_chooser"})
    if next_state == "account_chooser":
        raise _state_error("account_chooser", driver)
    return next_state


def _submit_password(driver, password: str, timeout: float = 25.0) -> None:
    from selenium.webdriver.common.by import By

    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        for selector in ("input[type='password']", "input[name*='password' i]", "input[autocomplete='current-password']"):
            elements = [element for element in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(element)]
            if not elements:
                continue
            _human_type_text(driver, elements[0], password, clear=True)
            human_delay("form")
            _click_continue(driver)
            logger.info("[Codex][Credential] 已提交账号密码")
            return
        time.sleep(0.4)
    raise RuntimeError("Codex credential password input not found")


def _submit_totp_code(driver, code: str) -> None:
    _clear_otp_inputs(driver)
    _type_otp(driver, code)
    human_delay("otp_input")
    _click_continue(driver)
    logger.info("[Codex][Credential] 已提交 authenticator code")


def _state_error(state: str, driver=None) -> Exception:
    if driver is not None and state in {"unknown", "account_chooser", "password", "totp"}:
        _log_state_snapshot(driver, state)
    if state == "email_otp":
        return RuntimeError("Codex credential login unexpectedly requires email OTP; mailbox fallback is disabled")
    if state.startswith("deactivated:"):
        error_code = state.split(":", 1)[1] or "account_deactivated"
        return AccountUnusableError(account_unusable_error_message(error_code), error_code=error_code)
    if state == "password_invalid":
        return RuntimeError("Codex credential login password was rejected")
    if state == "password":
        return RuntimeError("Codex credential login stayed on the password page after submit")
    if state == "account_chooser":
        return RuntimeError("Codex credential login account chooser did not expose the target account")
    if state == "totp":
        return RuntimeError("Codex credential login stayed on the authenticator page after submit")
    return RuntimeError(f"Codex credential login did not reach the OAuth flow: state={state}")


def login_with_credentials(
    driver,
    credentials: CodexLoginCredentials,
    auth_url: str,
) -> None:
    """Authenticate locally with password and authenticator TOTP."""
    entry_state = _open_and_submit_email(driver, credentials.email, auth_url)
    if entry_state == "account_chooser":
        state = _resolve_account_chooser(driver, credentials.email, entry_state)
    else:
        state = _wait_for_login_state(driver)
    state = _resolve_account_chooser(driver, credentials.email, state)
    if state == "accepted":
        return
    if state == "password":
        _submit_password(driver, credentials.password)
        state = _wait_for_login_state(driver, ignored_states={"password"})
        state = _resolve_account_chooser(driver, credentials.email, state)
    if state == "accepted":
        return
    if state != "totp":
        raise _state_error(state, driver)

    previous_code = None
    for attempt in range(2):
        code = generate_totp_code(credentials.totp_secret, previous_code=previous_code)
        previous_code = code
        _submit_totp_code(driver, code)
        state = _wait_for_login_state(driver, ignored_states={"totp"})
        state = _resolve_account_chooser(driver, credentials.email, state)
        if state == "accepted":
            return
        if state != "totp_invalid" or attempt >= 1:
            raise _state_error(state, driver)
    raise RuntimeError("Codex credential login TOTP was rejected")
