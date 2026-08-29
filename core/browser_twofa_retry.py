"""Provider-neutral retry workflow for completing 2FA on an existing account."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

from core import db
from core.account_export import (
    BrowserPageTransport,
    save_account_data,
    setup_2fa_in_page,
)
from core.browser_profile import open_browser_profile
from core.browser_twofa_login import _login_existing_account
from core.email_provider import resolve_email_source
from core.humanize import delay as human_delay
from core.rotating_proxy_runtime import TWOFA_RETRY_PROXY_SCOPE, resolve_rotating_proxy

logger = logging.getLogger(__name__)


def _account_credentials(account: dict) -> tuple[int, str, str] | None:
    account_id = int(account.get("id") or 0)
    email = str(account.get("email") or "").strip()
    password = str(account.get("registration_password") or "").strip()
    if not account_id or not email or not password:
        return None
    return account_id, email, password


def _failure_result(account_id: int, email: str, error: str) -> dict[str, object]:
    db.update_account_2fa(account_id, status="failed", error=error)
    return {
        "ok": False,
        "status": "failed",
        "email": email,
        "account_id": account_id,
        "message": error,
    }


@contextmanager
def _retry_browser_profile(
    proxy: str | None,
    *,
    lease_owner_id: str | None = None,
) -> Iterator[tuple[object, str | None]]:
    """Open the retry browser with an explicit proxy lease when configured."""
    if proxy is None:
        from core.nordvpn_wireguard import proxy_for_registration

        proxy_context = (
            proxy_for_registration(owner_id=lease_owner_id)
            if lease_owner_id is not None
            else proxy_for_registration()
        )
    else:
        proxy_context = nullcontext(proxy)

    with proxy_context as active_proxy:
        profile = None
        try:
            profile = open_browser_profile(proxy=active_proxy)
            yield profile, active_proxy
        finally:
            if profile is not None:
                try:
                    profile.close()
                except Exception as exc:  # noqa: BLE001 - driver shutdown is best effort.
                    logger.debug("[Browser 2FA] driver cleanup failed: %s", exc)
                try:
                    profile.cleanup()
                except Exception as exc:  # noqa: BLE001 - profile cleanup is best effort.
                    logger.debug("[Browser 2FA] profile cleanup failed: %s", exc)


def run_twofa_retry(
    account: dict,
    *,
    max_attempts: int = 2,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
    lease_owner_id: str | None = None,
) -> dict[str, object]:
    """Log in to an existing account and complete its pending 2FA state."""
    credentials = _account_credentials(account)
    if credentials is None:
        account_id = int(account.get("id") or 0)
        email = str(account.get("email") or "").strip()
        if not account_id or not email:
            return {"ok": False, "status": "failed", "message": "账号信息不完整，无法补做 2FA"}
        return {
            "ok": False,
            "status": "failed",
            "email": email,
            "account_id": account_id,
            "message": "账号缺少 OpenAI 登录密码，无法补做 2FA",
        }

    account_id, email, password = credentials
    last_error = ""
    try:
        active_proxy_input = resolve_rotating_proxy(
            proxy,
            scope=TWOFA_RETRY_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        with _retry_browser_profile(
            active_proxy_input,
            lease_owner_id=lease_owner_id,
        ) as (profile, active_proxy):
            attempts = max(1, int(max_attempts))
            for attempt in range(1, attempts + 1):
                try:
                    logger.info(
                        "[Browser 2FA] 开始第 %s/%s 次已有账号登录：%s provider=%s",
                        attempt,
                        attempts,
                        email,
                        profile.provider,
                    )
                    session_info = _login_existing_account(
                        profile.driver,
                        email,
                        password,
                        timeout=profile.timeout,
                    )
                    secret = setup_2fa_in_page(profile.driver, email, reauth=True)
                    if not secret:
                        raise RuntimeError("2FA 流程未返回 TOTP secret")

                    codex_result = {
                        "status": "skipped",
                        "ok": True,
                        "message": "2FA retry did not run Codex",
                    }
                    try:
                        from config import codex as codex_config
                        from config import register as register_config

                        post_auth_automation_enabled = bool(
                            getattr(register_config, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
                            or getattr(register_config, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
                            or getattr(codex_config, "ENABLE_CODEX_AUTO", False)
                        )
                        if post_auth_automation_enabled:
                            from core.codex_login_credentials import (
                                CodexLoginCredentials,
                            )
                            from core.registration_auto_codex import (
                                run_registration_auto_codex,
                            )
                            from core.roxy_codex_oauth import run_roxy_codex_oauth

                            credentials_for_codex = CodexLoginCredentials(
                                email=email,
                                password=password,
                                totp_secret=secret,
                            )
                            existing_opened = SimpleNamespace(
                                profile_id=profile.provider,
                                raw={"driver": profile.provider},
                            )

                            def _run_codex_in_current_browser(
                                _existing_opened=existing_opened,
                                _credentials=credentials_for_codex,
                            ) -> dict:
                                return run_roxy_codex_oauth(
                                    email,
                                    existing_driver=profile.driver,
                                    existing_opened=_existing_opened,
                                    reuse_existing_profile=True,
                                    force=True,
                                    clear_existing_state=True,
                                    credentials=_credentials,
                                )

                            auto_codex = run_registration_auto_codex(
                                account_id=account_id,
                                email=email,
                                access_token=session_info.get("accessToken")
                                or str(account.get("access_token") or ""),
                                proxy=active_proxy,
                                browser_transport=BrowserPageTransport(profile.driver),
                                run_codex=_run_codex_in_current_browser,
                                twofa_status="active",
                            )
                            codex_result = auto_codex["codex"]
                    except Exception as exc:  # noqa: BLE001 - preserve active account for later Codex retry.
                        codex_result = {
                            "status": "failed",
                            "ok": False,
                            "message": f"{type(exc).__name__}: {str(exc)[:180]}",
                        }

                    saved_id = save_account_data(
                        email=email,
                        access_token=session_info.get("accessToken") or str(account.get("access_token") or ""),
                        totp_secret=secret,
                        email_source=resolve_email_source(email),
                        proxy_used=active_proxy or account.get("proxy_used"),
                        registration_ip=account.get("registration_ip"),
                        extra={
                            "user": session_info.get("user"),
                            "account": session_info.get("account"),
                            "expires": session_info.get("expires"),
                            "registration_password": password,
                            "registration_driver": profile.provider,
                            "twofa_status": "active",
                            "twofa_error": None,
                            "twofa_retry_source_account_id": account_id,
                            "codex": codex_result,
                        },
                        auto_plan_check=False,
                    )
                    codex_ok = bool(codex_result.get("ok")) or codex_result.get("status") == "skipped"
                    return {
                        "ok": codex_ok,
                        "status": "success",
                        "email": email,
                        "account_id": saved_id,
                        "access_token": session_info.get("accessToken"),
                        "totp_secret": secret,
                        "browser_provider": profile.provider,
                        "codex": codex_result,
                    }
                except Exception as exc:  # noqa: BLE001 - isolate each retry attempt.
                    last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    logger.warning(
                        "[Browser 2FA] 第 %s/%s 次失败：provider=%s error=%s",
                        attempt,
                        attempts,
                        profile.provider,
                        last_error,
                    )
                    if attempt < attempts:
                        try:
                            profile.driver.get("https://chatgpt.com/auth/login")
                            human_delay("navigate")
                        except Exception as exc:  # noqa: BLE001 - retry navigation is best effort.
                            logger.debug("[Browser 2FA] retry navigation failed: %s", exc)
            return _failure_result(account_id, email, last_error)
    except Exception as exc:  # noqa: BLE001 - isolate the browser workflow.
        last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return _failure_result(account_id, email, last_error)
