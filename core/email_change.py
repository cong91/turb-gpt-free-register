"""Credential-driven ChatGPT email change workflow primitives."""
from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from urllib.parse import urlsplit

from config import email as email_config
from core.codex_login_credentials import CodexLoginCredentials, generate_totp_code
from core.gmail_aliases import (
    MAX_GMAIL_VARIANTS,
    GmailAliasError,
    build_gmail_alias_plan,
)
from core.gmail_api_url_client import (
    GmailApiUrlAccount,
    acknowledge_verification_code,
    poll_verification_code,
)
from core.gmail_api_url_client import (
    snapshot_verification_code as snapshot_gmail_verification_code,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_CHANGE_MAX_QUOTA = MAX_GMAIL_VARIANTS
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailChangeInput:
    old_email: str
    password: str = dataclass_field(repr=False)
    totp_secret: str = dataclass_field(repr=False)
    new_email: str
    code_url: str
    gmail_source_email: str = ""

    def credentials(self) -> CodexLoginCredentials:
        return CodexLoginCredentials(
            email=self.old_email,
            password=self.password,
            totp_secret=self.totp_secret,
        )

    def gmail_account(self) -> GmailApiUrlAccount:
        return GmailApiUrlAccount(email=self.new_email, code_url=self.code_url)


def _valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(str(value or "").strip()))


def _validate_code_url(code_url: str) -> None:
    parsed = urlsplit(code_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Gmail API URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Gmail API URL must not contain credentials")
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Gmail API URL must use a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Gmail API URL must use a public host")


def _parse_credential_lines_strict(text: str) -> list[tuple[str, str, str]]:
    records: list[tuple[str, str, str]] = []
    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.find("|")
        last = line.rfind("|")
        if first <= 0 or last <= first:
            raise ValueError(f"credential line {line_number} is invalid")
        email = line[:first].strip()
        password = line[first + 1:last].strip()
        totp_secret = line[last + 1:].strip()
        if not _valid_email(email) or not password or not totp_secret:
            raise ValueError(f"credential line {line_number} is invalid")
        records.append((email, password, totp_secret))
    return records


def _parse_gmail_api_lines(text: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        delimiter = "----" if "----" in line else "====" if "====" in line else ""
        if not delimiter:
            raise ValueError(f"Gmail API line {line_number} is invalid")
        email, code_url = (part.strip() for part in line.split(delimiter, 1))
        if not email or not code_url:
            raise ValueError(f"Gmail API line {line_number} is invalid")
        if not _valid_email(email):
            raise ValueError("target email is invalid")
        _validate_code_url(code_url)
        records.append((email, code_url))
    return records


def _parse_quota(quota: int | str) -> int:
    value = str(quota).strip()
    if not value.isdigit():
        raise ValueError(f"quota must be between 1 and {EMAIL_CHANGE_MAX_QUOTA}")
    parsed = int(value)
    if not 1 <= parsed <= EMAIL_CHANGE_MAX_QUOTA:
        raise ValueError(f"quota must be between 1 and {EMAIL_CHANGE_MAX_QUOTA}")
    return parsed


def parse_email_change_inputs(
    credentials_text: str,
    gmail_api_text: str,
    *,
    quota: int | str = 1,
) -> list[EmailChangeInput]:
    """Pair credentials with each Gmail API source expanded by its quota."""
    quota_value = _parse_quota(quota)
    credentials = _parse_credential_lines_strict(credentials_text)
    gmail_records = _parse_gmail_api_lines(gmail_api_text)
    if not credentials or not gmail_records:
        raise ValueError("both credential and Gmail API URL input are required")

    required_credentials = len(gmail_records) * quota_value
    if len(credentials) != required_credentials:
        raise ValueError(
            f"{len(gmail_records)} Gmail API lines with quota {quota_value} "
            f"require {required_credentials} credential lines"
        )

    targets: list[tuple[str, str, str]] = []
    seen_code_urls: set[str] = set()
    for source_email, code_url in gmail_records:
        if code_url in seen_code_urls:
            raise ValueError("Gmail API code URL must be unique")
        seen_code_urls.add(code_url)
        if quota_value == 1:
            aliases = [source_email]
        else:
            try:
                plan = build_gmail_alias_plan(source_email, limit=quota_value)
            except GmailAliasError as exc:
                raise ValueError("quota above 1 requires a Gmail source email") from exc
            aliases = [candidate.email for candidate in plan.original_candidates]
        targets.extend((source_email, alias, code_url) for alias in aliases)

    result: list[EmailChangeInput] = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    for (old_email, password, totp_secret), (gmail_source_email, new_email, code_url) in zip(credentials, targets):
        old_key = old_email.casefold()
        target_key = new_email.casefold()
        if old_key in seen_sources:
            raise ValueError("source email must be unique")
        if old_key == target_key:
            raise ValueError("target email must differ from source email")
        if target_key in seen_targets:
            raise ValueError("target email must be unique")
        seen_sources.add(old_key)
        seen_targets.add(target_key)
        result.append(
            EmailChangeInput(
                old_email=old_email,
                password=password,
                totp_secret=totp_secret,
                new_email=new_email,
                code_url=code_url,
                gmail_source_email=gmail_source_email,
            )
        )
    return result


def snapshot_verification_code(item: EmailChangeInput) -> str | None:
    """Capture the current provider code before requesting a new email."""
    return snapshot_gmail_verification_code(item.gmail_account())


def poll_new_email_otp(
    item: EmailChangeInput,
    *,
    after_ts: float,
    before_code: str | None,
) -> str:
    """Poll the configured Gmail API URL for the post-change verification code."""
    return poll_verification_code(
        item.gmail_account(),
        max_wait=int(getattr(email_config, "OTP_MAX_WAIT", 60) or 60),
        poll_interval=int(getattr(email_config, "OTP_POLL_INTERVAL", 3) or 3),
        after_ts=after_ts,
        before_code=before_code,
    )


def _page_snapshot(driver) -> dict:
    script = r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden'
          && getComputedStyle(el).display !== 'none';
        return {
          url: location.href || '',
          body: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 2400),
          inputs: [...document.querySelectorAll('input')].filter(visible).map(el => ({
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            autocomplete: el.getAttribute('autocomplete') || '',
            ariaLabel: el.getAttribute('aria-label') || ''
          })).slice(0, 30)
        };
    """
    try:
        return driver.execute_script(script) or {}
    except Exception:  # noqa: BLE001 - browser adapters expose driver-specific exceptions.
        return {"url": str(getattr(driver, "current_url", "") or ""), "body": "", "inputs": []}


def classify_email_change_state(snapshot: dict) -> str:
    """Classify only the states needed by the account email-change flow."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    url = str(snapshot.get("url") or "").lower()
    body = str(snapshot.get("body") or "").lower()
    inputs = snapshot.get("inputs") or []
    input_text = " ".join(
        " ".join(str(item.get(key) or "") for key in ("type", "name", "id", "autocomplete", "ariaLabel"))
        for item in inputs
        if isinstance(item, dict)
    ).lower()
    if any(marker in body for marker in (
        "email updated", "email changed", "email address updated", "successfully changed",
        "邮箱已更新", "邮箱已更改",
    )):
        return "success"
    if "auth.openai.com" in url and any(item.get("type") == "password" for item in inputs if isinstance(item, dict)):
        return "password"
    if (
        any(marker in url or marker in body for marker in ("/mfa", "/totp", "authenticator", "two-factor", "two factor"))
        and any(marker in input_text for marker in ("one-time-code", "code", "otp", "numeric", "tel"))
    ):
        return "totp"
    if "verification_code" in input_text or "one-time-code" in input_text or "verification code" in body:
        return "email_otp"
    if "settings" in url or body.strip() in {"account", "settings account"} or "account settings" in body:
        return "settings"
    if "chatgpt.com" in url and not any(
        marker in input_text for marker in ("email", "password", "one-time-code", "verification_code")
    ):
        return "authenticated"
    return "unknown"


def _wait_for_change_state(driver, timeout: float = 35.0, *, ignored: set[str] | None = None) -> str:
    ignored = {str(value).lower() for value in (ignored or set())}
    end = time.time() + max(0.0, float(timeout))
    last = "unknown"
    while time.time() < end:
        state = classify_email_change_state(_page_snapshot(driver))
        last = state
        if state not in ignored and state != "unknown":
            return state
        time.sleep(0.4)
    return last


def _login_chatgpt_with_credentials(driver, credentials: CodexLoginCredentials) -> None:
    from selenium.webdriver.common.by import By

    from core.browser_credential_login import (
        _clear_otp_inputs,
        _click_continue,
        _human_type_text,
        _maybe_accept,
        _submit_email_step,
        _type_email_address,
        _type_otp,
        _visible,
    )

    driver.get("https://chatgpt.com/auth/login")
    _maybe_accept(driver)
    _type_email_address(driver, credentials.email, timeout=20)
    _submit_email_step(driver, credentials.email)
    state = _wait_for_change_state(driver)
    if state == "password":
        end = time.time() + 25
        while time.time() < end:
            elements = [
                element
                for selector in ("input[type='password']", "input[name*='password' i]", "input[autocomplete='current-password']")
                for element in driver.find_elements(By.CSS_SELECTOR, selector)
                if _visible(element)
            ]
            if elements:
                _human_type_text(driver, elements[0], credentials.password, clear=True)
                _click_continue(driver)
                break
            time.sleep(0.4)
        state = _wait_for_change_state(driver, ignored={"password"})
    if state == "email_otp":
        raise RuntimeError("email OTP is required; credential email fallback is disabled")
    if state == "totp":
        previous_code = None
        for attempt in range(2):
            code = generate_totp_code(
                credentials.totp_secret,
                previous_code=previous_code,
            )
            previous_code = code
            _clear_otp_inputs(driver)
            _type_otp(driver, code)
            _click_continue(driver)
            state = _wait_for_change_state(driver, ignored={"totp"})
            if state in {"authenticated", "settings"}:
                return
            if state != "totp" or attempt == 1:
                break
    if state not in {"authenticated", "settings"}:
        raise RuntimeError(f"ChatGPT credential login did not complete: state={state}")


def _click_matching_text(driver, terms: tuple[str, ...]) -> bool:
    script = r"""
        const terms = arguments[0].map(value => String(value).toLowerCase());
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const nodes = [...document.querySelectorAll('button,[role="button"],[role="menuitem"],a')]
          .filter(visible)
          .map(el => ({el, text: `${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`.toLowerCase()}));
        const match = nodes.find(item => terms.some(term => item.text.includes(term)));
        if (!match) return false;
        match.el.click();
        return true;
    """
    try:
        return bool(driver.execute_script(script, list(terms)))
    except Exception:  # noqa: BLE001 - browser adapters expose driver-specific exceptions.
        return False


def _fill_email_input(driver, email: str) -> bool:
    script = r"""
        const value = String(arguments[0]);
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],#email')]
          .find(visible);
        if (!input) return false;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(input, value); else input.value = value;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    """
    try:
        return bool(driver.execute_script(script, email))
    except Exception:  # noqa: BLE001 - browser adapters expose driver-specific exceptions.
        return False


def _submit_email_change_request(driver, new_email: str) -> None:
    end = time.time() + 20
    while time.time() < end:
        if _fill_email_input(driver, new_email):
            submitted = driver.execute_script(r"""
                const input = [...document.querySelectorAll('input[type="email"],input[name="email"],#email')]
                  .find(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const form = input?.closest('form');
                const button = form?.querySelector('button[type="submit"],input[type="submit"]');
                if (button) { button.click(); return true; }
                if (form) { form.requestSubmit?.(); return true; }
                return false;
            """)
            if submitted:
                return
        time.sleep(0.4)
    raise RuntimeError("change email input or submit button not found")


def _submit_email_verification_code(driver, code: str) -> None:
    script = r"""
        const value = String(arguments[0]);
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const input = [...document.querySelectorAll('#verification_code,input[autocomplete="one-time-code"],input[name="verification_code"]')]
          .find(visible);
        if (!input) return false;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(input, value); else input.value = value;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        const form = input.closest('form');
        const button = form?.querySelector('button[type="submit"],input[type="submit"]');
        if (button) button.click(); else form?.requestSubmit?.();
        return true;
    """
    if not driver.execute_script(script, code):
        raise RuntimeError("email verification input not found")


def _session_email_matches(driver, expected: str) -> bool:
    try:
        return bool(driver.execute_async_script(r"""
            const expected = String(arguments[0]).toLowerCase();
            const done = arguments[arguments.length - 1];
            fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'})
              .then(response => response.ok ? response.json() : null)
              .then(data => {
                const email = data?.user?.email || data?.email || '';
                done(String(email).trim().toLowerCase() === expected);
              })
              .catch(() => done(false));
        """, expected))
    except Exception:  # noqa: BLE001 - browser adapters expose driver-specific exceptions.
        return False


def _wait_for_email_change_success(driver, new_email: str, timeout: float = 35.0) -> None:
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        state = classify_email_change_state(_page_snapshot(driver))
        if state == "success" or _session_email_matches(driver, new_email):
            return
        time.sleep(0.4)
    raise RuntimeError("email change confirmation was not observed")


def _open_settings_account(driver) -> None:
    driver.get("https://chatgpt.com/#settings/Account")
    end = time.time() + 20
    while time.time() < end:
        if _click_matching_text(driver, ("change email", "change email address", "更改邮箱", "修改邮箱")):
            return
        time.sleep(0.4)
    raise RuntimeError("ChatGPT change-email action was not found")


def change_email_in_browser(driver, item: EmailChangeInput) -> dict[str, object]:
    """Log in, submit a new email, verify it, and clear the session."""
    try:
        _login_chatgpt_with_credentials(driver, item.credentials())
        _open_settings_account(driver)
        before_code = snapshot_verification_code(item)
        submitted_at = time.time()
        _submit_email_change_request(driver, item.new_email)
        code = poll_new_email_otp(item, after_ts=submitted_at, before_code=before_code)
        _submit_email_verification_code(driver, code)
        _wait_for_email_change_success(driver, item.new_email)
        acknowledge_verification_code(item.gmail_account(), code)
        return {"ok": True, "old_email": item.old_email, "new_email": item.new_email}
    finally:
        logout = getattr(driver, "get", None)
        if callable(logout):
            try:
                logout("https://chatgpt.com/auth/logout")
            except Exception:  # noqa: BLE001 - logout is best-effort cleanup.
                logger.debug("ChatGPT logout cleanup failed")
