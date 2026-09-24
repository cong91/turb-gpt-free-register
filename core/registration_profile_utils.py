"""Classify and poll delayed about-you/profile submission errors.

The module also owns the stable failure-message contract and classification used
by registration retry and release policies. Drivers provide page snapshots and
remain responsible for logging and raising provider-specific failures.
"""
from __future__ import annotations

import time
from collections.abc import Callable

# 分类生效的 URL 白名单：只有停在这些页面才算资料页提交错误。
_PROFILE_URL_MARKERS = ("about-you", "profile", "create-account/about", "signup/profile")

# user_already_exists 类 marker —— 与 registration_service 的 alias matcher
# （registration_service.py "user_already_exists" / "already exists for this
# email"）保持同一语义。
_ALREADY_EXISTS_MARKERS = (
    "an account already exists for this email address",
    "account already exists for this email",
    "user_already_exists",
    "please log in instead",
)


def profile_submission_error(snapshot: dict) -> str | None:
    """Return a terminal about-you error reported by the provider, if present."""
    url = str(snapshot.get("url") or "").lower()
    if not any(x in url for x in _PROFILE_URL_MARKERS):
        return None
    messages = [str(snapshot.get("text") or "")]
    messages.extend(str(value or "") for value in (snapshot.get("errors") or []))
    text = " ".join(messages).replace("\\n", " ").strip()
    markers = (
        "利用規約のため、お客様のアカウントを作成できません。",
        "利用規約のため、お客様のアカウントを作成できません",
        "cannot create your account due to the terms of use.",
        "cannot create your account due to the terms",
        "cannot create your account because of the terms of use.",
        "cannot create your account because of the terms",
        "this email is not supported.",
        "this email is not supported",
        "email address is not supported.",
        "email address is not supported",
        "email is not supported",
        "email domain is not supported.",
        "email domain is not supported",
        "unsupported email.",
        "unsupported email",
        "email not supported",
        "email is unsupported",
        "email isn't supported",
        # Email đã có account server-side (vd WARNING_BANNER lần trước đã tạo
        # account nhưng job chết): terminal cho alias này, retry phải lấy alias
        # mới — đăng ký lại chắc chắn gặp lại lỗi này.
        "an account already exists for this email address",
        "account already exists for this email",
        "user_already_exists",
        "please log in instead",
    )
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            start = lowered.find(marker.lower())
            return text[start:start + len(marker)]
    if "cannot create your account" in lowered and "terms" in lowered:
        start = lowered.find("cannot create your account")
        return text[start:start + 500]
    return None


def poll_profile_submission_error(
    take_snapshot: Callable[[], dict],
    *,
    duration: float = 20.0,
    interval: float = 2.0,
    left_profile: Callable[[dict], bool] | None = None,
    classify: Callable[[dict], str | None] = profile_submission_error,
) -> str | None:
    """提交后轮询延迟渲染的 about-you 错误。

    sleep-first 语义：每轮先 sleep(interval) 再 snapshot —— 页面错误在点击后
    ~15s 才渲染（batch 2026-09-21）。classify 命中则返回错误字符串；
    left_profile 为 True 或时间用尽则返回 None（由 driver 决定后续日志/raise）。
    """
    poll_end = time.time() + duration
    while time.time() < poll_end:
        time.sleep(interval)
        snapshot = take_snapshot()
        profile_error = classify(snapshot)
        if profile_error:
            return profile_error
        if left_profile is not None and left_profile(snapshot):
            return None
    return None


def profile_submission_failure_message(profile_error: str) -> str:
    """message 契约 "about-you 提交失败：<错误>" 的唯一来源。"""
    return f"about-you 提交失败：{profile_error}"


def is_unsupported_email_error(error: object) -> bool:
    """Return whether an about-you failure explicitly rejects the email/domain."""
    text = str(error or "").lower()
    if "about-you 提交失败" not in text:
        return False
    return any(marker in text for marker in (
        "this email is not supported",
        "email is not supported",
        "email address is not supported",
        "email domain is not supported",
        "unsupported email",
        "email not supported",
        "email is unsupported",
        "email isn't supported",
    ))


def is_already_exists_error(error: object) -> bool:
    """Return whether the about-you failure means the email already has an account."""
    text = str(error or "").lower()
    return any(marker in text for marker in _ALREADY_EXISTS_MARKERS)
