"""通过 RoxyBrowser 指纹浏览器 + Selenium 执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from pathlib import Path

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core import db, registration_flow
from core.account_export import (
    BrowserPageTransport,
    post_register_dwell,
    save_account_data,
)
from core.browser_page_actions import (
    _log_prefix,
    _safe_get,
)
from core.browser_selenium_adapter import (
    build_selenium_driver as _build_driver,
)
from core.browser_selenium_adapter import (
    center_browser_window as _center_browser_window,
)
from core.browser_traffic import SeleniumTrafficTracker
from core.roxy_asset_cache import RoxyLocalAssetCache
from core.email_provider import (
    acquire_email_after_input,
    resolve_email_source,
    snapshot_verification_code,
)
from core.humanize import delay as human_delay
from core.registration_flow import (
    _complete_email_otp,
    release_registration_email_on_failure,
    run_registration_page_flow,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)


# 连续多次仅返回 WARNING_BANNER 视为网关拦截：先刷新一次页面，刷新后仍拦截则快速失败。
# 恢复流程（fast_fail=False）在主轮失败后继续等待重读的轮数，每轮独立短超时。
_SESSION_RECOVERY_REPOLL_ROUNDS = 2


def _recover_chatgpt_session(
    driver,
    email: str,
    first_error: Exception,
) -> dict:
    """session 拿不到 accessToken 时的恢复链（不能一次轮询失败就把 job 判死）。

    1. 等待片刻后重读 /api/auth/session（_SESSION_RECOVERY_REPOLL_ROUNDS 次）；
    2. 刷新 ChatGPT 页面后重读；
    3. 仍拿不到 → 用 2FA 同款 re-auth 邮箱 OTP 重新登录（auth.openai.com 已有
       本轮注册的 cookie），重建 session 后再读。

    全部失败时抛出原始 first_error（保留 WARNING_BANNER / _http_status 200
    marker，供 registration_service 的 retry 分类识别并换 IP 自动重试）。
    """
    last_error = first_error
    for repoll in range(1, _SESSION_RECOVERY_REPOLL_ROUNDS + 1):
        wait_seconds = random.uniform(8.0, 14.0)
        logger.warning(
            "%s session 恢复 %s/%s：等待 %.0fs 后重读 accessToken（上轮错误：%s）",
            _log_prefix(driver),
            repoll,
            _SESSION_RECOVERY_REPOLL_ROUNDS,
            wait_seconds,
            str(last_error)[:160],
        )
        time.sleep(wait_seconds)
        _check_manual_stop()
        try:
            return registration_flow._fetch_chatgpt_session(driver, timeout=35, auto_jump_wait=8)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    logger.warning("%s session 恢复：刷新 ChatGPT 页面后重读 accessToken", _log_prefix(driver))
    try:
        driver.refresh()
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s session 恢复：刷新失败，改为重新打开 ChatGPT：%s: %s",
            _log_prefix(driver),
            type(exc).__name__,
            exc,
        )
        _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=1, accept_hosts=("chatgpt.com",))
    try:
        return registration_flow._fetch_chatgpt_session(driver, timeout=35, auto_jump_wait=8)
    except Exception as exc:  # noqa: BLE001
        last_error = exc

    logger.warning(
        "%s session 恢复：auth.openai.com 已有本轮 cookie，用 re-auth API（2FA 同款）重新登录（email=%s）",
        _log_prefix(driver),
        email,
    )
    from core.account_export import reauth_login_after_session_timeout

    try:
        reauth_login_after_session_timeout(driver, email)
    except Exception as reauth_exc:
        logger.error(
            "%s session 恢复：re-auth 重新登录失败：%s",
            _log_prefix(driver),
            str(reauth_exc)[:200],
        )
        raise first_error from reauth_exc
    _check_manual_stop()
    try:
        return registration_flow._fetch_chatgpt_session(driver, timeout=60, auto_jump_wait=8)
    except Exception as final_exc:
        logger.error(
            "%s session 恢复：重新登录后仍未读到 accessToken：%s",
            _log_prefix(driver),
            str(final_exc)[:200],
        )
        raise first_error from final_exc


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def run_roxy_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    client = RoxyBrowserClient()
    opened = None
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    account_id: int | None = None
    network_identity: dict | None = None
    traffic_tracker: SeleniumTrafficTracker | None = None
    asset_cache: RoxyLocalAssetCache | None = None
    asset_cache_snapshot: dict | None = None
    network_traffic: dict | None = None

    def _traffic_checkpoint() -> None:
        if traffic_tracker is not None:
            try:
                traffic_tracker.checkpoint()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Roxy注册] 刷新浏览器流量统计失败：%s", exc)

    tunnel = getattr(proxy, "tunnel", None)
    if tunnel is not None:
        network_identity = {
            **tunnel.network_identity(),
            "profile_id": None,
            "verified": False,
        }
    try:
        opened = client.open_profile(proxy=proxy, stop_check=_check_manual_stop)
        if tunnel is not None:
            pool = getattr(tunnel, "pool", None)
            if pool is not None:
                pool.bind_profile(tunnel, opened.profile_id)
            from core.registration_network_identity import network_identity_for_tunnel

            network_identity = network_identity_for_tunnel(tunnel, opened.profile_id)
        driver = _build_driver(opened)
        try:
            asset_cache = RoxyLocalAssetCache(opened.debugger_address, label="Roxy").start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Roxy注册] 初始化本地静态资源缓存失败，继续联网加载：%s: %s",
                type(exc).__name__,
                str(exc)[:180],
            )
        from core.registration_network_identity import (
            NetworkIdentityError,
            probe_browser_geo,
            probe_browser_public_ip,
        )

        browser_geo = probe_browser_geo(driver) or {}
        try:
            traffic_tracker = SeleniumTrafficTracker(driver, label="Roxy")
            traffic_tracker.attach_local_asset_cache(asset_cache)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Roxy注册] 初始化浏览器流量统计失败，继续注册：%s: %s",
                type(exc).__name__,
                str(exc)[:180],
            )
        if network_identity is not None:
            from core.registration_network_identity import (
                verify_profile_network_identity,
            )

            network_identity = verify_profile_network_identity(driver, network_identity)
        if network_identity is None:
            network_identity = {"verified": False}
        if browser_geo:
            network_identity["browser_geo"] = browser_geo
        browser_ip = str(
            network_identity.get("browser_egress_ip")
            or browser_geo.get("ip")
            or ""
        ).strip()
        if not browser_ip:
            try:
                browser_ip = probe_browser_public_ip(driver)
            except NetworkIdentityError as exc:
                logger.warning(
                    "[Roxy网络] 无法记录浏览器出口 IP（不影响注册）：%s",
                    str(exc)[:180],
                )
        if browser_ip:
            network_identity.setdefault("browser_egress_ip", browser_ip)
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        try:
            driver.set_script_timeout(12)
        except Exception:  # noqa: BLE001, S110
            pass
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        def _flow_progress(_stage: str) -> None:
            _traffic_checkpoint()

        def _mark_account_created() -> None:
            nonlocal create_acknowledged
            create_acknowledged = True

        def _checkpoint_extras(session_info: dict | None = None) -> dict:
            extras = {
                "device_id": getattr(driver, "device_id", None),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "network_identity": network_identity,
            }
            if session_info is not None:
                extras.update({
                    "user": session_info.get("user"),
                    "account": session_info.get("account"),
                    "expires": session_info.get("expires"),
                })
            return extras

        def _fetch_session_for_flow(current_driver, *, timeout: int, auto_jump_wait: int) -> dict:
            del auto_jump_wait
            return registration_flow._fetch_chatgpt_session(current_driver, timeout=timeout)

        flow_result = run_registration_page_flow(
            driver,
            email,
            name,
            birthday,
            otp_code=otp_code,
            otp_before_code=snapshot_verification_code(
                email,
                stage="registration_email_request",
            ),
            email_supplier=_email_supplier_after_input,
            proxy=proxy,
            registration_driver="roxy",
            checkpoint_extras_factory=_checkpoint_extras,
            registration_ip=(network_identity or {}).get("browser_egress_ip"),
            login_page_timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            login_page_attempts=2,
            warmup_after_login=True,
            session_timeout=120,
            session_auto_jump_wait=15,
            session_recovery=lambda current_driver, _email, error: _recover_chatgpt_session(
                current_driver, _email, error
            ),
            session_fetch=_fetch_session_for_flow,
            otp_completion=lambda current_driver, current_email, **kwargs: _complete_email_otp(
                current_driver,
                current_email,
                **kwargs,
                max_attempts=3,
            ),
            on_page_progress=_flow_progress,
            on_account_created=_mark_account_created,
            early_checkpoint=True,
            log_prefix="[Roxy注册]",
            release_failure=False,
            raise_failure=True,
            failure_extras={"network_identity": network_identity},
        )
        if not flow_result.get("success"):
            if flow_result.get("account_id") and flow_result.get("twofa_status") == "pending":
                flow_result["network_identity"] = network_identity
                return flow_result
            raise RuntimeError(str(flow_result.get("error") or "注册流程失败"))

        email = flow_result["email"]
        openai_password = flow_result.get("openai_password")
        create_acknowledged = bool(flow_result.get("create_acknowledged"))
        account_id = flow_result["account_id"]
        access_token = flow_result["access_token"]
        session_info = flow_result["session_info"]
        email_source = resolve_email_source(email)
        _traffic_checkpoint()

        logger.info("[Roxy注册] token 检查点已保存：account_id=%s twofa=pending", account_id)
        _check_manual_stop()

        totp_secret = None
        twofa_status = "disabled"
        twofa_error = None
        if _twofa_cfg.ENABLE_2FA:
            from core.account_export import setup_2fa_for_registration
            try:
                human_delay("post_auth", minimum=2.0, maximum=4.0)
                totp_secret = setup_2fa_for_registration(driver, email)
                twofa_status = "active"
                db.update_account_2fa(account_id, status="active", totp_secret=totp_secret)
            except Exception as exc:  # noqa: BLE001
                twofa_status = "failed"
                twofa_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.error(
                    "[Roxy注册] 2FA 设置失败，账号已保存，交由队列自动补做 2FA：%s",
                    twofa_error,
                )
            if twofa_status == "failed":
                db.update_account_2fa(account_id, status="failed", error=twofa_error)
                logger.error("[Roxy注册] 2FA 设置失败，账号已保留待重试：%s", twofa_error)
                try:
                    from core.registration_auto_pay153 import (
                        enqueue_registration_auto_pay153,
                    )

                    enqueue_registration_auto_pay153(
                        account_id=account_id,
                        email=email,
                        access_token=access_token,
                        proxy=proxy,
                    )
                except Exception as queue_exc:  # noqa: BLE001 - preserve the checkpointed account.
                    logger.warning(
                        "[PAY.153][Roxy注册] 2FA 失败后的自动任务未入队: %s: %s",
                        type(queue_exc).__name__,
                        str(queue_exc)[:180],
                    )
                return {
                    "success": False,
                    "email": email,
                    "account_id": account_id,
                    "access_token": access_token,
                    "totp_secret": None,
                    "twofa_status": twofa_status,
                    "twofa_error": twofa_error,
                    "error": f"2FA 设置失败，账号已保存：{twofa_error}",
                }

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            from config import register as _register_cfg
            from core.codex_oauth import run_codex_oauth

            free_codex_auto_enabled = bool(
                getattr(_register_cfg, "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
            )
            codex_auto_enabled = bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False))
            codex_credentials = None
            if openai_password and totp_secret:
                from core.codex_login_credentials import CodexLoginCredentials

                codex_credentials = CodexLoginCredentials(
                    email=email,
                    password=openai_password,
                    totp_secret=totp_secret,
                )

            def _run_codex_in_current_browser() -> dict:
                login_mode = (
                    "密码 + authenticator TOTP"
                    if codex_credentials
                    else "邮箱 OTP fallback（注册密码或 TOTP 不完整）"
                )
                logger.info(
                    "[Roxy注册][Codex] 复用当前注册 Roxy 窗口执行 OAuth，不创建新环境，登录方式=%s",
                    login_mode,
                )
                _check_manual_stop()
                return run_codex_oauth(
                    email,
                    oauth_driver="roxy",
                    force=True,
                    credentials=codex_credentials,
                    existing_driver=driver,
                    existing_opened=opened,
                )

            post_auth_automation_enabled = bool(
                getattr(_register_cfg, "AUTO_PLAN_CHECK_AFTER_REGISTER", False)
                or free_codex_auto_enabled
                or bool(getattr(_register_cfg, "AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", False))
                or codex_auto_enabled
            )
            if post_auth_automation_enabled:
                from core.registration_auto_codex import run_registration_auto_codex

                auto_codex = run_registration_auto_codex(
                    account_id=account_id,
                    email=email,
                    access_token=access_token,
                    proxy=proxy,
                    browser_transport=BrowserPageTransport(driver),
                    run_codex=_run_codex_in_current_browser,
                    twofa_status=twofa_status,
                )
                codex_result = auto_codex["codex"]
            else:
                logger.info("[Roxy注册][Codex] 注册后自动 Plan/Codex 流程已关闭")
        except Exception as exc:  # noqa: BLE001
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Roxy注册")
        _traffic_checkpoint()
        if asset_cache is not None:
            try:
                asset_cache_snapshot = asset_cache.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Roxy注册] 保存本地静态资源缓存统计失败，继续保存账号：%s: %s",
                    type(exc).__name__,
                    str(exc)[:180],
                )
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Roxy注册] 保存浏览器网络流量统计失败，继续保存账号：%s: %s",
                    type(exc).__name__,
                    str(exc)[:180],
                )
        if asset_cache_snapshot is not None:
            if not isinstance(network_traffic, dict):
                network_traffic = {}
            network_traffic["local_asset_cache"] = asset_cache_snapshot
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=email_source,
            proxy_used=proxy or None,
            registration_ip=(network_identity or {}).get("browser_egress_ip"),
            batch_dir=batch_dir,
            auto_plan_check=False,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "network_identity": network_identity,
                "registration_password": openai_password,
                "registration_driver": "roxy",
                "twofa_status": twofa_status,
                "twofa_error": twofa_error,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "twofa_status": twofa_status,
            "twofa_error": twofa_error,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "network_identity": network_identity,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        if asset_cache is not None and asset_cache_snapshot is None:
            try:
                asset_cache_snapshot = asset_cache.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if asset_cache_snapshot is not None:
            if not isinstance(network_traffic, dict):
                network_traffic = {}
            network_traffic["local_asset_cache"] = asset_cache_snapshot
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Roxy注册] 失败详情", exc_info=True)
        release_registration_email_on_failure(
            exc,
            email,
            create_acknowledged=create_acknowledged,
            note_prefix="Roxy注册失败",
            log_prefix=_log_prefix(driver),
        )
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "network_identity": network_identity,
            "error": f"{type(exc).__name__}: {str(exc)[:800]}",
        }
    finally:
        if asset_cache is not None:
            try:
                asset_cache.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:  # noqa: BLE001, S110
                pass
        if opened is not None and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
