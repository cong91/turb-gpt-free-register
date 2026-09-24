"""Neutral Selenium authentication page actions."""
from __future__ import annotations

import logging

from core.browser_page_actions import _human_click

logger = logging.getLogger(__name__)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        result = driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        if (/oauth|authorize|consent/.test(url) && !/login|signup|identifier|email-verification/.test(url)) return true;
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        const actions = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
          .join(' ');
        return /oauth|authorize|consent|grant|allow/.test(actions) && !/email|username/.test(actions);
        """)
        if isinstance(result, dict):
            return bool(result.get("has_consent_action") and not result.get("has_email_entry"))
        return bool(result)
    except Exception:  # noqa: BLE001
        return False






def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) return {ok:false, reason:'missing_passwordless_button'};
        btn.scrollIntoView({block:'center'});
        return {
          ok:true,
          reason:'passwordless_send_otp_target',
          button: btn,
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if result.get("ok") and result.get("button"):
            _human_click(driver, result.get("button"), label="passwordless_otp")
            result["reason"] = "clicked_passwordless_send_otp"
            result.pop("button", None)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


__all__ = ["_click_passwordless_signup_if_present", "_is_oauth_consent_like"]


