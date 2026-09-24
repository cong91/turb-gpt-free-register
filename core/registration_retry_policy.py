"""Classify pre-account registration failures that may receive one fresh job."""

_TERMINAL_MARKERS = (
    "account deactivated",
    "account disabled",
    "account locked",
    "password incorrect",
    "invalid password",
    "code=602",
    "provider error code=602",
    "checkout_blocked",
    "checkout blocked",
    "out_of_stock",
    "out of stock",
    "sold out",
    "insufficient stock",
    "stock unavailable",
    "request blocked by security policy",
)

_TRANSIENT_MARKERS = (
    "vui long thu lai",
    "vui lòng thử lại",
    "please try again",
    "password page submit failed",
    "密码页提交失败",
    "未进入邮箱验证码页",
    "未进入密码页",
    "access token 超时",
    "accesstoken 超时",
    "access token timeout",
    "accesstoken timeout",
    "waiting for new otp",
    "等待新 otp",
    "等待邮箱验证码超时",
    "找不到可点击的重新发送验证码按钮",
)


def should_auto_retry_registration_failure(
    error: object,
    *,
    email_source: str,
    retry_attempt: int,
    max_attempts: int,
) -> bool:
    """Return whether a failed pre-account job warrants one fresh registration job."""
    if int(max_attempts or 0) <= int(retry_attempt or 0):
        return False
    message = str(error or "").strip().lower()
    if not message or any(marker in message for marker in _TERMINAL_MARKERS):
        return False
    return any(marker in message for marker in _TRANSIENT_MARKERS)
