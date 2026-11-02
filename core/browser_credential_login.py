"""Local Selenium password/TOTP login adapter for Codex OAuth."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from core.browser_email_entry import (
    _submit_email_step,
    _type_email_address,
)
from core.browser_page_actions import (
    _clear_otp_inputs,
    _click_any,
    _click_continue,
    _human_type_text,
    _type_otp,
    _type_otp_with_verification,
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
        const enabled = el => visible(el) && !el.matches(':disabled')
          && !el.closest('[aria-disabled="true" i], [inert]');
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '',
          name: el.getAttribute('name') || '',
          id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '',
          inputmode: el.getAttribute('inputmode') || '',
          ariaLabel: el.getAttribute('aria-label') || ''
        })).slice(0, 30);
        const sessionEntry = [...document.querySelectorAll('button[name="session_id"], input[type="submit"][name="session_id"], [role="button"][name="session_id"], [role="option"][name="session_id"]')]
          .some(enabled);
        const otherAccount = [...document.querySelectorAll('a[href], form[action], button[formaction], input[formaction]')]
          .some(el => {
            const url = el.getAttribute('href') || el.getAttribute('action') || el.getAttribute('formaction');
            if (!enabled(el) || !url) return false;
            if (url.startsWith('/') && !url.startsWith('//')) {
              return url.split(/[?#]/, 1)[0].replace(/\/+$/, '') === '/log-in-or-create-account';
            }
            try {
              const destination = new URL(url, location.href);
              return destination.pathname.replace(/\/+$/, '') === '/log-in-or-create-account'
                && (!/^https?:$/.test(location.protocol) || destination.origin === location.origin);
            } catch (_) { return false; }
          });
        return {
          url: location.href,
          body: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 1600),
          inputs,
          accountChooser: sessionEntry && otherAccount
        };
        """) or {}
    except WebDriverException:
        try:
            url = str(getattr(driver, "current_url", "") or "")
        except WebDriverException:
            url = ""
        return {"url": url, "body": "", "inputs": []}


def classify_login_state(driver) -> str:
    state = _page_state(driver)
    url = str(state.get("url") or "")
    body = str(state.get("body") or "").lower()
    try:
        parsed_url = urlsplit(url)
        host = parsed_url.hostname or ""
        port = parsed_url.port
    except ValueError:
        return "untrusted"
    if parsed_url.username is not None or parsed_url.password is not None:
        return "untrusted"
    if (
        parsed_url.scheme in {"http", "https"}
        and host in {"localhost", "127.0.0.1"}
        and port == 1455
        and parsed_url.path == "/auth/callback"
    ):
        return "accepted"
    if parsed_url.scheme != "https" or host != "auth.openai.com" or port not in {None, 443}:
        return "untrusted"
    path = parsed_url.path.lower()
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
    accepted_segments = {"add-phone", "phone-verification", "workspace", "consent"}
    if state.get("accountChooser") is True or any(marker in path for marker in account_chooser_markers):
        return "account_chooser"
    if accepted_segments.intersection(path.split("/")):
        return "accepted"
    if any(marker in body for marker in account_chooser_markers if marker not in {"continue as", "use another account"}):
        return "account_chooser"
    if any(marker in body for marker in ("allow access", "authorize codex", "continue to codex")):
        return "accepted"
    if any(marker in body for marker in account_chooser_markers):
        return "account_chooser"

    email_markers = (
        "email-verification",
        "email_otp",
        "sent a code to your email",
        "code to your email",
        "check your inbox",
        "邮箱验证码",
        "電子メールに送信",
    )
    if any(marker in path or marker in body for marker in email_markers):
        rejected = any(marker in body for marker in ("incorrect", "invalid", "expired", "try again", "错误", "无效", "过期"))
        return "email_otp_invalid" if rejected else "email_otp"

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
    is_totp = any(marker in path or marker in body for marker in totp_markers)
    rejection_markers = ("incorrect", "invalid", "expired", "try again", "错误", "无效", "过期")
    if is_totp and any(marker in body for marker in rejection_markers):
        return "totp_invalid"
    if is_totp and any(marker in input_text for marker in ("one-time-code", "code", "otp", "numeric", "tel")):
        return "totp"

    if "password" in path and any(marker in body for marker in rejection_markers):
        return "password_invalid"
    if any(str(item.get("type") or "").lower() == "password" for item in inputs if isinstance(item, dict)):
        return "password"
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
    logger.warning(
        "[Codex][Credential] 登录状态诊断：reason=%s input_count=%s",
        reason,
        len(snapshot.get("inputs") or []),
    )


def _select_matching_account(driver, email: str, timeout: float = 12.0) -> bool:
    """Select a unique, visibly identified account in one browser operation."""
    from selenium.common.exceptions import WebDriverException

    target = str(email or "").strip().casefold()
    if not target or "@" not in target:
        return False
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        try:
            state = classify_login_state(driver)
            if state == "unknown":
                time.sleep(0.4)
                continue
            if state != "account_chooser":
                return False
            result = driver.execute_script(r"""
            const target = String(arguments[0]);
            if (location.origin !== 'https://auth.openai.com') return 'rejected';
            const path = location.pathname.toLowerCase();
            const body = (document.body?.innerText || '').toLowerCase();
            if (path.split('/').some(part => ['consent', 'workspace', 'add-phone', 'phone-verification'].includes(part))) {
              return 'rejected';
            }
            const visible = el => {
              if (!el || !el.isConnected || ![...el.getClientRects()].some(rect => rect.width > 0 && rect.height > 0)
                  || el.closest('[hidden], [aria-hidden="true" i], [inert]')) return false;
              for (let node = el; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0
                    || style.clipPath !== 'none' || style.clip !== 'auto') return false;
              }
              return true;
            };
            const enabled = el => visible(el) && !el.matches(':disabled')
              && !el.closest('[aria-disabled="true" i], [inert]');
            const selector = 'button[name="session_id"], input[type="submit"][name="session_id"], '
              + '[role="button"][name="session_id"], [role="option"][name="session_id"]';
            const entries = [...document.querySelectorAll(selector)].filter(enabled);
            const otherAccount = [...document.querySelectorAll('a[href], form[action], button[formaction], input[formaction]')]
              .some(el => {
                if (!enabled(el)) return false;
                const value = el.getAttribute('href') || el.getAttribute('action') || el.getAttribute('formaction');
                try {
                  const destination = new URL(value, location.href);
                  return destination.origin === location.origin
                    && destination.pathname.replace(/\/+$/, '') === '/log-in-or-create-account';
                } catch (_) { return false; }
              });
            const chooserText = /account-chooser|choose-account|select-account|choose an account|select an account|pick an account|continue as|use another account|选择账号|选择一个账号|选择帐户|选择账户|chọn tài khoản/;
            if (/allow access|authorize codex/.test(body) || (/continue to codex/.test(body) && !otherAccount)) {
              return 'rejected';
            }
            if (!entries.length) return 'wait';
            if (!otherAccount && !chooserText.test(path + ' ' + body)) return 'wait';
            const unsafeProvider = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider/i;
            const unsafeAction = /\b(?:authoriz\w*|consent|(?:grant|allow)[\s_:-]+(?:access|permissions?|authorization))\b/i;
            const attrs = node => [node.id, node.className, node.getAttribute('name'), node.getAttribute('data-provider'),
              node.getAttribute('data-auth-provider'), node.getAttribute('data-idp'), node.getAttribute('href'),
              node.getAttribute('action'), node.getAttribute('formaction'), node.getAttribute('aria-label'),
              node.getAttribute('data-testid'), node.getAttribute('alt'), node.getAttribute('src')]
              .filter(Boolean).join(' ');
            const emailPattern = /[^\s@<>"(),;:]+@[^\s@<>"(),;:]+/g;
            const addresses = value => (String(value || '').match(emailPattern) || []).map(value => value.toLowerCase());
            const text = (node, visibleOnly) => {
              if (node.nodeType === Node.TEXT_NODE) return node.textContent;
              if (node.nodeType !== Node.ELEMENT_NODE || (visibleOnly && !visible(node))) return ' ';
              return [...node.childNodes].map(child => {
                const value = text(child, visibleOnly);
                return child.nodeType === Node.ELEMENT_NODE && addresses(value).length ? ' ' + value + ' ' : value;
              }).join('');
            };
            const matches = [];
            for (const el of entries) {
              const visibleText = el.tagName === 'INPUT' ? el.value : text(el, true);
              const allText = el.tagName === 'INPUT' ? el.value : text(el, false);
              const visibleAddresses = new Set(addresses(visibleText));
              const allAddresses = new Set(addresses(allText));
              let dataEmailConflict = false;
              const descendants = [el, ...el.querySelectorAll('*')];
              for (const node of descendants) {
                const dataEmail = node.getAttribute('data-email');
                if (dataEmail !== null && dataEmail.trim().toLowerCase() !== target) dataEmailConflict = true;
                addresses(dataEmail).forEach(address => allAddresses.add(address));
                addresses(node.getAttribute('aria-label')).forEach(address => allAddresses.add(address));
              }
              if (!allAddresses.has(target)) continue;
              const activation = [el];
              for (let node = el.parentElement; node; node = node.parentElement) {
                if (node.matches('a[href], form, button, input, select, textarea, label, [role="button"], '
                    + '[role="option"], [form], [formaction]')) activation.push(node);
              }
              for (const node of [...activation]) {
                if (node.form && !activation.includes(node.form)) activation.push(node.form);
                if (node.control && !activation.includes(node.control)) activation.push(node.control);
              }
              const badDestination = activation.some(node =>
                [node.getAttribute('href'), node.getAttribute('action'), node.getAttribute('formaction')]
                  .filter(Boolean).some(value => {
                    try { return new URL(value, location.href).origin !== location.origin; }
                    catch (_) { return true; }
                  }));
              const labels = visibleText.replace(emailPattern, '');
              const unsafeMetadata = [...activation, ...descendants].some(node =>
                unsafeProvider.test(attrs(node)) || unsafeAction.test(attrs(node)));
              if (dataEmailConflict || visibleAddresses.size !== 1 || !visibleAddresses.has(target)
                  || allAddresses.size !== 1 || unsafeMetadata
                  || unsafeProvider.test(labels) || unsafeAction.test(labels) || badDestination) return 'rejected';
              matches.push(el);
            }
            if (matches.length > 1) return 'rejected';
            if (!matches.length) return 'wait';
            matches[0].click();
            return 'selected';
            """, target)
            if result == "selected":
                logger.info("[Codex][Credential] 已选择匹配的 OAuth account chooser 项")
                return True
            if result != "wait":
                return False
        except WebDriverException:
            logger.debug("[Codex][Credential] account chooser 选择失败")
            return False
        time.sleep(0.4)
    return False


class _LoginStateChanged(Exception):
    def __init__(self, state: str):
        self.state = state
        super().__init__(state)


def _open_and_submit_email(driver, email: str, auth_url: str) -> str:
    def check_login_state() -> None:
        state = classify_login_state(driver)
        if state != "unknown":
            raise _LoginStateChanged(state)

    driver.get(auth_url)
    human_delay("navigate")
    try:
        check_login_state()
        driver.execute_script(r"""
        if (location.origin !== 'https://auth.openai.com') return;
        const selectors = 'button#onetrust-accept-btn-handler, button[data-testid="cookie-accept"], '
          + 'button[data-testid="accept-cookies"]';
        const buttons = [...document.querySelectorAll(selectors)].filter(el =>
          el.isConnected && el.getClientRects().length && !el.matches(':disabled')
          && !el.closest('[hidden], [aria-hidden="true" i], [aria-disabled="true" i], [inert]')
          && getComputedStyle(el).visibility === 'visible' && getComputedStyle(el).display !== 'none');
        if (buttons.length === 1) buttons[0].click();
        """)
        check_login_state()
        _type_email_address(driver, email, timeout=20, before_action=check_login_state)
        human_delay("form")
        check_login_state()
        _submit_email_step(driver, email)
    except _LoginStateChanged as exc:
        logger.info("[Codex][Credential] Auth 页无需输入邮箱：state=%s", exc.state)
        return exc.state
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


def request_email_otp(otp_provider, email: str, after_ts: float | None) -> str:
    """Invoke an explicit credential OTP provider without exposing its errors."""
    try:
        code = otp_provider(email, after_ts=after_ts)
    except Exception:  # noqa: BLE001 - provider exceptions can contain secrets.
        raise RuntimeError("Codex credential email OTP provider failed") from None
    if not isinstance(code, str) or not code.strip():
        raise RuntimeError("Codex credential email OTP provider returned no code")
    return code.strip()


def acknowledge_email_otp(otp_provider, code: str) -> None:
    """Acknowledge only a code whose submission has advanced the login."""
    try:
        acknowledge = getattr(otp_provider, "acknowledge", None)
        if callable(acknowledge):
            acknowledge(code)
    except Exception:  # noqa: BLE001 - provider exceptions can contain secrets.
        raise RuntimeError("Codex credential email OTP acknowledgement failed") from None


def _submit_email_otp(driver, code: str) -> None:
    from selenium.webdriver.common.by import By

    try:
        _clear_otp_inputs(driver)
        for selector in (
            "input[autocomplete='one-time-code']", "input[name='code']",
            "input[name='otp']", "input[inputmode='numeric']", "input[type='tel']",
        ):
            elements = [element for element in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(element)]
            if len(elements) == 1:
                def write_single(selector=selector) -> None:
                    current = [element for element in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(element)]
                    if len(current) != 1:
                        raise RuntimeError("OTP input changed")
                    current[0].clear()
                    current[0].send_keys(code)

                _type_otp_with_verification(driver, code, write_single, "single")
                break
        else:
            def numeric_inputs():
                elements = [element for element in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(element)]
                return [
                    element for element in elements
                    if any(
                        marker in " ".join(str(element.get_attribute(key) or "") for key in (
                            "inputmode", "autocomplete", "aria-label", "name", "id", "type",
                        )).lower()
                        for marker in ("numeric", "one-time", "code", "otp", "tel")
                    )
                ]

            def write_segmented() -> None:
                elements = numeric_inputs()
                if len(elements) < len(code):
                    raise RuntimeError("OTP inputs changed")
                for element, digit in zip(elements, code):
                    element.send_keys(digit)

            if len(numeric_inputs()) < len(code):
                raise RuntimeError("OTP inputs not found")
            _type_otp_with_verification(driver, code, write_segmented, "segmented")
        human_delay("otp_input")
        _click_any(driver, [
            "button[type='submit']", "//button[@data-dd-action-name='Continue']",
            "//button[@data-dd-action-name='continue']", "//button[@data-login-web-auth-control='true' and @type='submit']",
            "//button[contains(., 'Continue')]", "//button[contains(., '続行')]",
            "//button[contains(., '继续')]", "//button[contains(., 'Next')]",
        ], timeout=20)
    except Exception:  # noqa: BLE001 - browser exceptions may contain input values.
        raise RuntimeError("Codex credential email OTP submission failed") from None
    logger.info("[Codex][Credential] 已提交邮箱验证码")


def _state_error(state: str, driver=None) -> Exception:
    if driver is not None and state in {"unknown", "account_chooser", "password", "totp"}:
        _log_state_snapshot(driver, state)
    if state == "email_otp":
        return RuntimeError("Codex credential login unexpectedly requires email OTP; mailbox fallback is disabled")
    if state == "email_otp_invalid":
        return RuntimeError("Codex credential login email OTP was rejected")
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
    if state == "totp_invalid":
        return RuntimeError("Codex credential login TOTP was rejected")
    return RuntimeError(f"Codex credential login did not reach the OAuth flow: state={state}")


def login_with_credentials(
    driver,
    credentials: CodexLoginCredentials,
    auth_url: str,
    otp_provider=None,
) -> None:
    """Authenticate locally with credentials and an optional email OTP provider."""
    otp_after_ts = time.time()
    entry_state = _open_and_submit_email(driver, credentials.email, auth_url)
    if entry_state in {
        "accepted", "account_chooser", "password", "password_invalid", "totp", "totp_invalid",
        "email_otp", "email_otp_invalid",
    } or (isinstance(entry_state, str) and entry_state.startswith("deactivated:")):
        state = entry_state
    else:
        state = _wait_for_login_state(driver)
    if entry_state in {"email_otp", "email_otp_invalid"}:
        otp_after_ts = None
    state = _resolve_account_chooser(driver, credentials.email, state)
    password_submitted = False
    email_attempts = 0
    previous_email_code = None
    totp_attempts = 0
    previous_totp_code = None
    while True:
        if state == "accepted":
            return
        if state in {"email_otp", "email_otp_invalid"}:
            if otp_provider is None:
                raise _state_error("email_otp")
            if email_attempts >= 2:
                raise _state_error("email_otp_invalid")
            code = request_email_otp(otp_provider, credentials.email, otp_after_ts)
            if code == previous_email_code:
                raise RuntimeError("Codex credential email OTP provider returned a previously rejected code")
            retrying_rejection = state == "email_otp_invalid"
            previous_email_code = code
            email_attempts += 1
            _submit_email_otp(driver, code)
            ignored = {"email_otp", "email_otp_invalid"} if retrying_rejection else {"email_otp"}
            state = _wait_for_login_state(driver, ignored_states=ignored)
            if state == "account_chooser":
                if not _select_matching_account(driver, credentials.email):
                    raise _state_error("account_chooser")
                state = _wait_for_login_state(driver, ignored_states={"account_chooser"})
            if state in {"accepted", "totp"} or (state == "password" and not password_submitted):
                acknowledge_email_otp(otp_provider, code)
                continue
            if state == "email_otp_invalid" and email_attempts < 2:
                continue
            raise _state_error(state)
        if state == "password" and not password_submitted:
            password_submitted = True
            _submit_password(driver, credentials.password)
            state = _wait_for_login_state(driver, ignored_states={"password"})
            state = _resolve_account_chooser(driver, credentials.email, state)
            continue
        if state == "totp" or (state == "totp_invalid" and 0 < totp_attempts < 2):
            if totp_attempts >= 2:
                raise _state_error(state, driver)
            code = generate_totp_code(credentials.totp_secret, previous_code=previous_totp_code)
            previous_totp_code = code
            totp_attempts += 1
            _submit_totp_code(driver, code)
            state = _wait_for_login_state(driver, ignored_states={"totp"})
            state = _resolve_account_chooser(driver, credentials.email, state)
            continue
        raise _state_error(state, driver)
