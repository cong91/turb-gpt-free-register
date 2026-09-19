"""注册失败分类统计：把 error_message 归并为稳定失败类，供 /api/jobs/failure-stats 展示。

分类只用于运维观测（定位"这批为什么挂"），顺序即优先级：越靠前越具体，
例如 "2FA 设置失败，账号已保存：re-auth 未进入 email-verification" 归入
twofa_reauth_no_otp_page 而不是笼统的 twofa_setup_failed_saved。
"""
from __future__ import annotations

import re

UNCLASSIFIED = "other"

# (class, pattern) — 顺序敏感：具体的类放在宽泛的类之前。
_FAILURE_CLASSIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "alloc_failed",
        re.compile(
            r"所有邮箱来源均领取失败"
            r"|No Gmail API URL source available"
            r"|Gmail API source pool exhausted",
            re.IGNORECASE,
        ),
    ),
    ("session_token_timeout", re.compile(r"accessToken 超时|access.?token.?timeout", re.IGNORECASE)),
    ("resend_button_missing", re.compile(r"找不到可点击的重新发送验证码按钮")),
    ("otp_timeout", re.compile(r"Timeout after \d+s waiting for new OTP|等待邮箱验证码超时|等待新 ?OTP")),
    ("twofa_reauth_no_otp_page", re.compile(r"re-auth 未进入 email-verification")),
    ("twofa_setup_failed_saved", re.compile(r"2FA 设置失败，账号已保存")),
    ("provider_602", re.compile(r"provider error code=602|code=602", re.IGNORECASE)),
    ("email_already_registered", re.compile(r"按已注册/不可用邮箱处理并停用")),
    ("email_input_missing", re.compile(r"找不到邮箱输入框")),
    ("password_page_stuck", re.compile(r"密码页提交后未进入邮箱验证码页|邮箱提交后未进入密码页")),
    ("password_submit_retry", re.compile(r"密码页提交失败")),
    ("proxy_or_network", re.compile(r"chrome-error|net::ERR_", re.IGNORECASE)),
    ("codex_plan_403", re.compile(r"套餐查询失败")),
)


def classify_failure(error_message: object) -> str:
    """归并单条失败信息为失败类；无法识别时返回 other。"""
    text = str(error_message or "")
    if not text.strip():
        return UNCLASSIFIED
    for name, pattern in _FAILURE_CLASSIFIERS:
        if pattern.search(text):
            return name
    return UNCLASSIFIED


def failure_class_counts(limit: int = 5000) -> dict[str, int]:
    """统计失败任务按类别计数；只返回计数，不含邮箱/令牌等敏感内容。"""
    from core import db

    counts: dict[str, int] = {}
    for error_message in db.iter_failed_job_errors(limit=limit):
        name = classify_failure(error_message)
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))
