"""查活浏览器兜底：Roxy 指纹浏览器登录刷新 AT，不依赖出口 IP 干净。

协议链（curl_cffi）的 NextAuth 入口在出口 IP 被 Cloudflare 拦截时是硬墙；
真实浏览器能完成 CF JS 质解并走完整登录（密码 → MFA/邮箱 OTP），再从页面
内读取 /api/auth/session 的 accessToken。本模块只在协议链 403 兜底时调用。
"""
import logging
import time

from core.browser_page_actions import (
    _clear_otp_inputs,
    _click_continue,
    _email_otp_page_state,
    _is_email_verification_page,
    _maybe_accept,
    _page_warmup,
    _safe_get,
    _type_otp,
    _wait_after_email_otp_submit,
)
from core.browser_selenium_adapter import build_selenium_driver as _build_driver
from core.email_provider import wait_for_otp
from core.humanize import delay as human_delay
from core.registration_flow import (
    _fetch_chatgpt_session,
    _submit_email_step,
    _type_email_address,
)
from core.roxy_codex_oauth import (
    _fill_login_password_if_present,
    _fill_mfa_challenge_if_present,
    _is_mfa_challenge_page,
    _wait_for_otp_input,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://chatgpt.com/auth/login"
_OTP_MAX_ATTEMPTS = 3


def _submit_email_otp(driver, email: str, *, email_source: str | None, otp_after_ts: float) -> None:
    """等待并提交邮箱 OTP；失败时重发并重取，必须传账号注册时的邮箱来源。"""
    current_otp: str | None = None
    previous_submitted: str | None = None
    after_ts = otp_after_ts
    for attempt in range(1, _OTP_MAX_ATTEMPTS + 1):
        if current_otp is None:
            logger.info("[查活][Browser] 等待邮箱 OTP：%s（第 %s/%s 次）", email, attempt, _OTP_MAX_ATTEMPTS)
            kwargs = {"after_ts": after_ts, "email_source": email_source}
            if previous_submitted:
                kwargs["before_code"] = previous_submitted
            current_otp = wait_for_otp(email, **kwargs)
        previous_submitted = current_otp
        logger.info("[查活][Browser] 已收到邮箱 OTP，填写并提交")
        _wait_for_otp_input(driver, timeout=30)
        _clear_otp_inputs(driver)
        _type_otp(driver, current_otp)
        human_delay("otp_input")
        _click_continue(driver)
        if _wait_after_submit(driver) == "accepted":
            return
        if attempt >= _OTP_MAX_ATTEMPTS:
            raise RuntimeError("邮箱验证码连续错误/过期，浏览器登录失败")
        logger.warning("[查活][Browser] 验证码无效/过期，重发后重取（%s/%s）", attempt + 1, _OTP_MAX_ATTEMPTS)
        after_ts = time.time()
        from core.registration_flow import _resend_or_restart_email_otp

        _resend_or_restart_email_otp(driver, email)
        human_delay("api")
        current_otp = None


def _wait_after_submit(driver, timeout: int = 45) -> str:
    """OTP 提交后等待离开验证码页；无错误标记的超时按已接受处理。"""
    outcome = _wait_after_email_otp_submit(driver, timeout=timeout)
    if outcome == "invalid" and not _is_email_verification_page(driver):
        return "accepted"
    if outcome != "invalid":
        return outcome
    state = _email_otp_page_state(driver)
    has_error = bool(state.get("errors")) or any(
        str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or [])
    )
    return "invalid" if has_error else "accepted"


def browser_refresh_session(email: str, *, email_source: str | None = None) -> dict:
    """Roxy 浏览器登录并读取最新 session/accessToken。

    浏览器走本机网络（不经代理池出口）：目标是让真实浏览器解 CF 质解，
    与 IP 是否被代理池污染无关。返回结构对齐 check_account_liveness。
    """
    from config import roxybrowser as _roxy_cfg

    client = RoxyBrowserClient()
    opened = client.open_profile()
    driver = None
    try:
        driver = _build_driver(opened)
        driver.set_page_load_timeout(int(_roxy_cfg.ROXY_SELENIUM_TIMEOUT))
        logger.info("[查活][Browser] 开始浏览器登录：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        _safe_get(
            driver,
            _LOGIN_URL,
            timeout=min(45, int(_roxy_cfg.ROXY_SELENIUM_TIMEOUT)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="live_check_login")
        _maybe_accept(driver)

        _type_email_address(driver, email, timeout=12)
        human_delay("form")
        _submit_email_step(driver)
        logger.info("[查活][Browser] 已提交邮箱，等待密码页/验证码页")

        pw_result = _fill_login_password_if_present(driver, email, timeout=18)
        if pw_result == "next_step" and _is_mfa_challenge_page(driver):
            _fill_mfa_challenge_if_present(driver, email, timeout=15)
            logger.info("[查活][Browser] 密码 + MFA 登录完成，读取 session")
        else:
            if not _is_email_verification_page(driver):
                _fill_mfa_challenge_if_present(driver, email, timeout=15)
            if _is_email_verification_page(driver):
                _submit_email_otp(driver, email, email_source=email_source, otp_after_ts=otp_after_ts)
            logger.info("[查活][Browser] 邮箱登录完成，读取 session")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("浏览器登录后未拿到 accessToken")
        return {
            "ok": True,
            "access_token": access_token,
            "session": session_info,
        }
    finally:
        keep_open = bool(getattr(_roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        if driver is not None and not keep_open:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001, S110
                pass
        if not keep_open:
            client.cleanup_profile(opened)
