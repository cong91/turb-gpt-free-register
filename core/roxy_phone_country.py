"""Select phone countries in OpenAI's add-phone form."""
from __future__ import annotations

import time

from selenium.common.exceptions import WebDriverException

_SELECT_COUNTRY_SCRIPT = r"""
return (function () {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const isVietnam = (value) => {
    const text = clean(value);
    return /(?:vietnam|viet nam|việt nam)/i.test(text) || /\+84\b/.test(text);
  };
  const optionText = (option) => clean(
    option?.innerText || option?.textContent || option?.label || option?.value
  );

  const nativeSelect = [...document.querySelectorAll('select')]
    .filter(visible)
    .find((select) => [...select.options].some((option) => {
      const key = String(option.dataset?.key || option.value || '').toUpperCase();
      return key === 'VN' || isVietnam(optionText(option));
    }));
  if (nativeSelect) {
    const option = [...nativeSelect.options].find((item) => {
      const key = String(item.dataset?.key || item.value || '').toUpperCase();
      return key === 'VN' || isVietnam(optionText(item));
    });
    if (option) {
      nativeSelect.value = option.value;
      nativeSelect.dispatchEvent(new Event('input', { bubbles: true }));
      nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
      const text = optionText(option);
      return { ok: isVietnam(text), method: 'native', country: text };
    }
  }

  const tel = document.querySelector(
    'input[type="tel"], input[autocomplete="tel"], input[inputmode="tel"]'
  );
  const triggers = [...document.querySelectorAll(
    'button[aria-haspopup="listbox"], [role="combobox"]'
  )].filter(visible);
  triggers.sort((left, right) => {
    if (!tel) return 0;
    const telRect = tel.getBoundingClientRect();
    return Math.abs(left.getBoundingClientRect().top - telRect.top)
      - Math.abs(right.getBoundingClientRect().top - telRect.top);
  });
  const trigger = triggers[0];
  if (!trigger) return { ok: false, phase: 'trigger_missing' };
  const triggerText = clean(trigger.innerText || trigger.textContent);
  if (isVietnam(triggerText)) {
    return { ok: true, method: 'already_selected', country: triggerText };
  }

  const listbox = [...document.querySelectorAll('[role="listbox"]')].find(visible);
  if (!listbox) {
    trigger.scrollIntoView({ block: 'center', inline: 'nearest' });
    trigger.focus();
    trigger.click();
    try {
      trigger.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, pointerType: 'mouse'
      }));
      trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      trigger.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    } catch (_) {}
    return { ok: false, phase: 'opened' };
  }

  let option = [...listbox.querySelectorAll('[role="option"]')].find((item) => {
    const key = String(item.getAttribute('data-key') || '').toUpperCase();
    const text = optionText(item);
    return key === 'VN' || (isVietnam(text) && /84/.test(text));
  });
  if (!option) {
    let scroller = listbox;
    for (let node = listbox; node && node !== document.body; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight + 4) {
        scroller = node;
        break;
      }
    }
    scroller.scrollTop = scroller.scrollHeight;
    listbox.scrollTop = listbox.scrollHeight;
    scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
    listbox.dispatchEvent(new Event('scroll', { bubbles: true }));
    return { ok: false, phase: 'waiting_for_vn_option' };
  }

  option.scrollIntoView({ block: 'center', inline: 'nearest' });
  option.focus({ preventScroll: true });
  try {
    option.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, cancelable: true, pointerType: 'mouse'
    }));
    option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    option.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  } catch (_) {}
  option.click();
  const country = optionText(option);
  return { ok: false, phase: 'selected', country };
})();
"""


def select_vietnam_country(driver, *, timeout: float = 12.0) -> dict:
    """Select and verify Vietnam (+84) in the current add-phone form."""
    deadline = time.time() + max(0.0, float(timeout))
    last_result: dict = {}
    while time.time() < deadline:
        try:
            result = driver.execute_script(_SELECT_COUNTRY_SCRIPT) or {}
        except WebDriverException as exc:
            result = {"ok": False, "phase": "script_error", "error": str(exc)[:160]}
        if not isinstance(result, dict):
            result = {"ok": False, "phase": "invalid_script_result"}
        last_result = result
        if result.get("ok"):
            return result
        time.sleep(0.25)
    raise RuntimeError(
        "Không thể chọn quốc gia Vietnam (+84) trên trang xác minh điện thoại: "
        f"last={last_result}"
    )


_SELECT_PHONE_COUNTRY_SCRIPT = r"""
const rawPhone = String(arguments[0] || '');
return (function () {
  const digits = rawPhone.replace(/\D+/g, '');
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const optionText = (option) => clean(
    option?.innerText || option?.textContent || option?.label || option?.value
  );
  const optionDialCode = (option) => {
    const match = optionText(option).match(/\+(\d{1,4})\b/);
    return match ? match[1] : '';
  };
  const matchesPhone = (option) => {
    const code = optionDialCode(option);
    return !!code && digits.startsWith(code);
  };
  if (!digits) return { ok: false, phase: 'invalid_phone' };

  const nativeSelect = [...document.querySelectorAll('select')]
    .filter(visible)
    .find((select) => [...select.options].some(matchesPhone));
  if (nativeSelect) {
    const option = [...nativeSelect.options]
      .filter(matchesPhone)
      .sort((a, b) => optionDialCode(b).length - optionDialCode(a).length)[0];
    if (option && nativeSelect.value !== option.value) {
      nativeSelect.value = option.value;
      nativeSelect.dispatchEvent(new Event('input', { bubbles: true }));
      nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return { ok: !!option, method: 'native', country: optionText(option), dialCode: optionDialCode(option) };
  }

  const tel = document.querySelector("input[type='tel'], input[autocomplete='tel'], input[inputmode='tel']");
  const triggers = [...document.querySelectorAll("button[aria-haspopup='listbox'], [role='combobox']")]
    .filter(visible);
  triggers.sort((left, right) => {
    if (!tel) return 0;
    const top = tel.getBoundingClientRect().top;
    return Math.abs(left.getBoundingClientRect().top - top) - Math.abs(right.getBoundingClientRect().top - top);
  });
  const trigger = triggers[0];
  if (!trigger) return { ok: false, phase: 'trigger_missing' };
  const triggerCode = optionDialCode(trigger);
  if (triggerCode && digits.startsWith(triggerCode)) {
    return { ok: true, method: 'already_selected', country: optionText(trigger), dialCode: triggerCode };
  }

  const listbox = [...document.querySelectorAll('[role="listbox"]')].find(visible);
  if (!listbox) {
    trigger.scrollIntoView({ block: 'center', inline: 'nearest' });
    trigger.click();
    return { ok: false, phase: 'opened' };
  }
  const option = [...listbox.querySelectorAll('[role="option"]')]
    .filter(matchesPhone)
    .sort((a, b) => optionDialCode(b).length - optionDialCode(a).length)[0];
  if (!option) {
    let scroller = listbox;
    for (let node = listbox; node && node !== document.body; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight + 4) {
        scroller = node;
        break;
      }
    }
    const step = Math.max(240, scroller.clientHeight || 240);
    scroller.scrollTop = Math.min(scroller.scrollTop + step, scroller.scrollHeight);
    scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
    return { ok: false, phase: 'waiting_for_phone_country_option' };
  }
  option.scrollIntoView({ block: 'center', inline: 'nearest' });
  option.click();
  return { ok: false, phase: 'selected', country: optionText(option), dialCode: optionDialCode(option) };
})();
"""


def select_phone_country(driver, phone: str, *, timeout: float = 12.0) -> dict:
    """Select and verify the country whose dial code matches an E.164 phone."""
    deadline = time.time() + max(0.0, float(timeout))
    last_result: dict = {}
    while time.time() < deadline:
        try:
            result = driver.execute_script(_SELECT_PHONE_COUNTRY_SCRIPT, phone) or {}
        except WebDriverException as exc:
            result = {"ok": False, "phase": "script_error", "error": str(exc)[:160]}
        if not isinstance(result, dict):
            result = {"ok": False, "phase": "invalid_script_result"}
        last_result = result
        if result.get("ok"):
            return result
        time.sleep(0.25)
    phone_text = str(phone or '').strip()
    display_phone = phone_text if phone_text.startswith('+') else f'+{phone_text}'
    raise RuntimeError(
        f"Không thể chọn country theo số điện thoại {display_phone}: last={last_result}"
    )
