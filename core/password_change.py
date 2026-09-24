"""Pure ChatGPT password-change flow logic executed inside one browser page.

Không chứa vòng đời browser/proxy/persist (lớp đó nằm ở module batch riêng);
module này chỉ nhận một ``driver`` đã mở và chạy flow reauth đổi mật khẩu:

    csrf + signin (same-origin) -> authorize_url -> challenge (email OTP /
    mật khẩu hiện tại / TOTP) -> /reset-password/new-password -> fill + submit
    -> verify session còn accessToken.

Tránh dùng flow forgot-password vì nó kill toàn bộ session của account.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from urllib.parse import urlencode

from core import db
from core.account_export import BrowserPageTransport, fetch_session
from core.account_security import _should_try_oauth_fallback
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
from core.browser_credential_login import classify_login_state
from core.browser_page_actions import (
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
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
from core.openai_auth import AccountUnusableError, account_unusable_error_message
from core.registration_flow import _fetch_chatgpt_session

logger = logging.getLogger(__name__)

PASSWORD_CHANGE_MODES = frozenset({"post_login_password_reset", "post_login_add_password"})
PASSWORD_CHANGE_MAX_ITEMS = 50

_NEW_PASSWORD_URL_MARKER = "/reset-password/new-password"
_OTP_ATTEMPTS = 3
_TOTP_ATTEMPTS = 2
_LOGIN_PASSWORD_ATTEMPTS = 2
_CHALLENGE_STALL_TIMEOUT = 90.0
_NEW_PASSWORD_PAGE_TIMEOUT = 60.0
_NEW_PASSWORD_FILL_TIMEOUT = 60.0
_NEW_PASSWORD_SUBMIT_WAIT = 20.0
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Same-origin scripts mirroring core/account_export.py::BrowserPageTransport:
# page fetch keeps the logged-in cookie session; 15s AbortController caps each
# call so a hung renderer cannot block the driver thread forever.
_CSRF_FETCH_SCRIPT = r"""
const done = arguments[arguments.length - 1];
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), 15000);
fetch('/api/auth/csrf', {
  credentials: 'include',
  headers: {accept: '*/*'},
  signal: controller.signal,
})
  .then(async response => {
    const text = await response.text();
    let data = {};
    try { data = JSON.parse(text); } catch (_) {}
    done({ok: response.ok, status: response.status, data});
  })
  .catch(error => done({ok: false, status: 0, error: String(error)}))
  .finally(() => clearTimeout(timer));
"""

_SIGNIN_FETCH_SCRIPT = r"""
const url = arguments[0];
const body = arguments[1];
const done = arguments[arguments.length - 1];
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), 15000);
fetch(url, {
  method: 'POST',
  credentials: 'include',
  headers: {'content-type': 'application/x-www-form-urlencoded', accept: '*/*'},
  body,
  signal: controller.signal,
})
  .then(async response => {
    const text = await response.text();
    let data = {};
    try { data = JSON.parse(text); } catch (_) {}
    done({ok: response.ok, status: response.status, data, body: text.slice(0, 300)});
  })
  .catch(error => done({ok: false, status: 0, error: String(error)}))
  .finally(() => clearTimeout(timer));
"""

_NEW_PASSWORD_STATE_SCRIPT = r"""
const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
  && !el.disabled && !el.readOnly;
const inputs = [...document.querySelectorAll('input[type="password"], input[autocomplete*="password" i]')]
  .filter(visible);
const form = inputs[0]?.closest('form') || null;
const scope = form || document;
const buttons = [...scope.querySelectorAll('button, input[type="submit"]')].filter(visible);
const button = buttons.find(el => String(el.type || '').toLowerCase() === 'submit') || buttons[0] || null;
return {url: location.href, inputs, button};
"""


@dataclass(frozen=True, slots=True)
class PasswordChangeInput:
    email: str
    current_password: str = dataclass_field(default="", repr=False)
    new_password: str = dataclass_field(default="", repr=False)
    totp_secret: str | None = dataclass_field(default=None, repr=False)
    mode: str = ""


def parse_password_change_inputs(text: str) -> list[PasswordChangeInput]:
    """Parse ``email[----current_password[----totp_secret]]`` lines for a batch."""
    items: list[PasswordChangeInput] = []
    seen_emails: set[str] = set()
    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) > 3:
            raise ValueError(
                f"Dòng {line_number} có quá nhiều trường. "
                "Dùng: email----mật_khẩu_hiện_tại----2FA(tùy chọn)"
            )
        email = parts[0]
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError(f"Email dòng {line_number} không hợp lệ")
        email_key = email.casefold()
        if email_key in seen_emails:
            raise ValueError(f"Email bị trùng ở dòng {line_number}")
        if len(items) >= PASSWORD_CHANGE_MAX_ITEMS:
            raise ValueError(f"Tối đa {PASSWORD_CHANGE_MAX_ITEMS} tài khoản mỗi lượt đổi mật khẩu")
        seen_emails.add(email_key)
        items.append(
            PasswordChangeInput(
                email=email,
                current_password=parts[1] if len(parts) >= 2 else "",
                totp_secret=parts[2] if len(parts) >= 3 else None,
            )
        )
    if not items:
        raise ValueError("Cần danh sách tài khoản cần đổi mật khẩu")
    return items


def resolve_password_change_input(item: PasswordChangeInput) -> PasswordChangeInput:
    """Fill missing fields from the local DB and generate a random new password.

    Account không có trong DB -> current_password rỗng -> mode add-password,
    vẫn chấp nhận chạy (OpenAI sẽ yêu cầu OTP email trong luồng login sẵn có).
    """
    from core.registration_flow import _generate_registration_password

    account = db.get_account_by_email(item.email)
    current_password = str(item.current_password or "").strip()
    if not current_password and account is not None:
        current_password = db._extract_registration_password(account)
    totp_secret = str(item.totp_secret or "").strip()
    if not totp_secret and account is not None:
        totp_secret = str(account.get("totp_secret") or "").strip()
    new_password = str(item.new_password or "").strip() or _generate_registration_password()
    mode = str(item.mode or "").strip()
    if not mode:
        # Owner decision: random per account, so the shared REGISTER_PASSWORD
        # config is deliberately bypassed in favor of the registration generator.
        mode = "post_login_password_reset" if current_password else "post_login_add_password"
    if mode not in PASSWORD_CHANGE_MODES:
        raise ValueError(f"mode không hợp lệ: {mode}")
    return PasswordChangeInput(
        email=item.email,
        current_password=current_password,
        new_password=new_password,
        totp_secret=totp_secret or None,
        mode=mode,
    )


def _redacted_error(message: object, item: PasswordChangeInput) -> str:
    output = str(message or "")[:400]
    for secret in (item.new_password, item.current_password, item.totp_secret):
        if secret:
            output = output.replace(secret, "[redacted]")
    return re.sub(r"\b\d{6,8}\b", "[redacted-code]", output)


def _try_fetch_browser_session(driver) -> dict | None:
    """Read the browser's ChatGPT session; None when the browser has no login."""
    try:
        session = fetch_session(BrowserPageTransport(driver))
    except Exception as exc:  # noqa: BLE001 - no session is a normal state here.
        logger.debug(
            "[PassChange] chưa có phiên trong trình duyệt: %s: %s",
            type(exc).__name__,
            str(exc)[:160],
        )
        return None
    if not session.get("accessToken"):
        return None
    return session


def _oauth_password_change_session(item: PasswordChangeInput) -> dict:
    """Protocol OAuth login; refreshes the account token but not browser cookies."""
    from core.account_liveness import login_account_via_oauth

    result = login_account_via_oauth(item.email, item.current_password, item.totp_secret)
    if result.get("ok") and result.get("access_token"):
        return result
    error = str(result.get("error") or "OAuth login did not return accessToken").strip()
    raise RuntimeError(f"ChatGPT OAuth login failed: {error[:300]}")


def _ensure_password_change_session(
    driver,
    item: PasswordChangeInput,
    *,
    allow_oauth_fallback: bool,
) -> dict:
    """Ensure the browser holds an authenticated ChatGPT session and return it."""
    session = _try_fetch_browser_session(driver)
    if session is not None:
        return session
    from core.browser_twofa_login import _login_existing_account

    logger.info("[PassChange] phiên không dùng được, đăng nhập lại tài khoản")
    try:
        session = _login_existing_account(
            driver,
            item.email,
            item.current_password,
            totp_secret=item.totp_secret or None,
        )
        if session.get("accessToken"):
            return session
        raise RuntimeError("Đăng nhập lại xong nhưng không có accessToken")
    except Exception as exc:
        if not allow_oauth_fallback or not _should_try_oauth_fallback(exc):
            raise
        logger.warning(
            "[PassChange] browser login thất bại, thử OAuth login: %s: %s",
            type(exc).__name__,
            str(exc)[:180],
        )
        _oauth_password_change_session(item)
        # OAuth login chỉ cấp bearer token, không tạo cookie phiên trình duyệt;
        # flow đổi mật khẩu chạy same-origin nên phiên trình duyệt vẫn bắt buộc.
        session = _try_fetch_browser_session(driver)
        if session is None:
            raise RuntimeError(
                "OAuth login chỉ cấp token; trình duyệt vẫn chưa có phiên ChatGPT"
            ) from exc
        return session


def _build_password_change_request(email: str, device_id: str, csrf_token: str, mode: str) -> dict:
    """Build the same-origin reauth signin request for one password-change mode."""
    query = {
        "connection": "password",
        "login_hint": str(email or "").strip(),
        "reauth": "password",
        mode: "true",
        "max_age": "0",
        "ext-oai-did": str(device_id or "").strip(),
    }
    body = {
        "callbackUrl": "https://chatgpt.com/",
        "csrfToken": str(csrf_token or ""),
        "json": "true",
    }
    return {
        "url": "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query),
        "body": urlencode(body),
    }


def _password_change_device_id(driver) -> str:
    try:
        value = driver.execute_script("return window.localStorage.getItem('oaicom_stable_id') || '';")
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 - device id is best-effort.
        logger.debug("[PassChange] oaicom_stable_id lookup failed")
    return str(uuid.uuid4())


def _fetch_password_change_authorize_url(driver, email: str, mode: str) -> str:
    """Run csrf + signin reauth inside the logged-in chatgpt.com page."""
    current_url = str(getattr(driver, "current_url", "") or "").lower()
    if "chatgpt.com" not in current_url:
        _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))

    csrf_result = driver.execute_async_script(_CSRF_FETCH_SCRIPT) or {}
    csrf_data = csrf_result.get("data") if isinstance(csrf_result, dict) else None
    csrf_token = str(csrf_data.get("csrfToken") or "") if isinstance(csrf_data, dict) else ""
    if not csrf_token:
        # CSRF response body contains the token itself; never echo it into errors.
        raise RuntimeError(
            "Lấy CSRF cho đổi mật khẩu thất bại: "
            f"status={csrf_result.get('status')} error={csrf_result.get('error')}"
        )

    request = _build_password_change_request(
        email,
        _password_change_device_id(driver),
        csrf_token,
        mode,
    )
    result = driver.execute_async_script(_SIGNIN_FETCH_SCRIPT, request["url"], request["body"]) or {}
    data = result.get("data") if isinstance(result, dict) else None
    authorize_url = str(data.get("url") or "").strip() if isinstance(data, dict) else ""
    if not authorize_url:
        detail = "" if result.get("ok") else f" body={result.get('body')}"
        raise RuntimeError(
            f"Không lấy được authorize URL cho đổi mật khẩu: status={result.get('status')}{detail}"
        )
    logger.info("[PassChange] đã tạo reauth đổi mật khẩu mode=%s", mode)
    return authorize_url


def _on_new_password_page(driver) -> bool:
    url = str(getattr(driver, "current_url", "") or "").lower()
    return _NEW_PASSWORD_URL_MARKER in url


def _password_change_state(driver) -> str:
    """Classify the page reached after opening the reauth authorize URL."""
    if _on_new_password_page(driver):
        return "new_password"
    if _is_email_verification_page(driver):
        return "email_otp"
    if _is_login_password_page(driver):
        return "login_password"
    return classify_login_state(driver)


def _is_otp_wait_timeout(error: BaseException) -> bool:
    """Only provider-reported OTP wait timeouts justify a resend (602 is terminal)."""
    if isinstance(error, TimeoutError):
        return True
    message = str(error).casefold()
    return any(marker in message for marker in ("timeout", "timed out", "超时"))


def _submit_password_change_email_otp(driver, email: str, otp_baseline: str | None) -> None:
    """Wait for the mailbox OTP and submit it; resends up to 3 attempts."""
    from config import twofa as _twofa_cfg

    otp_after_ts = time.time()
    previous_submitted_otp = None
    for attempt in range(1, _OTP_ATTEMPTS + 1):
        wait_kwargs = {
            "after_ts": otp_after_ts,
            "before_code": otp_baseline,
            "max_wait": int(getattr(_twofa_cfg, "TWOFA_OTP_MAX_WAIT", 90) or 90),
            "stage": "password_change_email_otp",
        }
        if previous_submitted_otp:
            wait_kwargs["before_code"] = previous_submitted_otp
        try:
            otp = wait_for_otp(email, **wait_kwargs)
        except Exception as exc:
            if not _is_otp_wait_timeout(exc) or attempt >= _OTP_ATTEMPTS:
                raise
            logger.warning(
                "[PassChange] chờ OTP quá hạn, gửi lại mã (%s/%s)", attempt + 1, _OTP_ATTEMPTS,
            )
        else:
            # The page can auto-advance to the new-password form while polling.
            if not _is_email_verification_page(driver):
                return
            _clear_otp_inputs(driver)
            _type_otp(driver, otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:  # noqa: BLE001 - page state polling is authoritative.
                logger.debug("[PassChange] OTP continue click unavailable: %s", exc)
            outcome = _wait_after_email_otp_submit(driver, timeout=15)
            if outcome != "accepted" and not _is_email_verification_page(driver):
                outcome = "accepted"
            if outcome == "accepted":
                acknowledge_verification_code(email, otp, stage="password_change_email_otp")
                return
            if attempt >= _OTP_ATTEMPTS:
                raise RuntimeError("Mã xác thực email sai hoặc hết hạn sau 3 lần thử")
            previous_submitted_otp = otp
        otp_baseline = snapshot_verification_code(email, stage="password_change_email_resend") or otp_baseline
        _click_resend_email_otp(driver, timeout=25)
        human_delay("api")
        otp_after_ts = time.time()
    raise RuntimeError("Mã xác thực email sai hoặc hết hạn sau 3 lần thử")


def _resolve_password_change_challenge(driver, item: PasswordChangeInput, otp_baseline: str | None) -> None:
    """Drive whatever challenge auth.openai.com shows until the new-password page."""
    from core.browser_twofa_login import (
        _login_password,
        _submit_existing_account_totp,
    )

    stall_deadline = time.time() + _CHALLENGE_STALL_TIMEOUT
    totp_attempts = 0
    login_password_attempts = 0
    otp_rounds = 0
    while True:
        state = _password_change_state(driver)
        if state == "new_password":
            return
        if state.startswith("deactivated:"):
            error_code = state.split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(
                account_unusable_error_message(error_code),
                error_code=error_code,
            )
        if state == "email_otp":
            otp_rounds += 1
            if otp_rounds > _OTP_ATTEMPTS:
                raise RuntimeError("Trang xác thực email lặp lại liên tục")
            _submit_password_change_email_otp(driver, item.email, otp_baseline)
        elif state == "login_password":
            if item.mode != "post_login_password_reset":
                raise RuntimeError(
                    "OpenAI yêu cầu mật khẩu hiện tại nhưng tài khoản này chưa có mật khẩu để xác thực lại"
                )
            login_password_attempts += 1
            if login_password_attempts > _LOGIN_PASSWORD_ATTEMPTS:
                raise RuntimeError("Đăng nhập bằng mật khẩu hiện tại lặp lại thất bại")
            _login_password(driver, item.current_password)
        elif state in {"totp", "totp_invalid"}:
            if not str(item.totp_secret or "").strip():
                raise RuntimeError("Tài khoản bật 2FA: cần totp_secret để tiếp tục")
            totp_attempts += 1
            if totp_attempts > _TOTP_ATTEMPTS:
                raise RuntimeError("Mã 2FA bị từ chối liên tục")
            _submit_existing_account_totp(driver, str(item.totp_secret))
        elif state == "password_invalid":
            raise RuntimeError("Mật khẩu hiện tại bị từ chối")
        else:
            if time.time() > stall_deadline:
                raise RuntimeError(
                    f"Không xác định được trang xác thực sau reauth: state={state}"
                )
            time.sleep(0.4)
            continue
        stall_deadline = time.time() + _CHALLENGE_STALL_TIMEOUT


def _wait_for_new_password_page(driver, timeout: float = _NEW_PASSWORD_PAGE_TIMEOUT) -> None:
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        if _on_new_password_page(driver):
            return
        time.sleep(0.4)
    raise RuntimeError("Không tới trang đặt mật khẩu mới sau reauth")


def _wait_until_left_new_password_page(driver, timeout: float = _NEW_PASSWORD_SUBMIT_WAIT) -> bool:
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        if not _on_new_password_page(driver):
            return True
        time.sleep(0.4)
    return False


def _new_password_body_text(driver) -> str:
    try:
        return str(driver.execute_script("return document.body?.innerText || '';") or "").lower()
    except Exception:  # noqa: BLE001 - body text is best-effort diagnostics.
        return ""


def _fill_new_password_page(driver, password: str, timeout: float = _NEW_PASSWORD_FILL_TIMEOUT) -> bool:
    """Fill every visible password input, submit, and report ``already_set``."""
    end = time.time() + max(10.0, float(timeout))
    while time.time() < end:
        state = driver.execute_script(_NEW_PASSWORD_STATE_SCRIPT) or {}
        inputs = state.get("inputs") or []
        if not inputs:
            time.sleep(0.5)
            continue
        for element in inputs:
            _human_type_text(driver, element, password, clear=True)
        # Let React commit the controlled inputs before enabling Continue.
        human_delay("form", minimum=2.0, maximum=3.6)
        button = state.get("button")
        if button is not None:
            _human_click(driver, button, label="password_change_submit")
        else:
            _click_continue(driver)
        if _wait_until_left_new_password_page(driver):
            return False
        body = _new_password_body_text(driver)
        if "password_already_set" in body:
            return True
        time.sleep(2.0)
    raise RuntimeError("Đổi mật khẩu chưa hoàn tất: trang vẫn còn ở new-password")


def change_password_in_browser(
    driver,
    item: PasswordChangeInput,
    *,
    access_token: str | None = None,
    allow_oauth_fallback: bool = True,
) -> dict[str, object]:
    """Run the reauth password change in one browser; errors come back redacted.

    ``driver`` must already be open; the browser cookie session is authoritative
    (a passed-in bearer token cannot drive the same-origin reauth). ``item``
    must come from :func:`resolve_password_change_input`.
    """
    mode = str(item.mode or "").strip()
    if mode not in PASSWORD_CHANGE_MODES:
        raise ValueError("Cần gọi resolve_password_change_input để xác định mode trước khi chạy")
    new_password = str(item.new_password or "").strip()
    if not new_password:
        raise ValueError("Cần gọi resolve_password_change_input để sinh mật khẩu mới")
    active_access_token = str(access_token or "").strip()
    try:
        # Baseline BEFORE the reauth so a stale mailbox OTP can never be picked.
        try:
            otp_baseline = snapshot_verification_code(item.email, stage="password_change_reauth")
        except Exception as exc:  # noqa: BLE001 - baseline is a stale guard, not a hard dependency.
            logger.warning(
                "[PassChange] không lấy được baseline OTP: %s: %s",
                type(exc).__name__,
                str(exc)[:160],
            )
            otp_baseline = None

        session = _ensure_password_change_session(
            driver,
            item,
            allow_oauth_fallback=allow_oauth_fallback,
        )
        active_access_token = str(session.get("accessToken") or "").strip() or active_access_token

        authorize_url = _fetch_password_change_authorize_url(driver, item.email, mode)
        _safe_get(
            driver,
            authorize_url,
            timeout=45,
            attempts=2,
            accept_hosts=("auth.openai.com", "chatgpt.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="password_change_challenge")
        _maybe_accept(driver)
        _wait_for_browser_challenge(driver, timeout=30)

        _resolve_password_change_challenge(driver, item, otp_baseline)
        _wait_for_new_password_page(driver)
        already_set = _fill_new_password_page(driver, new_password)

        session = _fetch_chatgpt_session(driver, timeout=60)
        final_token = str(session.get("accessToken") or "").strip()
        if not final_token:
            raise RuntimeError("Đổi mật khẩu xong nhưng phiên không còn accessToken")
        result: dict[str, object] = {
            "ok": True,
            "email": item.email,
            "mode": mode,
            "new_password": new_password,
            "access_token": final_token,
        }
        if already_set:
            result["already_set"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - each account must produce a redacted result.
        result = {
            "ok": False,
            "email": item.email,
            "mode": mode,
            "error": _redacted_error(f"{type(exc).__name__}: {exc}", item),
        }
        if active_access_token:
            result["access_token"] = active_access_token
        return result
    finally:
        logout = getattr(driver, "get", None)
        if callable(logout):
            try:
                logout("https://chatgpt.com/auth/logout")
            except Exception:  # noqa: BLE001 - logout is best-effort cleanup.
                logger.debug("ChatGPT logout cleanup failed")
