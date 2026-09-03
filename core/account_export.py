"""
注册后处理模块：
    1. 拉取 /api/auth/session，从中抽取 accessToken / user 信息
    2. 设置 2FA（TOTP），返回 secret
    3. 把账号信息（邮箱 + accessToken + TOTP secret）保存到 SQLite

整体复用注册阶段的 BrowserSession（同一 cookie jar / 同一 IP / 同一 UA），
避免再起新会话被风控关联或缺失登录态。
"""
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

import pyotp

from core.humanize import delay as human_delay
from core.session import BrowserSession
from core.time_utils import local_now

logger = logging.getLogger(__name__)

_TWOFA_ACTION_URL = "https://chatgpt.com/?action=enable&factor=totp"
_TWOFA_REAUTH_MAX_ATTEMPTS = 3
_TWOFA_REAUTH_RETRY_BACKOFF_BASE_SECONDS = 5.0
_TWOFA_REAUTH_RATE_LIMIT_BACKOFF_BASE_SECONDS = 15.0
_TWOFA_REAUTH_RETRY_BACKOFF_MAX_SECONDS = 60.0
_TWOFA_API_MAX_ATTEMPTS = 3
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _PROJECT_ROOT / "accounts"
_BATCH_ARCHIVE_LOCK = threading.RLock()

def _post_register_dwell_seconds() -> float:
    try:
        from config import register as _register_cfg

        raw = str(getattr(_register_cfg, "POST_REGISTER_DWELL_SECONDS_RANGE", "18,45") or "0,0").strip()
    except Exception:  # noqa: BLE001
        raw = "0,0"
    try:
        parts = [float(x.strip()) for x in raw.replace(";", ",").replace("|", ",").split(",") if x.strip()]
        if not parts:
            lo = hi = 0.0
        elif len(parts) == 1:
            lo = hi = parts[0]
        else:
            lo, hi = parts[0], parts[1]
    except Exception:  # noqa: BLE001
        lo = hi = 0.0
    lo, hi = max(0.0, lo), max(0.0, hi)
    if hi < lo:
        lo, hi = hi, lo
    seconds = random.uniform(lo, hi) if hi > lo else lo
    return max(0.0, min(300.0, seconds))


def post_register_dwell(email: str, *, label: str = "注册后") -> None:
    """注册成功后随机停留一段时间；供不同浏览器驱动复用。"""
    seconds = _post_register_dwell_seconds()
    if seconds <= 0:
        return
    logger.info("[%s] 注册成功后随机停留 %.1fs：%s", label, seconds, email)
    time.sleep(seconds)


class _BrowserResponse:
    def __init__(self, response):
        self.status_code = int(getattr(response, "status", 0) or 0)
        text = getattr(response, "text", "")
        self.text = text() if callable(text) else str(text or "")
        self._response = response
        self.url = str(getattr(response, "url", "") or "")

    def json(self):
        return self._response.json()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:500]}")


class _ScriptResponse:
    def __init__(self, payload: dict):
        self.status_code = int(payload.get("status") or 0)
        self.text = str(payload.get("text") or "")
        self.url = str(payload.get("url") or "")
        self._data = payload.get("json")

    def json(self):
        if self._data is not None:
            return self._data
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:500]}")


def _json_object_response(response, *, action: str) -> dict:
    """Decode a browser API response without exposing its body in errors."""
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{action} response is not JSON: "
            f"status={getattr(response, 'status_code', 0)} "
            f"url={str(getattr(response, 'url', '') or '')[:160]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(  # noqa: TRY004 - public transport errors preserve the existing API contract.
            f"{action} response is not a JSON object: "
            f"status={getattr(response, 'status_code', 0)}"
        )
    return payload


class BrowserPageTransport:
    """用 Selenium 页面 fetch 执行同源 API 请求，保持当前浏览器登录态。"""

    def __init__(self, driver):
        self.driver = driver
        self.device_id = ""
        try:
            cookie = self.driver.get_cookie("oai-did") or {}
            self.device_id = str(cookie.get("value") or "")
        except Exception:  # noqa: BLE001, S110
            pass

    def navigator_language(self) -> str:
        try:
            return str(self.driver.execute_script("return navigator.language") or "en-US")
        except Exception:  # noqa: BLE001
            return "en-US"

    def js_timezone_offset_min(self) -> int:
        try:
            return int(self.driver.execute_script("return new Date().getTimezoneOffset()") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _request(self, method: str, url: str, headers: dict | None = None, data: str | None = None):
        current_url = str(getattr(self.driver, "current_url", "") or "")
        if current_url and urlparse(current_url).netloc != urlparse(url).netloc:
            self.driver.get(url)
        script = """
        const done = arguments[arguments.length - 1];
        const method = arguments[0];
        const url = arguments[1];
        const headers = arguments[2] || {};
        const body = arguments[3];
        fetch(url, {method, headers, body, credentials: 'include', redirect: 'follow'})
          .then(async response => {
            const text = await response.text();
            let json = null;
            try { json = text ? JSON.parse(text) : null; } catch (_) {}
            done({status: response.status, url: response.url, text, json});
          })
          .catch(error => done({status: 0, url, text: String(error), json: null}));
        """
        timeout = max(1, int(getattr(self.driver, "script_timeout", 30) or 30))
        set_timeout = getattr(self.driver, "set_script_timeout", None)
        previous_timeout = None
        if callable(set_timeout):
            try:
                previous_timeout = getattr(self.driver, "script_timeout", None)
                set_timeout(timeout)
            except Exception:  # noqa: BLE001
                previous_timeout = None
        try:
            payload = self.driver.execute_async_script(script, method, url, headers or {}, data)
        finally:
            if callable(set_timeout) and previous_timeout is not None:
                try:
                    set_timeout(previous_timeout)
                except Exception:  # noqa: BLE001, S110
                    pass
        return _ScriptResponse(payload or {"status": 0, "text": "browser request returned no response"})

    def get(self, url: str, headers: dict | None = None, **kwargs):
        return self._request("GET", url, headers=headers)

    def post(self, url: str, headers: dict | None = None, **kwargs):
        return self._request("POST", url, headers=headers, data=kwargs.get("data"))

    def get_nextauth_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "*/*", "content-type": "application/json", "referer": referer}

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> dict:
        return {"accept": "*/*", "content-type": "application/json", "referer": referer}

    def get_auth_headers(self, referer: str = "https://auth.openai.com/create-account/password") -> dict:
        return {"accept": "application/json", "content-type": "application/json", "origin": "https://auth.openai.com", "referer": referer}

    def get_auth_navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "text/html,application/xhtml+xml", "referer": referer}

    def get_chatgpt_navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "text/html,application/xhtml+xml", "referer": referer}

    def navigate(self, url: str, referer: str | None = None) -> None:
        self.driver.get(url)


class BrowserSessionTransport(Protocol):
    """Minimal transport contract shared by Selenium and Playwright adapters."""

    def get_nextauth_headers(self, referer: str = "https://chatgpt.com/") -> dict: ...

    def get(self, url: str, headers: Any = None, **kwargs): ...


class BrowserContextTransport:
    """用已登录 Playwright context 执行同源 API 请求，保持原浏览器 Cookie。"""

    def __init__(self, context, page):
        self.context = context
        self.page = page
        self.device_id = self._read_cookie("oai-did") or ""

    def _read_cookie(self, name: str) -> str:
        try:
            cookies = self.context.cookies(["https://chatgpt.com", "https://auth.openai.com"])
            for cookie in cookies:
                if cookie.get("name") == name:
                    return str(cookie.get("value") or "")
        except Exception:  # noqa: BLE001, S110
            pass
        return ""

    def navigator_language(self) -> str:
        try:
            return str(self.page.evaluate("() => navigator.language") or "en-US")
        except Exception:  # noqa: BLE001
            return "en-US"

    def js_timezone_offset_min(self) -> int:
        try:
            return int(self.page.evaluate("() => new Date().getTimezoneOffset()") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _headers(self, headers: dict | None) -> dict:
        out = dict(headers or {})
        out.setdefault("accept", "*/*")
        out.setdefault("origin", "https://chatgpt.com")
        out.setdefault("user-agent", "Mozilla/5.0")
        if self.device_id:
            out.setdefault("oai-device-id", self.device_id)
        return out

    def get(self, url: str, headers: dict | None = None, **kwargs):
        response = self.context.request.get(
            url,
            headers=self._headers(headers),
            timeout=int(float(kwargs.get("timeout", 30)) * 1000),
            fail_on_status_code=False,
        )
        return _BrowserResponse(response)

    def post(self, url: str, headers: dict | None = None, **kwargs):
        response = self.context.request.post(
            url,
            headers=self._headers(headers),
            data=kwargs.get("data"),
            timeout=int(float(kwargs.get("timeout", 30)) * 1000),
            fail_on_status_code=False,
        )
        return _BrowserResponse(response)

    def get_nextauth_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "*/*", "content-type": "application/json", "referer": referer}

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> dict:
        return {"accept": "*/*", "content-type": "application/json", "referer": referer}

    def get_auth_headers(self, referer: str = "https://auth.openai.com/create-account/password") -> dict:
        return {"accept": "application/json", "content-type": "application/json", "origin": "https://auth.openai.com", "referer": referer}

    def get_auth_navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "text/html,application/xhtml+xml", "referer": referer}

    def get_chatgpt_navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {"accept": "text/html,application/xhtml+xml", "referer": referer}

    def navigate(self, url: str, referer: str | None = None) -> None:
        self.page.goto(url, wait_until="domcontentloaded")


def setup_2fa_in_browser(context, page, email: str, *, reauth: bool = False) -> str:
    """在现有 Playwright 登录态中执行 2FA 设置。"""
    return setup_2fa(BrowserContextTransport(context, page), email, reauth=reauth)


def setup_2fa_in_page(driver, email: str, *, reauth: bool = False) -> str:
    """在现有 Selenium 登录态中执行 2FA 设置。"""
    return setup_2fa(BrowserPageTransport(driver), email, reauth=reauth)


def _account_material_line(email: str, row: dict | None = None) -> str:
    """优先输出 Outlook 原始素材；没有素材时退回邮箱地址。"""
    if row:
        base = row.get("original_email_line") or row.get("email") or email
        password = ""
        if isinstance(row.get("extra_json"), str) and row.get("extra_json"):
            try:
                extra = json.loads(str(row.get("extra_json") or ""))
                password = str(extra.get("registration_password") or "").strip()
            except Exception:  # noqa: BLE001
                password = ""
        if not password:
            password = str(row.get("password") or "").strip()
        parts = [p for p in str(base or "").split("----") if p != ""]
        if password:
            if not parts:
                base = password
            elif len(parts) == 1:
                if parts[0] != password:
                    parts.insert(1, password)
                base = "----".join(parts)
            elif parts[1] != password:
                looks_like_material = (
                    parts[1].startswith("M.")
                    or parts[1].startswith("m.")
                    or (len(parts[1]) >= 32 and parts[1].count("-") >= 4)
                    or any(ch in parts[1] for ch in ("@", ":", "/", "\\"))
                )
                if looks_like_material:
                    parts.insert(1, password)
                    base = "----".join(parts)
        return base
    return email


def _account_copy_line(
    material_line: str,
    access_token: str,
    gpt_password: str | None = None,
    totp_secret: str | None = None,
) -> str:
    """生成包含 token 的整行归档，方便从批次汇总文件里复制。"""
    parts = [material_line, access_token]
    if gpt_password or totp_secret:
        parts.append(str(gpt_password or ""))
    if totp_secret:
        parts.append(totp_secret)
    return "----".join(parts)


def create_batch_archive_dir(count: int, workers: int = 1) -> Path:
    """为一次运行创建只读导出批次目录；SQLite 仍是唯一运行时 source of truth。"""
    day = local_now().strftime("%Y%m%d")
    base_name = f"{day}-{count}个" if workers <= 1 else f"{day}-{count}个-{workers}线程"
    folder = _ACCOUNTS_DIR / base_name
    suffix = 2
    while folder.exists():
        folder = _ACCOUNTS_DIR / f"{base_name}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "注册成功的邮箱.txt").write_text("", encoding="utf-8")
    (folder / "注册成功的token.txt").write_text("", encoding="utf-8")
    (folder / "注册成功整行.txt").write_text("", encoding="utf-8")
    (folder / "注册成功账号.json").write_text("[]\n", encoding="utf-8")
    return folder


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")


def _append_batch_archive(
    *,
    row_id: int,
    email: str,
    access_token: str,
    totp_secret: str | None,
    email_source: str | None,
    proxy_used: str | None,
    extra: dict,
    batch_dir: Path | None,
) -> Path:
    """将 SQLite 中已保存的账号同步到本次批次的兼容导出目录。"""
    from core import db

    folder = batch_dir or create_batch_archive_dir(count=1)
    row = db.get_account(row_id) or {}
    material_line = _account_material_line(email, row)
    copy_line = db.account_line(row, "modern")
    archive_extra = extra
    if isinstance(row.get("extra_json"), str) and row.get("extra_json"):
        try:
            parsed_extra = json.loads(str(row["extra_json"]))
            if isinstance(parsed_extra, dict):
                archive_extra = parsed_extra
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    archive = {
        "id": row_id,
        "email": email,
        "email_source": email_source,
        "proxy_used": proxy_used,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "material_line": material_line,
        "copy_line": copy_line,
        "saved_at": local_now().isoformat(timespec="seconds"),
        "row": row,
        "extra": archive_extra,
    }
    with _BATCH_ARCHIVE_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        _append_line(folder / "注册成功的邮箱.txt", material_line)
        _append_line(folder / "注册成功的token.txt", access_token)
        _append_line(folder / "注册成功整行.txt", copy_line)
        json_path = folder / "注册成功账号.json"
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else []
        except (TypeError, ValueError, json.JSONDecodeError):
            rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(archive)
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    """
    步骤12.5: 跟随 create_account 返回的 continue_url，完成 OAuth 回调。

    create_account 成功后返回的 continue_url 一般指向
        https://auth.openai.com/authorize/continue?...
    它会再 302 到
        https://chatgpt.com/api/auth/callback/openai?code=...&state=...
    回调请求会让 chatgpt.com 设置 `__Secure-next-auth.session-token` cookie，
    之后 /api/auth/session 才能返回 accessToken。

    Returns:
        重定向链最终落点 URL（一般是 chatgpt.com 站内地址）
    """
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")

    # continue_url 通常是 auth.openai.com/authorize/continue；
    # OTP 后 external_url 分支也可能直接给 chatgpt.com 回调地址。
    # 按目标域名选择导航头，避免 auth step 正确但请求头语义不一致。
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)

    logger.info("[OAuth回调] 跟随 continue_url 完成 OAuth 回调...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    logger.info(f"[OAuth回调] 完成, 最终落点: {resp.url}")
    return resp.url


def fetch_session(session: BrowserSessionTransport) -> dict:
    """
    GET https://chatgpt.com/api/auth/session
    注册成功后立刻调用，拿到 accessToken / user / account / expires。

    Returns:
        完整 session JSON，包含字段:
            - accessToken: str (Bearer token, 用于 backend-api 调用)
            - user: {id, name, email, idp, iat, mfa}
            - account: {id, planType, structure, ...}
            - expires: ISO 时间字符串
    """
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")

    logger.info("[Session] 拉取 ChatGPT session 信息...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("accessToken"):
        logger.error(f"[Session] 响应中没有 accessToken: {data}")
        raise RuntimeError("未拿到 accessToken，登录态可能未建立")

    user = data.get("user") or {}
    account = data.get("account") or {}
    logger.info(
        f"[Session] 成功，user_id={user.get('id')}, email={user.get('email')}, "
        f"plan={account.get('planType')}, mfa={user.get('mfa')}"
    )
    return data


def _trigger_reauth(session: BrowserSession, email: str) -> str:
    """为当前已认证 session 发起 2FA 重认证，返回 authorize URL。"""
    csrf_url = "https://chatgpt.com/api/auth/csrf"
    csrf_resp = session.get(csrf_url, headers=session.get_nextauth_headers(referer="https://chatgpt.com/"))
    csrf_resp.raise_for_status()
    csrf_data = _json_object_response(csrf_resp, action="re-auth CSRF")
    csrf_token = csrf_data.get("csrfToken")
    if not csrf_token:
        raise RuntimeError("re-auth CSRF response missing csrfToken")

    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"
    body = urlencode({
        "callbackUrl": _TWOFA_ACTION_URL,
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info("[2FA] 当前 session 发起 re-auth...")
    response = session.post(signin_url, headers=headers, data=body)
    response.raise_for_status()
    auth_data = _json_object_response(response, action="re-auth signin")
    auth_url = auth_data.get("url")
    if not auth_url:
        raise RuntimeError(f"未拿到 reauth authorize URL: {response.text}")
    return auth_url


def _follow_reauth(session: BrowserSession, auth_url: str) -> str:
    """跟随 re-auth URL，等待认证页自身触发邮箱 OTP。"""
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    response = session.get(auth_url, headers=headers, allow_redirects=True)
    final_url = str(getattr(response, "url", "") or "")
    lower_url = final_url.lower()
    logger.info("[2FA] re-auth OTP 页面: %s", final_url)
    if "/log-in/password" in lower_url:
        raise RuntimeError(
            "re-auth 未进入 email-verification 页面，当前落在登录密码页"
        )
    if "email-verification" not in lower_url:
        raise RuntimeError(
            f"re-auth 未进入 email-verification 页面: {final_url[:180]}"
        )
    return final_url


def _validate_reauth_otp(session: BrowserSession, code: str) -> str:
    """提交已保存账号的 re-auth 邮箱 OTP。"""
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    response = session.post(url, headers=headers, data=json.dumps({"code": code}))
    response.raise_for_status()
    response_data = _json_object_response(response, action="re-auth OTP validate")
    continue_url = response_data.get("continue_url")
    if not continue_url:
        raise RuntimeError(f"OTP 验证响应缺少 continue_url: {response.text}")
    return continue_url


def _is_wrong_email_otp_error(exc: Exception) -> bool:
    """判断是否是可通过重新登录并获取新码恢复的邮箱 OTP 错误。"""
    error_text = str(exc).lower()
    return "wrong_email_otp_code" in error_text or "wrong code" in error_text


def _is_reauth_retryable_error(exc: Exception) -> bool:
    """判断 re-auth 是否应重新建立认证步骤并重新获取邮箱 OTP。"""
    if _is_wrong_email_otp_error(exc):
        return True
    try:
        if int(getattr(exc, "status_code", 0) or 0) == 429:
            return True
    except (TypeError, ValueError):
        pass
    error_text = str(exc).lower()
    if re.search(r"\bhttp\s+429\b", error_text):
        return True
    return any(
        marker in error_text
        for marker in (
            "invalid_auth_step",
            "invalid authorization step",
            "rate_limit_exceeded",
            "rate limit exceeded",
            "未进入 email-verification",
            "waiting for new otp",
            "response is not json",
            "response is not a json object",
            "missing csrftoken",
            "missing csrf token",
        )
    )


def _is_reauth_rate_limit_error(exc: Exception) -> bool:
    """Return whether the auth provider explicitly rate-limited the retry."""
    error_text = str(exc).lower()
    return "rate_limit_exceeded" in error_text or "rate limit exceeded" in error_text


def _reauth_retry_backoff_seconds(exc: Exception, attempt: int) -> float:
    """Calculate the bounded delay before the next re-auth attempt."""
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    base = (
        _TWOFA_REAUTH_RATE_LIMIT_BACKOFF_BASE_SECONDS
        if _is_reauth_rate_limit_error(exc)
        else _TWOFA_REAUTH_RETRY_BACKOFF_BASE_SECONDS
    )
    return min(base * (2 ** (attempt - 1)), _TWOFA_REAUTH_RETRY_BACKOFF_MAX_SECONDS)


def _reset_reauth_context(session: BrowserSessionTransport) -> None:
    """Return the live session to ChatGPT before starting a fresh auth step."""
    navigate = getattr(session, "navigate", None)
    if callable(navigate):
        navigate("https://chatgpt.com/")
        return

    headers = session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/")
    response = session.get(
        "https://chatgpt.com/",
        headers=headers,
        allow_redirects=True,
    )
    response.raise_for_status()


def _resend_reauth_otp(session: BrowserSessionTransport) -> None:
    """Ask auth.openai.com for a fresh OTP after a failed re-auth attempt."""
    from core.openai_auth import send_email_otp

    send_email_otp(
        session,
        referer="https://auth.openai.com/email-verification",
    )


def _exchange_new_token(session: BrowserSession, continue_url: str) -> str:
    """完成 re-auth 回调并取回带新认证时间的 access token。"""
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    session.get(continue_url, headers=headers, allow_redirects=True)
    new_token = fetch_session(session)["accessToken"]
    logger.info("[2FA] re-auth 完成，刷新 accessToken")
    return new_token


def _open_twofa_action(session: BrowserSession) -> None:
    """在已登录的注册会话中打开首页 2FA action，不重新进入登录流程。"""
    logger.info("[2FA] 在当前登录态打开首页 2FA action...")
    navigate = getattr(session, "navigate", None)
    if callable(navigate):
        navigate(_TWOFA_ACTION_URL, referer="https://chatgpt.com/")
        return

    headers = session.get_chatgpt_navigate_headers(referer="https://chatgpt.com/")
    response = session.get(_TWOFA_ACTION_URL, headers=headers, allow_redirects=True)
    response.raise_for_status()


def _enroll_totp(session: BrowserSession, access_token: str) -> tuple[str, str]:
    """
    步骤6: 注册 TOTP，返回 (secret, session_id)
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    body = json.dumps({"factor_type": "totp"})

    logger.info("[2FA] 注册 TOTP...")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[2FA] enroll 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    secret = data.get("secret")
    session_id = data.get("session_id")
    if not secret or not session_id:
        raise RuntimeError(f"enroll 响应字段缺失: {data}")
    logger.info(f"[2FA] TOTP secret 已获取: {secret[:4]}...{secret[-4:]}")
    return secret, session_id


def _activate_totp(
    session: BrowserSession,
    access_token: str,
    secret: str,
    session_id: str,
) -> bool:
    """
    步骤7: 用 secret 生成 6 位 TOTP 码，激活 2FA。
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    totp_code = pyotp.TOTP(secret).now()
    body = json.dumps({
        "code": totp_code,
        "factor_type": "totp",
        "session_id": session_id,
    })

    logger.info(f"[2FA] 激活 enrollment, code={totp_code}")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[2FA] activate 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"激活返回 success=false: {data}")
    return True


def _is_transient_twofa_error(exc: Exception) -> bool:
    """Return whether a 2FA API failure can recover on a repeated request."""
    if isinstance(exc, TimeoutError):
        return True
    status_code = int(getattr(exc, "status_code", 0) or 0)
    if 500 <= status_code <= 599:
        return True
    message = str(exc or "").lower()
    if re.search(r"\bhttp\s+5\d{2}\b", message):
        return True
    return any(
        marker in message
        for marker in (
            "request timeout",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
            "service unavailable",
            "connection reset",
            "connection aborted",
        )
    )


def _run_twofa_api_with_retry(action: str, operation):
    """Retry only transient enroll/activate transport failures."""
    for attempt in range(1, _TWOFA_API_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_transient_twofa_error(exc) or attempt >= _TWOFA_API_MAX_ATTEMPTS:
                raise
            backoff = min(1.0 * (2 ** (attempt - 1)), 4.0)
            logger.warning(
                "[2FA] %s 暂时失败，第 %s/%s 次后将在 %.1fs 后重试：%s: %s",
                action,
                attempt,
                _TWOFA_API_MAX_ATTEMPTS,
                backoff,
                type(exc).__name__,
                str(exc)[:180],
            )
            human_delay("api", minimum=backoff, maximum=backoff)
    raise RuntimeError(f"2FA {action} retry loop exited unexpectedly")


def setup_2fa(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    access_token: str | None = None,
    *,
    reauth: bool = False,
) -> str:
    """
    为账号设置 2FA。

    默认复用当前登录态完成 enroll/activate。需要 recent-auth 的流程通过
    ``reauth=True`` 显式执行邮箱 OTP re-auth；注册流程使用
    :func:`setup_2fa_for_registration`，在 enroll 前强制完成 re-auth。

    Args:
        session: 已完成注册的会话
        email: 账号邮箱
        reauth: 是否先执行 re-auth 邮箱 OTP 流程

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    from config import email as _email_cfg
    from core.chatgpt_bootstrap import authenticated_bootstrap
    logger.info("=" * 60)
    logger.info("开始设置 2FA：%s", email)
    logger.info("=" * 60)

    if access_token:
        try:
            logger.info("[2FA] 使用现有 accessToken 预热登录态...")
            authenticated_bootstrap(session, access_token, strict=False)
            human_delay("navigate")
            logger.info("[2FA] accessToken 预热完成")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[2FA] accessToken 预热失败，继续按当前登录态执行：%s: %s", type(exc).__name__, str(exc)[:180])

    if reauth:
        from config import email as _email_cfg
        from core.email_provider import (
            acknowledge_verification_code,
            snapshot_verification_code,
        )

        previous_submitted_otp = None
        continue_url = None
        for reauth_attempt in range(1, _TWOFA_REAUTH_MAX_ATTEMPTS + 1):
            reauth_before_code = snapshot_verification_code(
                email,
                stage="twofa_reauth_request",
            ) or previous_submitted_otp
            reauth_otp_after_ts = time.time()
            current_otp = otp_code if reauth_attempt == 1 else None
            try:
                auth_url = _trigger_reauth(session, email)
                human_delay("api")
                _follow_reauth(session, auth_url)
                human_delay("navigate")
                if reauth_attempt > 1:
                    # A failed code invalidates the previous auth step; request
                    # the next code only after rebuilding the auth page.
                    _resend_reauth_otp(session)
                if _email_cfg.USE_EMAIL_SERVICE:
                    from core.email_provider import wait_for_otp

                    logger.info(
                        "[2FA] 等待 re-auth 邮箱 OTP（第 %s/%s 次）",
                        reauth_attempt,
                        _TWOFA_REAUTH_MAX_ATTEMPTS,
                    )
                    wait_kwargs = {
                        "after_ts": reauth_otp_after_ts,
                        "before_code": reauth_before_code,
                        "stage": "twofa_reauth_email_otp",
                    }
                    current_otp = wait_for_otp(email, **wait_kwargs)
                else:
                    current_otp = current_otp or input(">>> 2FA 验证码: ").strip()
                logger.info(
                    "[2FA] re-auth OTP 已返回，提交校验（第 %s/%s 次）",
                    reauth_attempt,
                    _TWOFA_REAUTH_MAX_ATTEMPTS,
                )
                human_delay("otp_input")
                continue_url = _validate_reauth_otp(session, current_otp)
                acknowledge_verification_code(
                    email,
                    current_otp,
                    stage="twofa_reauth_email_otp",
                )
                logger.info("[2FA] re-auth OTP 校验通过")
            except Exception as exc:
                if not _is_reauth_retryable_error(exc) or reauth_attempt >= _TWOFA_REAUTH_MAX_ATTEMPTS:
                    raise
                if current_otp:
                    previous_submitted_otp = current_otp
                backoff = _reauth_retry_backoff_seconds(exc, reauth_attempt)
                logger.warning(
                    "[2FA] re-auth 步骤失败，第 %s/%s 次，%.1fs 后重新登录并获取新验证码：%s",
                    reauth_attempt,
                    _TWOFA_REAUTH_MAX_ATTEMPTS,
                    backoff,
                    str(exc)[:180],
                )
                try:
                    _reset_reauth_context(session)
                except Exception as reset_exc:  # noqa: BLE001
                    logger.warning(
                        "[2FA] re-auth retry session reset failed: %s: %s",
                        type(reset_exc).__name__,
                        str(reset_exc)[:160],
                    )
                backoff_max = min(backoff * 1.25, _TWOFA_REAUTH_RETRY_BACKOFF_MAX_SECONDS)
                human_delay("api", minimum=backoff, maximum=backoff_max)
                continue
            break
        if not continue_url:
            raise RuntimeError("re-auth 未返回 continue_url")
        human_delay("api")
        access_token = _exchange_new_token(session, continue_url)
    else:
        if access_token:
            logger.info("[2FA] 复用现有登录态打开 2FA 页面")
        else:
            logger.info("[2FA] 使用当前登录态打开 2FA 页面")
        _open_twofa_action(session)
        human_delay("navigate")
        home_session = fetch_session(session)
        access_token = home_session["accessToken"]
    human_delay("api")

    secret, session_id = _run_twofa_api_with_retry(
        "enroll",
        lambda: _enroll_totp(session, access_token),
    )
    human_delay("form")
    _run_twofa_api_with_retry(
        "activate",
        lambda: _activate_totp(session, access_token, secret, session_id),
    )

    logger.info("=" * 60)
    logger.info(f"✅ 2FA 设置完成! Secret: {secret[:4]}...{secret[-4:]}")
    logger.info("=" * 60)
    return secret


def setup_2fa_for_registration(session: BrowserSession, email: str) -> str:
    """Enable MFA after the fresh signup session completes an email re-auth."""
    if not callable(getattr(session, "get_auth_headers", None)) and callable(
        getattr(session, "execute_async_script", None)
    ):
        session = BrowserPageTransport(session)
    logger.info("[2FA] 注册完成，先完成邮箱 re-auth 再 enroll TOTP...")
    return setup_2fa(session, email, reauth=True)


def checkpoint_account_data(
    email: str,
    access_token: str,
    extra: dict | None = None,
    *,
    email_source: str | None = None,
    proxy_used: str | None = None,
    registration_ip: str | None = None,
) -> int:
    """在 2FA 前保存已创建账号的最小可恢复状态。"""
    from core.db import insert_account

    payload = dict(extra or {})
    from core.account_locale import derive_account_locale

    locale_fields = derive_account_locale(extra=payload)
    user = payload.get("user") or {}
    account = payload.get("account") or {}
    source_cdk = None
    if email_source == "gmail_123452026":
        from core.gmail_123452026_client import get_account_context

        context = get_account_context(email)
        source_cdk = context.cdk if context is not None else None
    elif email_source == "paymesh":
        from core.paymesh_mail_client import get_account_context

        context = get_account_context(email)
        source_cdk = context.cdk if context is not None else None

    row_id = insert_account(
        email=email,
        access_token=access_token,
        user_id=user.get("id"),
        user_name=user.get("name"),
        plan_type=account.get("planType"),
        expires_at=payload.get("expires"),
        device_id=payload.get("device_id"),
        proxy_used=proxy_used,
        registration_ip=registration_ip,
        account_locale=locale_fields["account_locale"],
        account_country=locale_fields["account_country"],
        account_locale_source=locale_fields["account_locale_source"],
        email_source=email_source,
        source_cdk=source_cdk,
        registration_password=str(payload.get("registration_password") or ""),
        twofa_status="pending",
        twofa_error=None,
        extra={**payload, "twofa_status": "pending", "twofa_error": None},
    )
    # The API-URL alias is already consumed at this checkpoint even if the
    # later 2FA step needs a retry. Other providers keep their existing flow.
    if email_source == "gmail_api_url":
        from core.email_provider import mark_email_consumed

        mark_email_consumed(email)
    return row_id


def save_account_data(
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    extra: dict | None = None,
    output_path: Path | None = None,  # 兼容老接口，已废弃
    email_source: str | None = None,
    proxy_used: str | None = None,
    registration_ip: str | None = None,
    batch_dir: Path | None = None,
    auto_plan_check: bool | None = None,
) -> int:
    """
    将账号信息保存到 SQLite；output_path 仅为兼容旧调用方保留。
    返回新插入/更新的 row id。
    """
    from core.db import insert_account
    extra = dict(extra or {})
    # Remail service token 只存在进程内上下文中；注册成功后持久化订单上下文，
    # 让服务重启后的查活流程可以恢复取件凭证。
    if str(email_source or "").strip().lower() == "remail":
        try:
            from core.remail_client import get_account_context_metadata

            remail_metadata = get_account_context_metadata(email)
            if remail_metadata:
                existing_service = extra.get("email_service")
                merged_service = dict(existing_service) if isinstance(existing_service, dict) else {}
                merged_service.update(remail_metadata)
                extra["email_service"] = merged_service
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Save] 保存 Remail 订单上下文失败，后续将尝试按邮箱恢复：%s: %s",
                type(exc).__name__,
                str(exc)[:180],
            )
    from core.account_locale import derive_account_locale

    locale_fields = derive_account_locale(extra=extra)
    user = extra.get("user") or {}
    account = extra.get("account") or {}
    # 从 extra.codex 抽出顶层 codex 状态/错误，方便 WebUI 直接读账号字段
    codex = extra.get("codex") or {}
    codex_status = codex.get("status")  # success / failed / skipped
    codex_error = None
    if codex_status == "failed":
        codex_error = codex.get("message")

    source_cdk = None
    if email_source == "gmail_123452026":
        from core.gmail_123452026_client import get_account_context

        gmail_context = get_account_context(email)
        source_cdk = gmail_context.cdk if gmail_context is not None else None
    elif email_source == "paymesh":
        from core.paymesh_mail_client import get_account_context

        paymesh_context = get_account_context(email)
        source_cdk = paymesh_context.cdk if paymesh_context is not None else None

    row_id = insert_account(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        user_id=user.get("id"),
        user_name=user.get("name"),
        plan_type=account.get("planType"),
        expires_at=extra.get("expires"),
        proxy_used=proxy_used,
        registration_ip=registration_ip,
        account_locale=locale_fields["account_locale"],
        account_country=locale_fields["account_country"],
        account_locale_source=locale_fields["account_locale_source"],
        email_source=email_source,
        source_cdk=source_cdk,
        registration_password=str(extra.get("registration_password") or ""),
        twofa_status=str(extra.get("twofa_status") or ("active" if totp_secret else "disabled")),
        twofa_error=extra.get("twofa_error"),
        extra=extra,
        codex_status=codex_status,
        codex_error=codex_error,
    )
    from core.email_provider import mark_email_consumed
    mark_email_consumed(email)
    _append_batch_archive(
        row_id=row_id,
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        email_source=email_source,
        proxy_used=proxy_used,
        extra=extra,
        batch_dir=batch_dir,
    )
    logger.info("[Save] 账号及凭证已保存到 SQLite, id=%s, email=%s", row_id, email)

    auto_twofa = False
    try:
        from config import twofa as _twofa_cfg

        auto_twofa = bool(getattr(_twofa_cfg, "ENABLE_2FA", False))
    except Exception:  # noqa: BLE001
        auto_twofa = False
    if auto_twofa and not str(totp_secret or "").strip():
        try:
            from core.twofa_service import enqueue_account_totp_setup

            queued = enqueue_account_totp_setup(
                account_id=row_id,
                email=email,
                access_token=access_token,
                trigger="registration_auto",
                proxy=proxy_used,
            )
            if queued.get("accepted"):
                logger.info(f"[2FA] 注册后自动开启 2FA 已入队: id={row_id}, email={email}")
            elif queued.get("busy"):
                logger.info(f"[2FA] 账号已有 2FA 任务，注册流程不重复入队: id={row_id}, email={email}")
            else:
                logger.warning(f"[2FA] 注册后自动开启 2FA 入队失败（不影响注册结果）: {email}, {queued.get('error')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[2FA] 注册后自动开启 2FA 入队异常（不影响注册结果）: "
                f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
            )

    if auto_plan_check is None:
        try:
            from config import register as _register_cfg

            auto_plan_check = bool(
                getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
                or getattr(_register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
            )
        except Exception:  # noqa: BLE001
            auto_plan_check = False
    if not auto_plan_check:
        logger.info(
            f"[Plan] 注册后不创建异步套餐查询任务（当前流程可能已同步完成或配置已关闭）: "
            f"id={row_id}, email={email}"
        )
        return row_id
    # session 中的 account.planType 不能说明 Plus 试用资格。账号落库后只负责
    # 入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=row_id,
            email=email,
            access_token=access_token,
            trigger="registration_auto",
        )
        if queued.get("accepted"):
            logger.info(f"[Plan] 注册后自动查询已入队: id={row_id}, email={email}")
        elif queued.get("busy"):
            logger.info(f"[Plan] 账号已有套餐查询，注册流程不重复入队: id={row_id}, email={email}")
        else:
            logger.warning(f"[Plan] 注册后自动查询入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
