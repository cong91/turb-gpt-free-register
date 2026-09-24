"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import resolve_plan_check_route
from core.rotating_proxy_runtime import (
    LIVE_CHECK_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
    release_rotating_proxy,
    resolve_rotating_proxy,
)
from core.time_utils import local_now

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_PROXY_INVENTORY_READY = False


def _prepare_proxy_inventory() -> None:
    global _PROXY_INVENTORY_READY
    from config import proxy as proxy_config

    if not bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)):
        return
    with _LOCK:
        if not _PROXY_INVENTORY_READY:
            prepare_rotating_proxy_lanes(1, scope=LIVE_CHECK_PROXY_SCOPE)
            _PROXY_INVENTORY_READY = True


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = local_now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _reenqueue_failed_plan_check(account_id: int, email: str) -> None:
    """查活刷新 AT 成功后，若套餐查询处于失败态则自动重新入队复查。"""
    try:
        account = db.get_account(account_id) or {}
    except Exception:  # noqa: BLE001
        return
    if str(account.get("plan_check_status") or "") != "failed":
        return
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=int(account_id),
            email=email,
            access_token=access_token,
            trigger="after_live_check",
        )
    except Exception as exc:  # noqa: BLE001 - 查活成功不应被复查入队失败拖垮。
        _append_log(email, f"[查活] 自动复查套餐入队失败: {type(exc).__name__}: {str(exc)[:160]}")
        return
    if queued.get("accepted"):
        _append_log(email, "[查活] AT 已刷新，套餐查询失败态已自动重新入队复查")
    elif queued.get("error"):
        _append_log(email, f"[查活] 自动复查套餐未执行: {queued.get('error')}")


def _run_live_check(
    *,
    account_id: int,
    email: str,
    proxy: str | None,
    trigger: str,
    proxy_lane_id: int | None = None,
) -> dict:
    rotating_proxy: str | None = None
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        selected_proxy = resolve_rotating_proxy(
            proxy,
            scope=LIVE_CHECK_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        if proxy is None:
            rotating_proxy = selected_proxy
        route = resolve_plan_check_route(explicit_proxy=selected_proxy)
        selected_proxy = route.get("proxy")
        # 查活必须沿用账号注册时记录的邮箱来源。不能只调用
        # resolve_email_source(email)：Remail 等临时邮箱的上下文只在领取进程
        # 内存中存在，服务重启后按当前 EMAIL_SOURCE 推断会把来源判错。
        try:
            account = db.get_account(account_id) or {}
        except Exception:  # noqa: BLE001
            account = {}
        email_source = str(account.get("email_source") or "").strip() or None
        if email_source:
            _append_log(email, f"[查活] 使用注册时保存的邮箱来源：{email_source}")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        result = check_account_liveness(
            email,
            proxy=selected_proxy,
            clear_log=False,
            email_source=email_source,
        )
        # 认证链早期 403 通常是出口 IP 被 Cloudflare 拦截，不代表账号死亡。
        error_text = str(result.get("error") or "")
        if (
            rotating_proxy is not None
            and not result.get("ok")
            and result.get("status") == "failed"
            and "403" in error_text
            and selected_proxy
            and str(route.get("network_route") or "") == "proxy"
        ):
            # 早期 403 多为当前出口 IP 被 Cloudflare 拦截；查活预检内部的多次
            # 重试会复用同一 IP，全部撞墙。这里作废当前租约并强制换一个出口
            # IP 重试一次，避免整次查活浪费在被封 IP 上。
            _append_log(email, "[查活] 代理出口收到 403，作废租约并更换出口 IP 重试一次")
            blocked_proxy = rotating_proxy
            release_rotating_proxy(
                scope=LIVE_CHECK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=blocked_proxy,
                retire=True,
            )
            rotating_proxy = None
            try:
                fresh_proxy = resolve_rotating_proxy(
                    None,
                    scope=LIVE_CHECK_PROXY_SCOPE,
                    lane_id=proxy_lane_id,
                    force_refresh=True,
                    exclude_proxy_url=blocked_proxy,
                )
            except Exception as exc:  # noqa: BLE001 - 换出口失败保留原失败结果。
                _append_log(email, f"[查活] 更换出口 IP 失败，保留原失败结果: {type(exc).__name__}: {str(exc)[:160]}")
                fresh_proxy = None
            if fresh_proxy:
                rotating_proxy = fresh_proxy
                selected_proxy = fresh_proxy
                result = check_account_liveness(
                    email,
                    proxy=fresh_proxy,
                    clear_log=False,
                    email_source=email_source,
                )
        # 403 兜底直连：代理池整段出口被 CF 拦截时（换 key 也拿到同一 IP），
        # 与其在被拦链路上反复消耗，直接用本地网络登录一次完成 AT 刷新。
        # BrowserSession 约定：None=从代理池抽取，""=明确直连。
        error_text = str(result.get("error") or "")
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and "403" in error_text
            and selected_proxy
            and str(route.get("network_route") or "") == "proxy"
        ):
            _append_log(email, "[查活] 代理出口持续 403，转直连登录兜底一次")
            result = check_account_liveness(
                email,
                proxy="",
                clear_log=False,
                email_source=email_source,
            )
        # 浏览器兜底：直连出口也被 CF 拦截时，用 Roxy 指纹浏览器完成登录
        # （真实浏览器可解 CF 质解，不依赖出口 IP 干净），从页面内读取新 AT。
        error_text = str(result.get("error") or "")
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and "403" in error_text
            and str(route.get("network_route") or "") == "proxy"
        ):
            _append_log(email, "[查活] 直连出口仍 403，转 Roxy 浏览器登录兜底一次")
            try:
                from core.live_check_browser import browser_refresh_session

                browser_result = browser_refresh_session(email, email_source=email_source)
                if browser_result.get("ok"):
                    result = {
                        "ok": True,
                        "status": "live",
                        "checked_at": local_now().isoformat(timespec="seconds"),
                        "access_token": browser_result["access_token"],
                        "session": browser_result.get("session") or {},
                    }
            except Exception as exc:  # noqa: BLE001 - 浏览器兜底失败保留原失败结果。
                _append_log(
                    email,
                    f"[查活] 浏览器兜底失败，保留原失败结果: {type(exc).__name__}: {str(exc)[:200]}",
                )
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
            _reenqueue_failed_plan_check(account_id, email)
        elif result.get("status") == "deactivated":
            _append_log(email, f"[查活] 完成：{result.get('error') or 'OpenAI đã khóa tài khoản'}")
        else:
            _append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": local_now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:  # noqa: BLE001, S110
            pass
        return result
    finally:
        if rotating_proxy is not None:
            release_rotating_proxy(
                scope=LIVE_CHECK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=rotating_proxy,
            )
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_live_check(
    *,
    account_id: int,
    email: str,
    trigger: str = "manual",
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _prepare_proxy_inventory()
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
            proxy_lane_id=proxy_lane_id,
        )
    except Exception as exc:  # noqa: BLE001
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": local_now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
