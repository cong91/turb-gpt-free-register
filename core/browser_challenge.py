"""Detect and wait for browser-level anti-bot challenge pages."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "performing security verification",
    "しばらくお待ちください",
    "请稍候",
    "請稍候",
    "安全验证",
    "安全驗證",
    "恶意机器人",
    "惡意機器人",
    "chờ một chút",
)


def inspect_turnstile_response(driver, state: dict | None = None) -> dict:
    """Inspect the response source without returning the response value."""
    if state is None:
        try:
            state = driver.execute_script(
                r"""
const input = document.querySelector('input[name="cf-turnstile-response"]');
const inputValue = String((input && input.value) || '').trim();
const apiAvailable = !!(
  window.turnstile && typeof window.turnstile.getResponse === 'function'
);
let apiHasResponse = false;
if (apiAvailable) {
  try {
    apiHasResponse = !!String(window.turnstile.getResponse() || '').trim();
  } catch (_) {
    apiHasResponse = false;
  }
}
const source = inputValue
  ? 'input[name=cf-turnstile-response]'
  : apiHasResponse
    ? 'turnstile.getResponse()'
    : 'none';
return {
  input_present: !!input,
  api_available: apiAvailable,
  source,
};
            """
            ) or {}
        except Exception as exc:  # noqa: BLE001 - browser backends expose different script exceptions
            return {
                "input_present": False,
                "api_available": False,
                "source": "unavailable",
                "error": type(exc).__name__,
            }

    if not isinstance(state, dict):
        state = {}
    return {
        "input_present": bool(state.get("input_present")),
        "api_available": bool(state.get("api_available")),
        "source": str(
            state.get("turnstile_response_source")
            or state.get("source")
            or "none"
        ),
    }


def browser_challenge_state(driver) -> dict:
    """Return a small, credential-free snapshot of a browser challenge state."""
    try:
        state = driver.execute_script(
            r"""
const visible = el => !!el
  && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  && getComputedStyle(el).visibility !== 'hidden'
  && getComputedStyle(el).display !== 'none'
  && !el.disabled;
const title = String(document.title || '').trim();
const bodyText = String(document.body?.innerText || '').slice(0, 2000);
const responseInput = document.querySelector('input[name="cf-turnstile-response"]');
const inputResponse = String((responseInput && responseInput.value) || '').trim();
const turnstileIframes = [...document.querySelectorAll(
  'iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"]'
)];
const visibleCheckboxes = [...document.querySelectorAll(
  'input[type="checkbox"],[role="checkbox"]'
)].filter(el => visible(el));
const hasEmailInput = [...document.querySelectorAll(
  'input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'
)].some(visible);
const checkboxText = /(?:select|check|click|verify)[^\n]{0,60}checkbox|i\s+am\s+human/i.test(bodyText);

const hasChallengeCheckbox = visibleCheckboxes.length > 0
  && (checkboxText || !hasEmailInput);

const apiAvailable = !!(
  window.turnstile && typeof window.turnstile.getResponse === 'function'
);
let apiHasResponse = false;
if (apiAvailable) {
  try {
    apiHasResponse = !!String(window.turnstile.getResponse() || '').trim();
  } catch (_) {
    apiHasResponse = false;
  }
}
const turnstileResponseSource = inputResponse
  ? 'input[name=cf-turnstile-response]'
  : apiHasResponse
    ? 'turnstile.getResponse()'
    : 'none';
const hasChallengeWidget = !!document.querySelector(
  'iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"],'
  'input[name="cf-turnstile-response"],div.cf-turnstile'
) || hasChallengeCheckbox;
return {
  url: String(location.href || ''),
  title,
  body_text: bodyText,
  turnstile_response_source: turnstileResponseSource,
  turnstile_input_present: !!responseInput,
  turnstile_api_available: apiAvailable,
  turnstile_iframe_count: turnstileIframes.length,
  visible_checkbox_count: visibleCheckboxes.length,
  has_challenge_checkbox: hasChallengeCheckbox,
  has_email_input: hasEmailInput,
  has_challenge_widget: hasChallengeWidget,
};
            """
        ) or {}
    except Exception as exc:  # noqa: BLE001 - browser backends expose different script exceptions
        return {
            "url": str(getattr(driver, "current_url", "") or ""),
            "title": "",
            "body_text": "",
            "has_email_input": False,
            "has_challenge_widget": False,
            "has_challenge_checkbox": False,
            "turnstile_response_source": "unavailable",
            "turnstile_response_ready": False,
            "challenge_completed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "is_challenge": False,
        }

    if not isinstance(state, dict):
        state = {}
    turnstile_observation = inspect_turnstile_response(driver, state=state)
    title = str(state.get("title") or "")
    body_text = str(state.get("body_text") or "")
    combined = f"{title}\n{body_text}".lower()
    marker = next((item for item in _CHALLENGE_MARKERS if item.lower() in combined), "")
    has_challenge_checkbox = bool(state.get("has_challenge_checkbox"))
    has_challenge_widget = bool(
        state.get("has_challenge_widget") or has_challenge_checkbox
    )
    response_ready = turnstile_observation["source"] not in {"", "none", "unavailable"}
    is_challenge = bool(not response_ready and (marker or has_challenge_widget))
    reason = marker or (
        "challenge checkbox"
        if has_challenge_checkbox
        else "challenge widget without email form"
        if has_challenge_widget
        else ""
    )
    return {
        **state,
        "turnstile_response_source": turnstile_observation["source"],
        "turnstile_response_ready": response_ready,
        "challenge_completed": response_ready,
        "is_challenge": is_challenge,
        "reason": reason,
    }


def describe_browser_challenge(driver) -> dict:
    """Detect a challenge and print the remaining lesson steps.

    This is the non-blocking entry point for the exercise. It reads the
    current browser state once and explains what the next stage must do; it
    deliberately does not sleep, click a real challenge, or read a token.
    """
    state = browser_challenge_state(driver)
    if not state.get("is_challenge"):
        logger.info(
            "[%s] Browser challenge not detected; continue with the page flow",
            getattr(driver, "_registration_log_prefix", "Browser"),
        )
        return {**state, "next_steps": []}

    next_steps = [
        "complete the checkbox challenge in the browser",
        "observe the response input or Turnstile API",
        "dispatch the form only after challenge completion",
    ]
    logger.info(
        "[%s] Browser challenge detected: url=%s reason=%s source=%s",
        getattr(driver, "_registration_log_prefix", "Browser"),
        str(state.get("url") or "")[:180],
        str(state.get("reason") or "")[:120],
        str(state.get("turnstile_response_source") or "none"),
    )
    for index, step in enumerate(next_steps, start=1):
        logger.info("[%s] Lesson step %s: %s", getattr(driver, "_registration_log_prefix", "Browser"), index, step)
    return {**state, "next_steps": next_steps}


def complete_owned_lab_challenge(driver) -> dict:
    """Click the explicitly marked checkbox on the local classroom lab only."""
    current_url = str(getattr(driver, "current_url", "") or "")
    hostname = urlsplit(current_url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "complete_owned_lab_challenge is restricted to an owned classroom lab"
        )

    result = driver.execute_script(
        r'''
const checkbox = document.querySelector(
  'input[data-classroom-challenge="true"]'
);
if (!checkbox) {
  throw new Error('classroom challenge checkbox was not found');
}
if (checkbox.disabled) {
  throw new Error('classroom challenge checkbox is disabled');
}
checkbox.click();
return {
  clicked: true,
  checked: checkbox.checked === true,
};
'''
    ) or {}
    if not result.get("clicked"):
        raise RuntimeError("classroom challenge checkbox was not clicked")
    return result


def run_owned_lab_challenge(driver) -> dict:
    """Connect the existing lesson functions for one local-lab run."""
    initial_state = describe_browser_challenge(driver)
    if not initial_state.get("is_challenge"):
        return initial_state

    action = complete_owned_lab_challenge(driver)
    final_state = browser_challenge_state(driver)
    return {
        **final_state,
        "initial_state": initial_state,
        "action": action,
    }


def wait_for_browser_challenge(
    driver,
    timeout: float = 45,
) -> dict:
    """Run the browser challenge gate used by the registration flow.

    Reading order for students:
    1. Read the current page and widget state from ``driver``.
    2. If a challenge is present, let the browser/authorized user complete it.
    3. Poll the same driver until the response source is visible in the DOM.
    4. Return the credential-free final state to the caller.

    This function observes completion; it does not solve the challenge or
    expose the response value.
    """
    state = browser_challenge_state(driver)
    if not state.get("is_challenge"):
        return state

    wait_timeout = max(0.0, float(timeout))
    logger.info(
        "[%s] Browser challenge detected; waiting up to %.1fs: url=%s title=%s reason=%s source=%s",
        getattr(driver, "_registration_log_prefix", "Browser"),
        wait_timeout,
        str(state.get("url") or "")[:180],
        str(state.get("title") or "")[:120],
        str(state.get("reason") or "")[:120],
        str(state.get("turnstile_response_source") or "none"),
    )
    # The browser challenge is completed outside this registration function.
    # The loop only observes the driver's state after each short interval.
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        # A legitimate Turnstile callback may update the hidden input while
        # its iframe remains mounted, so response readiness ends the wait.
        state = browser_challenge_state(driver)
        if not state.get("is_challenge"):
            logger.info(
                "[%s] Browser challenge finished; next page is available: url=%s source=%s",
                getattr(driver, "_registration_log_prefix", "Browser"),
                str(state.get("url") or "")[:180],
                str(state.get("turnstile_response_source") or "none"),
            )
            return state

    raise RuntimeError(
        "browser challenge was not completed before timeout: "
        f"url={str(state.get('url') or '')[:180]} "
        f"title={str(state.get('title') or '')[:120]} "
        f"reason={str(state.get('reason') or '')[:120]} "
        f"source={state.get('turnstile_response_source') or 'none'!s}"
    )
