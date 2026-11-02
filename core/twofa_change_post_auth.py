"""Codex follow-up after a persisted 2FA browser change."""
from __future__ import annotations

import logging
from collections.abc import Callable

from core.account_security import TwofaChangeInput

logger = logging.getLogger(__name__)


def _notify_progress(callback, index: int | None, update: dict[str, object]) -> None:
    if callback is None or index is None:
        return
    try:
        callback(index, dict(update))
    except Exception:
        logger.debug("2FA progress callback failed")


def _codex_skipped(email: str, reason: str) -> dict[str, object]:
    outcome: dict[str, object] = {"status": "skipped", "ok": False, "reason": reason}
    try:
        from core.personal_info_codex import record_personal_info_codex

        record_personal_info_codex(email, outcome)
    except Exception:
        outcome["status_persisted"] = False
    return outcome


def record_skipped_change(email: str, result: dict[str, object]) -> None:
    result["codex"] = _codex_skipped(
        email,
        "Bỏ qua: thao tác đổi 2FA chưa thành công; trạng thái đổi 2FA được giữ nguyên.",
    )


def record_failed_codex(
    email: str, result: dict[str, object], *, identity_unverified: bool = False,
) -> None:
    outcome: dict[str, object] = {
        "status": "failed",
        "ok": False,
        "reason": "Đăng nhập Codex thất bại. Thao tác đổi 2FA vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản.",
    }
    if identity_unverified:
        outcome["reason"] = (
            "Không thể xác minh tài khoản trong phiên trình duyệt; không chạy Codex. "
            "Thao tác đổi 2FA vẫn thành công; có thể chạy Codex bổ sung từ trang Tài khoản."
        )
    try:
        from core.personal_info_codex import record_personal_info_codex

        record_personal_info_codex(email, outcome)
    except Exception:
        outcome["status_persisted"] = False
    result["codex"] = outcome


def run_twofa_post_auth(
    *,
    account_id: int,
    item: TwofaChangeInput,
    access_token: str,
    profile,
    active_proxy: str | None,
    new_totp_secret: str,
    persisted: bool,
    login_codex_after_change: bool,
    progress_callback: Callable[[int, dict[str, object]], None] | None,
    progress_index: int | None,
    result: dict[str, object],
) -> None:
    """Add Codex outcomes without changing the persisted MFA result."""
    if access_token and profile is not None and profile.driver is not None:
        from core.twofa_change_plan_check import run_twofa_plan_check

        run_twofa_plan_check(account_id, item, access_token, profile, result)

    if not login_codex_after_change:
        return
    if not persisted or not new_totp_secret:
        result["codex"] = _codex_skipped(
            item.email,
            "Bỏ qua: đổi thông tin chưa thành công; thao tác đổi 2FA vẫn được giữ nguyên trạng thái.",
        )
        return
    if profile is None:
        result["codex"] = _codex_skipped(
            item.email,
            "Bỏ qua đăng nhập Codex: phiên trình duyệt hiện tại không còn khả dụng",
        )
        return
    if str(getattr(profile, "provider", "") or "").strip().lower() == "skyvern" and active_proxy is not None:
        result["codex"] = _codex_skipped(
            item.email,
            "Bỏ qua đăng nhập Codex: Skyvern không hỗ trợ proxy tùy chỉnh của phiên hiện tại; thao tác đổi 2FA vẫn thành công.",
        )
        return

    _notify_progress(
        progress_callback,
        progress_index,
        {"status": "running", "email": item.email, "codex": {"status": "running", "ok": False}},
    )
    try:
        from core.personal_info_codex import (
            _codex_route_skip_reason,
            run_personal_info_codex_and_record,
            verify_live_identity,
        )

        route_skip = _codex_route_skip_reason(profile, active_proxy)
        if route_skip:
            result["codex"] = _codex_skipped(item.email, route_skip)
            return
        try:
            verify_live_identity(profile.driver, item.email)
        except Exception:  # noqa: BLE001 - every verification error must fail closed.
            record_failed_codex(item.email, result, identity_unverified=True)
            return
        result["codex"] = run_personal_info_codex_and_record(
            email=item.email,
            profile=profile,
            active_proxy=active_proxy,
            password=item.password,
            totp_secret=new_totp_secret,
        )
    except Exception:
        record_failed_codex(item.email, result)
