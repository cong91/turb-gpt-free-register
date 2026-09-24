"""Account-unusable probes and shared release-status policy."""
from __future__ import annotations

import logging
import time

from core.openai_auth import (
    AccountUnusableError,
    account_unusable_error_message,
    detect_account_unusable_text,
)
from core.registration_profile_utils import is_unsupported_email_error

logger = logging.getLogger(__name__)


def account_unusable_page_code(driver) -> str:
    """Read a Selenium-compatible driver or Playwright page body and classify it."""
    if isinstance(driver, str):
        return detect_account_unusable_text(driver)
    execute_script = getattr(driver, "execute_script", None)
    if callable(execute_script):
        from selenium.common.exceptions import WebDriverException

        try:
            body = execute_script("return document.body?.innerText || ''; ")
        except WebDriverException:
            return ""
    else:
        locator = getattr(driver, "locator", None)
        if not callable(locator):
            return ""
        try:
            body = locator("body").inner_text(timeout=1000)
        except Exception:  # noqa: BLE001 - a page disappearing during observation is not a dead account.
            return ""
    return detect_account_unusable_text(str(body or ""))


def raise_if_account_unusable(driver) -> None:
    """Trang hiện tại là trang账号停用/删除 → raise AccountUnusableError giữa flow."""
    code = account_unusable_page_code(driver)
    if code:
        raise AccountUnusableError(account_unusable_error_message(code), error_code=code)


def wait_after_password_submit(driver, initial_url: str, timeout: float = 5.0) -> None:
    """等待密码提交结果；账号停用页出现时立即停止后续步骤。"""
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        raise_if_account_unusable(driver)
        current_url = str(getattr(driver, "current_url", "") or "")
        if current_url and current_url != initial_url:
            return
        time.sleep(0.25)


def is_account_unusable_failure(error: object) -> bool:
    """Failure có nghĩa mailbox/OpenAI account đã废 (删除/停用/封禁)，重试无意义。"""
    if isinstance(error, AccountUnusableError):
        return True
    text = str(error or "")
    return any(
        marker in text
        for marker in (
            "AccountUnusableError",
            "account_deactivated",
            "account_deleted",
            "account_banned",
        )
    )


def release_status_for_failure(exc: BaseException, *, create_acknowledged: bool = False) -> str:
    """Release-status policy superset: disabled / failed / available.

    - disabled:账号已废（AccountUnusableError / dead-code text）或 mailbox bị
      OpenAI từ chối (unsupported email) — không tái sử dụng email này.
    - failed: đã submit password (create_acknowledged) hoặc dừng ở login-password
      page (email đã có account) — alias tiêu tốn nhưng root email còn tốt.
    - available: các lỗi khác (mạng/thay thế IP tạm thời) — email quay lại pool.
    """
    error_text = str(exc)
    if (
        isinstance(exc, AccountUnusableError)
        or is_account_unusable_failure(exc)
        or is_unsupported_email_error(error_text)
    ):
        return "disabled"
    if (
        create_acknowledged
        or "邮箱提交后进入登录密码页" in error_text
        or "auth.openai.com/log-in/password" in error_text
        or "/log-in/password" in error_text
    ):
        return "failed"
    return "available"
