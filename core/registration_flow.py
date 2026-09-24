"""Shared registration orchestration on a PageDriver and browser adapter.

Flow: submit email -> force password -> email OTP -> about-you -> session ->
checkpoint. Provider-specific behavior is supplied through adapter hooks.
"""
from __future__ import annotations

import logging
import random
import time

from core.account_export import checkpoint_account_data
from core.browser_challenge import (
    browser_challenge_state as _browser_challenge_state,
)
from core.browser_challenge import (
    wait_for_browser_challenge as _wait_for_browser_challenge,
)
from core.browser_failure_policy import (
    raise_if_account_unusable as _raise_if_account_unusable,
)
from core.browser_failure_policy import (
    release_status_for_failure,
)
from core.browser_failure_policy import (
    wait_after_password_submit as _wait_after_password_submit,
)
from core.browser_page_actions import (
    _browser_actions_enabled,
    _clear_otp_inputs,
    _click_continue,
    _click_resend_email_otp,
    _coerce_browser_mapping,
    _email_input_value_state,
    _email_otp_page_state,
    _find_any,
    _has_access_token,
    _human_click,
    _human_type_text,
    _is_chrome_error_page,
    _is_email_verification_page,
    _is_login_password_page,
    _is_profile_like,
    _is_signup_password_page,
    _is_transient_email_submission_error,
    _log_prefix,
    _maybe_accept,
    _page_snapshot,
    _page_warmup,
    _password_page_state,
    _registration_timeout,
    _safe_get,
    _select_or_type,
    _type_otp,
    _wait_after_email_otp_submit,
    _wait_email_submit_next_state,
)
from core.email_provider import (
    _BEFORE_CODE_UNSET as _OTP_BEFORE_CODE_UNSET,
)
from core.email_provider import (
    acknowledge_verification_code,
    resolve_email_source,
    snapshot_verification_code,
    wait_for_otp,
)
from core.humanize import delay as human_delay
from core.openai_auth import (
    AccountUnusableError,
    account_unusable_message,
)
from core.registration_profile_utils import (
    poll_profile_submission_error as _poll_profile_submission_error,
)
from core.registration_profile_utils import (
    profile_submission_error as _profile_submission_error,
)
from core.registration_profile_utils import (
    profile_submission_failure_message as _profile_submission_failure_message,
)
from core.time_utils import local_today

logger = logging.getLogger(__name__)

# 连续收到 WARNING_BANNER 多少次后刷新 ChatGPT 页面重读 session（测试共享该阈值）。
_SESSION_BANNER_REFRESH_AFTER = 5


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
        return _coerce_browser_mapping(driver, result, label="email_entry")
    except Exception as exc:  # noqa: BLE001
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        state = driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        const visibleEmailInput = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
          .some(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
        const actionAttrs = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('autocomplete'), el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
        const actions = actionAttrs.join(' ');
        return {
          url,
          has_email_entry: visibleEmailInput || formsWithEmail || actionAttrs.some(attrs => /email|username|passwordless|one[-_ ]?time|otp|magic/.test(attrs)),
          has_consent_action: /oauth|authorize|consent|grant|allow/.test(actions)
        };
        """) or {}
        if not isinstance(state, dict):
            return bool(state)
        return bool(state.get("has_consent_action") and not state.get("has_email_entry"))
    except Exception:  # noqa: BLE001
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:  # noqa: BLE001
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    if (typeof candidates[0].el?.scrollIntoView === 'function') candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _wait_for_email_input(driver, timeout: int | None = None):
    """进入邮箱登录/注册方式并返回已找到的可见邮箱输入框。"""
    end = time.time() + (timeout or _registration_timeout(driver))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        try:
            # All providers expose the same Selenium-compatible locator surface.
            # Returning an element from execute_script can leave a detached/null
            # handle when React remounts the auth form between lookup and typing.
            el = _find_any(driver, _EMAIL_INPUT_SELECTORS, timeout=2)
            return el
        except Exception as exc:  # noqa: BLE001
            last_state = {"native_locator_error": f"{type(exc).__name__}: {exc}"}
        last_state = _email_entry_state(driver)
        try:
            challenge_state = _browser_challenge_state(driver)
        except Exception as exc:  # noqa: BLE001 - a state probe must not hide the original lookup failure.
            logger.debug(
                "%s 浏览器 challenge 状态读取失败，继续等待邮箱输入框：%s",
                _log_prefix(driver),
                str(exc)[:160],
            )
            challenge_state = {}
        if isinstance(challenge_state, dict) and challenge_state.get("is_challenge"):
            remaining = max(0.0, end - time.time())
            logger.warning(
                "%s 查找邮箱输入框期间检测到浏览器 challenge，等待 challenge 完成：url=%s title=%s reason=%s",
                _log_prefix(driver),
                str(challenge_state.get("url") or last_state.get("url") or "")[:180],
                str(challenge_state.get("title") or last_state.get("title") or "")[:120],
                str(challenge_state.get("reason") or "")[:120],
            )
            if remaining <= 0:
                break
            _wait_for_browser_challenge(
                driver,
                timeout=min(float(_registration_timeout(driver)), remaining),
            )
            continue
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    element = _wait_for_email_input(driver, timeout=timeout)
    try:
        _human_type_text(driver, element, email, clear=True)
    except Exception as exc:  # noqa: BLE001 - auth forms may remount while typing.
        logger.warning(
            "%s 邮箱输入控件在输入期间被页面替换，重新定位后重试：error=%s",
            _log_prefix(driver),
            str(exc)[:180],
        )
        replacement = _wait_for_email_input(driver, timeout=min(5, timeout or 5))
        _human_type_text(driver, replacement, email, clear=True)


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    if (typeof target?.scrollIntoView === 'function') target.scrollIntoView({block:'center'});
    // Store transient diagnostics for the shared email submit step.
    window.__browser_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:  # noqa: BLE001, S110
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (typeof input?.scrollIntoView === 'function') input.scrollIntoView({block:'center', inline:'nearest'});
        if (typeof input?.focus === 'function') input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        if (typeof input?.focus === 'function') input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (typeof input?.scrollIntoView === 'function') input.scrollIntoView({block:'center', inline:'nearest'});
        if (typeof input?.focus === 'function') input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        if (typeof input?.focus === 'function') input.focus();

        if (typeof submit?.scrollIntoView === 'function') submit.scrollIntoView({block:'center', inline:'nearest'});

        // The synchronous script runner can wait for navigation; schedule the
        // click so the caller returns before the page transition completes.
        setTimeout(() => {
          try {
            if (typeof input?.focus === 'function') input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keypress', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__browser_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # Keep the provider-neutral UI submit path as the single implementation.
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _reset_login_page_for_retry(driver) -> None:
    """Tải lại login page trước retry để bỏ DOM SPA đã bị unmount sau submit."""
    driver.get("https://chatgpt.com/auth/login")
    human_delay("navigate")
    _maybe_accept(driver)
    _assert_not_external_idp(driver, "retry login page")


def _submit_email_and_wait_next(driver, email: str | None, attempts: int = 3, allow_login_password: bool = False, email_supplier=None) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in。"""
    last_state = None
    for attempt in range(1, attempts + 1):
        try:
            _wait_for_browser_challenge(driver, timeout=_registration_timeout(driver))
            if email is None and email_supplier is not None:
                email = email_supplier()
            _type_email_address(driver, email, timeout=20)
            state = _email_input_value_state(driver)
            last_state = state
            email_text = str(email or "").strip()
            values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
            if not any(v.strip().lower() == email_text.lower() for v in values):
                logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
                time.sleep(0.8)
                continue
            logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
            human_delay("form")
            _submit_email_step(driver, email)
            logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
            state_name = _wait_email_submit_next_state(driver, email, timeout=20)
            if state_name == "login_password" and not allow_login_password:
                raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
            if state_name in ("password", "otp", "logged_in", "login_password"):
                logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
                return state_name
            logger.warning(
                "%s 邮箱提交后仍未进入下一步：%s，准备重填重试",
                _log_prefix(driver),
                state_name,
            )
            if attempt < attempts:
                _reset_login_page_for_retry(driver)
            else:
                time.sleep(1.0)
        except Exception as exc:
            if attempt >= attempts or not _is_transient_email_submission_error(exc):
                raise
            logger.warning(
                "%s 邮箱提交后的页面状态读取遇到临时错误，reload 登录页后重试：attempt=%s/%s error=%s",
                _log_prefix(driver),
                attempt,
                attempts,
                str(exc)[:180],
            )
            _reset_login_page_for_retry(driver)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _resend_or_restart_email_otp(driver, email: str) -> None:
    """Trigger a new email OTP when the verification page is in an error state."""
    if _is_chrome_error_page(driver):
        logger.warning(
            "%s[OTP] Trang xác thực là trang lỗi (chrome-error/HTTP 500)，mở lại login page và submit lại email để trigger OTP mới",
            _log_prefix(driver),
        )
        _reset_login_page_for_retry(driver)
        _check_manual_stop()
        next_state = _submit_email_and_wait_next(driver, email, attempts=2)
        if next_state == "otp":
            _click_continue_with_password_link(driver)
            _check_manual_stop()
            _fill_password_page_if_present(driver, email, timeout=25)
        return
    _click_resend_email_otp(driver, timeout=25)


def _ensure_email_otp_page_ready(driver, timeout: int = 12) -> None:
    """提交验证码前必须真的在验证码页；否则页面错误会被误报成“找不到重发按钮”。

    密码页被 OpenAI 拒绝（Failed to create account）时会一直停在密码页：没有
    OTP 输入框、也没有重发按钮。先短等页面切换；超时就把页面真实错误抛出，
    便于一眼看出是“验证码页没到”还是“创建账号被拒”。页面状态探测不了时
    （驱动异常/测试桩）保持沉默，不改变原有流程。
    """
    def _otp_page_reachable() -> bool:
        return bool(_is_email_verification_page(driver) or _has_access_token(driver))

    try:
        if _otp_page_reachable():
            return
    except Exception:  # noqa: BLE001
        return
    end = time.time() + timeout
    while time.time() < end:
        _check_manual_stop()
        try:
            if _otp_page_reachable():
                return
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.5)
    state = _email_otp_page_state(driver)
    if not isinstance(state, dict):
        state = {}
    if state.get("error"):
        # Trang không probe được (reader tổng hợp đánh dấu bằng error) — giữ im lặng
        # theo đúng cam kết ở docstring, không đổi flow gốc.
        return
    errors = [str(err).strip() for err in (state.get("errors") or []) if str(err).strip()]
    detail = "; ".join(errors[:3]) or str(state.get("text") or "")[:200]
    raise RuntimeError(
        f"当前页面不是邮箱验证码页，无法提交验证码: url={state.get('url')} 页面错误={detail}"
    )


def _complete_email_otp(
    driver,
    email: str,
    *,
    otp_after_ts: float,
    otp_code: str | None = None,
    otp_before_code: str | None | object = _OTP_BEFORE_CODE_UNSET,
    max_attempts: int = 3,
) -> None:
    """获取、提交并在失败后重发邮箱 OTP；每次重试都只接受新取到的码。"""
    current_otp = otp_code
    previous_submitted_otp = None
    last_error: Exception | None = None
    for otp_attempt in range(1, max(1, int(max_attempts)) + 1):
        if current_otp is None:
            logger.info(
                "%s[OTP] 等待验证码：%s（第 %s/%s 次）",
                _log_prefix(driver),
                email,
                otp_attempt,
                max_attempts,
            )
            try:
                wait_kwargs = {
                    "after_ts": otp_after_ts,
                    "stage": "registration_email_otp",
                }
                if otp_before_code is not _OTP_BEFORE_CODE_UNSET and otp_before_code:
                    wait_kwargs["before_code"] = otp_before_code
                elif previous_submitted_otp:
                    wait_kwargs["before_code"] = previous_submitted_otp
                current_otp = wait_for_otp(email, **wait_kwargs)
            except Exception as exc:
                last_error = exc
                if otp_attempt >= max_attempts:
                    raise
                logger.warning(
                    "%s[OTP] 取码失败，先重新发送验证码再请求新码（%s/%s）：%s: %s",
                    _log_prefix(driver),
                    otp_attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                otp_after_ts = time.time()
                otp_before_code = snapshot_verification_code(
                    email,
                    stage="registration_email_resend",
                )
                _resend_or_restart_email_otp(driver, email)
                human_delay("api")
                continue

        try:
            logger.info("%s[OTP] 收到验证码：%s", _log_prefix(driver), current_otp)
            _ensure_email_otp_page_ready(driver)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            previous_submitted_otp = current_otp
            logger.info("%s[OTP] 已填写邮箱验证码", _log_prefix(driver))
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("%s[OTP] 已提交邮箱验证码，等待资料页或登录态", _log_prefix(driver))
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "%s[OTP] 未找到显式提交按钮，继续等待页面状态：%s",
                    _log_prefix(driver),
                    str(exc)[:120],
                )
            outcome = _wait_after_email_otp_submit(driver, timeout=30)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            outcome = "error"

        if outcome == "accepted":
            acknowledge_verification_code(
                email,
                current_otp,
                stage="registration_email_otp",
            )
            return
        if otp_attempt >= max_attempts:
            if outcome == "invalid":
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            if last_error is not None:
                raise last_error
            raise RuntimeError("邮箱验证码提交失败，已达到最大重试次数")

        logger.warning(
            "%s[OTP] 验证码提交失败，准备重新发送并重新获取验证码（%s/%s）",
            _log_prefix(driver),
            otp_attempt + 1,
            max_attempts,
        )
        otp_after_ts = time.time()
        otp_before_code = snapshot_verification_code(
            email,
            stage="registration_email_resend",
        )
        if previous_submitted_otp and not otp_before_code:
            otp_before_code = current_otp
        _resend_or_restart_email_otp(driver, email)
        human_delay("api")
        current_otp = None


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:  # noqa: BLE001, S110
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("typeof arguments[0]?.scrollIntoView === 'function' && arguments[0].scrollIntoView({block:'center'}); typeof arguments[0]?.focus === 'function' && arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:  # noqa: BLE001
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), exc)
        return None


def _generate_registration_password() -> str:
    """14 位密码，仅含大小写字母和数字，避免符号方便人工登录时手输。"""
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    groups = [upper, lower, digits]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, 'REGISTER_PASSWORD', '') or '').strip()
        if configured:
            return configured
    except Exception:  # noqa: BLE001, S110
        pass
    return _generate_registration_password()


def _password_submission_error(driver, snapshot: dict | None = None) -> str | None:
    """Return a visible password-submit error without exposing form values."""
    snapshot = snapshot if isinstance(snapshot, dict) else _page_snapshot(driver)
    if not isinstance(snapshot, dict):
        return None
    url = str(snapshot.get("url") or "").lower()
    if not any(x in url for x in ("/create-account/password", "/u/signup/password", "/signup/password")):
        return None

    messages: list[str] = []
    for value in snapshot.get("errors") or []:
        message = " ".join(str(value or "").split()).strip()
        if message and message not in messages:
            messages.append(message[:240])
    if messages:
        return "拒绝创建账号: " + "; ".join(messages[:3])

    text = " ".join(str(snapshot.get("text") or "").split()).lower()
    markers = (
        "cannot create your account",
        "could not create your account",
        "unable to create your account",
        "failed to create account",
        "something went wrong",
        "an error occurred",
        "try again",
        "vui lòng thử lại",
        "không tạo được tài khoản",
        "无法创建账号",
        "无法创建账户",
        "请重试",
    )
    for marker in markers:
        if marker in text:
            return marker
    return None


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
        if (typeof btn?.scrollIntoView === 'function') btn.scrollIntoView({block:'center'});
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


def _click_continue_with_password_link(driver) -> bool:
    """Khi ChatGPT landed trên email-verification page, click 'Continue with password'
    hoặc navigate thẳng đến /create-account/password để force password step.

    Giống JnmBrowser engine.rs:3636-3678 — không bao giờ đi OTP-only.
    """
    clicked = False
    try:
        clicked = bool(driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const bad = /passwordless|one[-_\s]?time|otp|magic|code/;
        const good = /continue.*password|password.*continue|continuar.*senha|senha.*continuar/;
        const candidates = [...document.querySelectorAll('button,a,[role="button"],[role="link"]')]
          .filter(visible)
          .filter(el => {
            const text = norm(el.textContent || '');
            const attrs = [el.id, el.getAttribute('name'), el.getAttribute('aria-label'),
              el.getAttribute('title'), el.getAttribute('data-testid'), el.className]
              .join(' ').toLowerCase();
            return good.test(text) || good.test(attrs);
          })
          .filter(el => !bad.test(norm(el.textContent || '')));
        if (candidates.length < 1) return false;
        if (typeof candidates[0]?.scrollIntoView === 'function') candidates[0].scrollIntoView({block:'center'});
        candidates[0].click();
        return true;
        """) or False)
    except Exception:  # noqa: BLE001, S110
        pass
    if clicked:
        logger.info("%s 已点击 'Continue with password' 链接，等待密码表单", _log_prefix(driver))
        time.sleep(2.0)
        return True
    logger.info("%s 未找到 'Continue with password' 链接，直接导航到 /create-account/password", _log_prefix(driver))
    try:
        driver.get("https://auth.openai.com/create-account/password")
        time.sleep(2.0)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 导航到 /create-account/password 失败: %s", _log_prefix(driver), exc)
        return False


def _fill_password_page_if_present(driver, email: str, timeout: int = 25) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    last = {}
    verification_redirected = False
    while time.time() < end:
        _raise_if_account_unusable(driver)
        if _is_email_verification_page(driver):
            if verification_redirected:
                time.sleep(0.5)
                continue
            # The session endpoint can expose a token before email verification
            # completes. Always leave verification for the signup password step.
            _click_continue_with_password_link(driver)
            verification_redirected = True
            continue
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            time.sleep(0.5)
            continue
        # Force password: không click passwordless OTP, luôn fill password (yêu cầu user).
        # _click_passwordless_signup_if_present đã bị bỏ để không bao giờ đi OTP-only.
        if is_login_password:
            # 与 _wait_email_submit_next_state 的 login_password 分支同语义：邮箱已注册，
            # 继续走 OTP 流程只会停在密码页，必须在进入取码前停用该邮箱。
            raise RuntimeError(
                f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: "
                f"url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}"
            )
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        if (typeof buttons[0].el?.scrollIntoView === 'function') buttons[0].el.scrollIntoView({block:'center'});
        return {ok:true, reason:'password_targets', input, button: buttons[0].el};
        """) or {}
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        _human_type_text(driver, result.get("input"), password, clear=True)
        human_delay("form", minimum=0.4, maximum=1.4)
        initial_url = str(getattr(driver, "current_url", "") or "")
        _human_click(driver, result.get("button"), label="password_submit")
        _wait_after_password_submit(driver, initial_url, timeout=min(5.0, max(0.0, float(timeout))))
        logger.info("%s 已填写并提交密码页", _log_prefix(driver))
        # 提交密码后通常进入邮箱验证码页，最多等一段时间。
        wait_end = time.time() + 20
        while time.time() < wait_end:
            _raise_if_account_unusable(driver)
            if _is_email_verification_page(driver):
                logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                return password
            if _has_access_token(driver):
                logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                return password
            if _is_signup_password_page(driver):
                submission_error = _password_submission_error(driver, last)
                if submission_error:
                    raise RuntimeError(f"密码页提交失败：{submission_error}")
            else:
                return password
            time.sleep(0.5)
        if _is_signup_password_page(driver):
            submission_error = _password_submission_error(driver)
            if submission_error:
                raise RuntimeError(f"密码页提交失败：{submission_error}")
            raise RuntimeError("密码页提交后未进入邮箱验证码页，仍停留在注册密码页")
        return password
    if verification_redirected:
        raise RuntimeError(
            f"邮箱验证码页未能跳转到 create-account/password，拒绝绕过密码步骤: state={last}"
        )
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            const scrollTarget = label && visible(label) ? label : el;
            if (typeof scrollTarget?.scrollIntoView === 'function') scrollTarget.scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:  # noqa: BLE001
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    today = local_today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                time.sleep(1)
                profile_error = _profile_submission_error(_page_snapshot(driver))
                if profile_error:
                    logger.error(
                        '%s about-you 提交被服务端拒绝：%s',
                        _log_prefix(driver),
                        profile_error,
                    )
                    raise RuntimeError(_profile_submission_failure_message(profile_error))
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
    # Collect delayed terminal errors after the profile submission.

                def _left_profile(snap: dict) -> bool:
                    url_now = str(snap.get('url') or '').lower()
                    return 'about-you' not in url_now and 'profile' not in url_now

                delayed_error = _poll_profile_submission_error(
                    lambda: _page_snapshot(driver),
                    left_profile=_left_profile,
                )
                if delayed_error:
                    logger.error(
                        '%s about-you 提交被服务端拒绝（延迟渲染）：%s',
                        _log_prefix(driver),
                        delayed_error,
                    )
                    raise RuntimeError(_profile_submission_failure_message(delayed_error))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            if (typeof submit?.scrollIntoView === 'function') submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          if (typeof submitters[0]?.scrollIntoView === 'function') submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          if (typeof buttons[0]?.scrollIntoView === 'function') buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取 /api/auth/session，拿不到 token 返回 None。"""
    request_session = getattr(driver, "get_chatgpt_auth_session", None)
    if callable(request_session):
        try:
            data = request_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s /api/auth/session request failed: %s", _log_prefix(driver), exc)
            return None
        if isinstance(data, dict):
            if data.get("accessToken"):
                logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            else:
                logger.info("%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s", _log_prefix(driver), list(data.keys()))
            return data
        return None

    script = r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'})
      .then(r => r.json().then(data => {
        if (data && typeof data === 'object') data._http_status = r.status;
        done({ok: true, data});
      }))
      .catch(e => done({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script)
    if result and result.get("ok"):
        data = result.get("data") or {}
        if isinstance(data, dict):
            data.setdefault("_http_status", result.get("status"))
            if data.get("accessToken"):
                logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            else:
                logger.info("%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s", _log_prefix(driver), list(data.keys()))
            return data
    return None


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:  # noqa: BLE001
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("切换浏览器窗口探测失败：%s: %s", type(exc).__name__, exc)
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:  # noqa: BLE001, S110
                pass
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在认证域上一直等待到总超时，部分浏览器场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False
    banner_count = 0
    banner_refresh_done = False
    session_banner_refresh_after = _SESSION_BANNER_REFRESH_AFTER

    while time.time() < end:
        _check_manual_stop()
        try:
            current = str(driver.current_url or '')
        except Exception:  # noqa: BLE001
            current = ''

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    time.sleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:  # noqa: BLE001
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current:
            data = _read_chatgpt_session_once(driver)
            if isinstance(data, dict):
                if data.get("accessToken"):
                    return data
                last_data = data
                if "WARNING_BANNER" in data:
                    banner_count += 1
                    if banner_count >= session_banner_refresh_after:
                        if banner_refresh_done:
                            raise RuntimeError(
                                "等待 /api/auth/session accessToken 超时，最后响应: "
                                f"{str(data)[:800]}"
                            )
                        logger.warning(
                            "%s /api/auth/session 连续收到 WARNING_BANNER，刷新 ChatGPT 页面后重读 session",
                            _log_prefix(driver),
                        )
                        try:
                            driver.refresh()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("%s ChatGPT 页面刷新失败，改为重新打开：%s", _log_prefix(driver), exc)
                            _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=1, accept_hosts=("chatgpt.com",))
                        banner_refresh_done = True
                        banner_count = 0
                        time.sleep(3)
                        continue
                else:
                    banner_count = 0
            else:
                last_data = "session 暂无 accessToken"
        time.sleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def registration_failure_result(
    exc: BaseException,
    email: str | None,
    extras: dict | None = None,
) -> dict:
    """Kết quả failure chuẩn cho mọi lane (hướng 4 của except-block chung)."""
    result: dict = {
        "success": False,
        "email": email,
        "error": f"{type(exc).__name__}: {str(exc)[:800]}",
    }
    if extras:
        result.update(extras)
    return result


def release_registration_email_on_failure(
    exc: BaseException,
    email: str | None,
    *,
    create_acknowledged: bool = False,
    note_prefix: str = "",
    log_prefix: str = "",
) -> None:
    """Except-block chung 4 hướng — hướng 1-3: paymesh terms-block → release theo policy.

    Hướng 1: lỗi about-you 条款 (利用規約/terms/cannot create) → chặn Paymesh card.
    Hướng 2: policy superset quyết disabled/failed/available (browser_failure_policy).
    Hướng 3: note text giữ account_unusable_message cho AccountUnusableError.
    Hướng 4 nằm ở registration_failure_result.
    """
    try:
        from core.email_provider import release_email

        error_text = str(exc)
        note_text = account_unusable_message(exc.error_code) if isinstance(exc, AccountUnusableError) else error_text
        if "about-you 提交失败" in error_text and (
            "利用規約" in error_text
            or "terms of use" in error_text.lower()
            or "cannot create your account" in error_text.lower()
        ):
            try:
                from core.paymesh_mail_client import block_account_card
                block_account_card(email, reason="terms_rejected")
            except Exception:
                logger.debug("%s 标记 Paymesh card blocked 失败", log_prefix, exc_info=True)
        release_status = release_status_for_failure(exc, create_acknowledged=create_acknowledged)
        note = f"{note_prefix}: {error_text[:180]}" if note_prefix else note_text[:180]
        release_email(email, status=release_status, note=note)
    except Exception:  # noqa: BLE001, S110
        pass


def run_registration_page_flow(
    driver,
    email: str | None,
    name: str,
    birthday: str,
    *,
    otp_code: str | None = None,
    otp_before_code: str | None | object = _OTP_BEFORE_CODE_UNSET,
    email_supplier=None,
    proxy: str | None = None,
    registration_driver: str,
    checkpoint_extras: dict | None = None,
    registration_ip: str | None = None,
    login_page_timeout: int = 45,
    login_page_attempts: int = 2,
    warmup_after_login: bool = False,
    session_timeout: int = 120,
    session_auto_jump_wait: int = 15,
    session_recovery=None,
    session_fetch=None,
    otp_completion=None,
    after_profile_submit=None,
    on_page_progress=None,
    on_account_created=None,
    checkpoint_extras_factory=None,
    early_checkpoint: bool = False,
    log_prefix: str = "[Browser注册]",
    failure_note_prefix: str = "",
    release_failure: bool = True,
    raise_failure: bool = False,
    failure_extras: dict | None = None,
) -> dict:
    """Chạy flow đăng ký chung trên PageDriver; trả state cho lane hookup 2FA/Codex.

    Bước: submit email → force password → OTP → about-you → session → checkpoint
    (early + token). Khác biệt lane đi qua hook: otp_completion (loop OTP riêng),
    after_profile_submit (chuyển trang sau profile), session_recovery (re-auth
    lại session), early_checkpoint (lưu email+password trước khi có token).
    Lane tự làm 2FA setup / Codex / save_account_data với state trả về.
    """
    create_acknowledged = False
    openai_password: str | None = None
    account_id: int | None = None

    def _supplier_after_input() -> str:
        nonlocal email
        email = email_supplier()
        return email

    supplier = _supplier_after_input if email_supplier is not None else None
    try:
        otp_after_ts = time.time()
        logger.info("%s 打开登录页：https://chatgpt.com/auth/login", log_prefix)
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=login_page_timeout,
            attempts=login_page_attempts,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        if warmup_after_login:
            _page_warmup(driver, reason="login_page")
        logger.info("%s 登录页加载完成，准备填写邮箱", log_prefix)
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=supplier,
        )
        _check_manual_stop()
        if on_page_progress is not None:
            on_page_progress("email_submitted")

        # Luôn force password: nếu email transition trả về OTP (hoặc logged-in
        # nhưng form OTP còn hiển thị), chuyển sang create-account/password trước
        # khi nhập, không bao giờ đi OTP-only.
        if next_state == "otp" or (next_state == "logged_in" and _is_email_verification_page(driver)):
            _click_continue_with_password_link(driver)
            _check_manual_stop()
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        if openai_password:
            create_acknowledged = True
            if on_account_created is not None:
                on_account_created()
        _check_manual_stop()

        if otp_completion is None:
            otp_completion = _complete_email_otp
        otp_completion(
            driver,
            email,
            otp_after_ts=otp_after_ts,
            otp_code=otp_code,
            otp_before_code=otp_before_code,
        )

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("%s 开始等待资料页/登录态", log_prefix)
        _check_manual_stop()
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            if early_checkpoint and openai_password:
                # about-you 已提交 = 账号已在服务端创建。提前落检查点（email +
                # password，twofa=pending，暂无 accessToken）：后续 session 被
                # 网关拦截（WARNING_BANNER）时账号/alias 不再变成孤儿，改由
                # 队列的 login-retry 补做 2FA。无密码则无法 login-retry，跳过。
                early_extras = (
                    checkpoint_extras_factory(session_info=None)
                    if checkpoint_extras_factory is not None
                    else checkpoint_extras or {}
                )
                account_id = checkpoint_account_data(
                    email=email,
                    access_token="",
                    email_source=resolve_email_source(email),
                    proxy_used=str(proxy) if proxy else None,
                    registration_ip=registration_ip,
                    extra={
                        **early_extras,
                        "registration_password": openai_password,
                        "registration_driver": registration_driver,
                        "registration_name": name,
                        "registration_birthday": birthday,
                        "session_pending": True,
                    },
                )
                logger.info(
                    "%s about-you 检查点已保存（账号已创建，等待 session）：account_id=%s twofa=pending",
                    log_prefix,
                    account_id,
                )
            if after_profile_submit is not None:
                after_profile_submit(driver)
            if on_page_progress is not None:
                on_page_progress("profile_submitted")
            if on_account_created is not None:
                on_account_created()
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("%s 等待 ChatGPT 跳转并写入 session/accessToken", log_prefix)
        _check_manual_stop()
        try:
            session_reader = session_fetch or _fetch_chatgpt_session
            session_info = session_reader(
                driver,
                timeout=session_timeout,
                auto_jump_wait=session_auto_jump_wait,
            )
        except Exception as session_err:
            if session_recovery is None:
                raise
            logger.warning(
                "%s 首轮未拿到 accessToken，进入恢复流程：%s",
                log_prefix,
                str(session_err)[:200],
            )
            try:
                session_info = session_recovery(driver, email, session_err)
            except Exception as recover_err:
                if account_id is not None:
                    # 账号已在 about-you 后落盘：不烧 alias、不重注册——
                    # 交给队列 login-retry（补做 2FA）用新 IP 重新登录。
                    logger.error(
                        "%s session 恢复失败但账号已创建，交由队列 login 重试并补做 2FA：account_id=%s err=%s",
                        log_prefix,
                        account_id,
                        str(recover_err)[:200],
                    )
                    return {
                        "success": False,
                        "email": email,
                        "account_id": account_id,
                        "twofa_status": "pending",
                        **(failure_extras or {}),
                        "error": f"session 超时但账号已创建，已保留待 login 补做：{str(recover_err)[:200]}",
                    }
                raise
        access_token = session_info["accessToken"]
        logger.info("%s 已拿到 accessToken：%s", log_prefix, email)
        _check_manual_stop()

        token_extras = (
            checkpoint_extras_factory(session_info=session_info)
            if checkpoint_extras_factory is not None
            else checkpoint_extras or {}
        )
        token_extras = dict(token_extras)
        token_extras.pop("session_pending", None)
        account_id = checkpoint_account_data(
            email=email,
            access_token=access_token,
            email_source=resolve_email_source(email),
            proxy_used=str(proxy) if proxy else None,
            registration_ip=registration_ip,
            extra={
                **token_extras,
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "registration_password": openai_password,
                "registration_driver": registration_driver,
                "registration_name": name,
                "registration_birthday": birthday,
            },
        )
        logger.info("%s token 检查点已保存：account_id=%s twofa=pending", log_prefix, account_id)
        _check_manual_stop()
        return {
            "success": True,
            "stage": "checkpointed",
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "session_info": session_info,
            "openai_password": openai_password,
            "create_acknowledged": create_acknowledged,
        }
    except Exception as exc:
        logger.error("%s 失败：%s: %s", log_prefix, type(exc).__name__, exc)
        logger.debug("%s 失败详情", log_prefix, exc_info=True)
        if raise_failure:
            raise
        if release_failure:
            release_registration_email_on_failure(
                exc,
                email,
                create_acknowledged=create_acknowledged,
                note_prefix=failure_note_prefix,
                log_prefix=log_prefix,
            )
        return registration_failure_result(exc, email, extras=failure_extras)
