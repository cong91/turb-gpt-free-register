# -*- coding: utf-8 -*-
"""Codex 授权补跑服务，供账号页和注册任务队列共同使用。"""
import ctypes
import logging
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from core import db
from core.rotating_proxy_runtime import CODEX_RETRY_PROXY_SCOPE, resolve_rotating_proxy

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RETRYING: set[str] = set()
_RETRYING_LOCK = threading.Lock()
_STOP_REQUESTED: set[str] = set()
_RUNNING_THREADS: dict[str, int] = {}
_RESERVED_AT: dict[str, float] = {}


class CodexRetryStopped(Exception):
    """用户手动停止 Codex 补跑。"""


def _thread_alive(thread_id: int | None) -> bool:
    if not thread_id:
        return False
    try:
        tid = int(thread_id)
    except Exception:
        return False
    return any(getattr(t, "ident", None) == tid and t.is_alive() for t in threading.enumerate())


def _clear_state_locked(key: str) -> None:
    _RETRYING.discard(key)
    _RUNNING_THREADS.pop(key, None)
    _RESERVED_AT.pop(key, None)


def log_path(email: str) -> Path:
    safe = email.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"codex-retry-{safe}.log"


def reserve(email: str) -> bool:
    """进程内防止同一账号被重复补跑。"""
    key = (email or "").strip().lower()
    if not key:
        return False
    with _RETRYING_LOCK:
        if key in _RETRYING:
            thread_id = _RUNNING_THREADS.get(key)
            alive = _thread_alive(thread_id)
            age = time.time() - float(_RESERVED_AT.get(key) or 0)
            stop_req = key in _STOP_REQUESTED
            try:
                acc = db.get_account_by_email(email)
                status = str((acc or {}).get("codex_status") or "").lower()
            except Exception:
                status = ""
            # 修复“实际已停止/线程已结束，但进程内占位未释放”导致无法再次补跑。
            # 用户点停止后，部分浏览器/短信等待步骤可能不会立刻退出，UI 已是 stopped 但进程占位仍在。
            # 这种场景允许清理占位后重新补跑；旧线程仍保留 stop_requested，会在检查点退出。
            terminal_status = status in {"stopped", "failed", "success", "deactivated", "skipped", "cancelled"}
            if ((not alive) and (status != "retrying" or age > 15 * 60)) or (terminal_status and (stop_req or age > 30)):
                logger.warning(
                    "[Codex 补跑] 清理脏占位：email=%s status=%s thread_id=%s alive=%s stop_requested=%s age=%.1fs",
                    email, status or "-", thread_id or "-", alive, stop_req, age,
                )
                _clear_state_locked(key)
            else:
                return False
        _STOP_REQUESTED.discard(key)
        _RUNNING_THREADS.pop(key, None)
        _RETRYING.add(key)
        _RESERVED_AT[key] = time.time()
        return True


def release(email: str) -> None:
    key = (email or "").strip().lower()
    with _RETRYING_LOCK:
        _clear_state_locked(key)


def is_retrying(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _RETRYING


def active_retrying_emails() -> set[str]:
    """Return emails with a retry reservation in this WebUI process."""
    with _RETRYING_LOCK:
        return set(_RETRYING)


def reconcile_persisted_retrying_statuses() -> dict[str, int]:
    """Reset persisted retrying states that have no live process reservation."""
    active = active_retrying_emails()
    reset = 0
    for row in db.list_accounts(limit=5000, archived="all"):
        if str(row.get("codex_status") or "").strip().lower() != "retrying":
            continue
        email = str(row.get("email") or "").strip()
        if not email or email.lower() in active:
            continue
        if db.update_account_codex_status(
            email,
            "failed",
            "WebUI khởi động lại; trạng thái retrying cũ không còn worker",
        ):
            reset += 1
    return {"reset": reset, "active": len(active)}


def is_stop_requested(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _STOP_REQUESTED


def check_stop_requested(email: str) -> None:
    if is_stop_requested(email):
        raise CodexRetryStopped("用户手动停止 Codex 补跑")


def _is_retryable_browser_network_result(result: dict) -> bool:
    """Only retry failures caused by a transient browser/network disconnect."""
    if not isinstance(result, dict) or result.get("ok"):
        return False
    text = str(result.get("message") or result.get("error") or "").lower()
    return any(
        marker in text
        for marker in (
            "err_empty_response",
            "err_connection_reset",
            "err_connection_closed",
            "err_timed_out",
            "err_network_changed",
            "err_proxy_connection_failed",
        )
    )


def _async_raise(thread_id: int, exc_type: type[BaseException]) -> bool:
    """向指定 Python 线程注入异常，用于尽快中断阻塞中的补跑流程。"""
    if not thread_id:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread_id),
        ctypes.py_object(exc_type),
    )
    if res == 0:
        return False
    if res != 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
        return False
    return True


def request_stop(email: str) -> dict:
    """请求停止单个 Codex 补跑。运行中会注入停止异常；排队中会在启动前退出。"""
    key = (email or "").strip().lower()
    if not key:
        return {"ok": False, "error": "email 为空", "status": 400}
    with _RETRYING_LOCK:
        retrying = key in _RETRYING
        thread_id = _RUNNING_THREADS.get(key)
        _STOP_REQUESTED.add(key)
    if not retrying:
        db.update_account_codex_status(email, "stopped", "用户手动停止（未发现运行中的补跑）")
        return {"ok": True, "message": "未发现运行中的补跑，已标记为已停止", "state": "stopped", "running": False}

    injected = bool(thread_id and _async_raise(int(thread_id), CodexRetryStopped))
    db.update_account_codex_status(email, "stopped", "用户手动停止 Codex 补跑")
    # 如果没有可注入的存活线程，立即释放进程内占位，避免 UI 显示已停止但再次补跑仍 409。
    with _RETRYING_LOCK:
        if not _thread_alive(thread_id):
            _clear_state_locked(key)
    if injected:
        # 异常注入通常会很快让线程进入 finally/release；若浏览器/CDP/短信等待阻塞导致线程
        # 短时间内仍未退出，延迟清理占位，避免 UI 已显示“已停止”但再次补跑仍 409。
        def _delayed_release() -> None:
            time.sleep(5)
            with _RETRYING_LOCK:
                if key in _RETRYING and key in _STOP_REQUESTED:
                    try:
                        acc = db.get_account_by_email(email)
                        status = str((acc or {}).get("codex_status") or "").lower()
                    except Exception:
                        status = ""
                    if status == "stopped":
                        logger.warning("[Codex 补跑] 停止后延迟释放占位：email=%s thread_id=%s", email, thread_id or "-")
                        _clear_state_locked(key)

        threading.Thread(target=_delayed_release, name=f"codex-stop-release-{key}", daemon=True).start()
    try:
        p = log_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now().strftime('%H:%M:%S')} [WARNING] [Codex 补跑] 用户手动停止，已发送停止信号 injected={injected}\n")
    except Exception:
        logger.exception("写入 Codex 停止日志失败")
    return {"ok": True, "message": "已发送停止信号", "state": "stopped", "running": True, "injected": injected}


def run_worker(
    email: str,
    *,
    batch_label: str | None = None,
    clear_log: bool = True,
    target_log_path: str | Path | None = None,
    oauth_driver: str | None = None,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
    lease_owner_id: str | None = None,
    sub2_callback_context: dict | None = None,
) -> dict:
    """执行一次 Codex 补跑。调用前必须先 reserve，结束时会自动 release。"""
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    result: dict = {"status": "failed", "ok": False, "message": "Codex 补跑未返回结果"}
    key = (email or "").strip().lower()
    try:
        with _RETRYING_LOCK:
            _RUNNING_THREADS[key] = threading.get_ident()
            _RESERVED_AT[key] = time.time()
        check_stop_requested(email)

        from core.codex_oauth import run_codex_oauth, sub2_callback_override

        path = Path(target_log_path) if target_log_path else log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        if clear_log:
            path.write_text("", encoding="utf-8")

        thread_name = threading.current_thread().name
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        try:
            import config as config_pkg
            config_pkg.reload_all()
            from config import codex as codex_cfg
            from config import roxybrowser as roxy_cfg
            logger.info(
                "[Codex 补跑] 已热加载配置：CODEX_OAUTH_DRIVER=%s SMS_PROVIDER=%s ROXY_OPEN_HEADLESS=%s ROXY_KEEP_BROWSER_OPEN=%s",
                getattr(codex_cfg, "CODEX_OAUTH_DRIVER", ""),
                getattr(codex_cfg, "SMS_PROVIDER", ""),
                getattr(roxy_cfg, "ROXY_OPEN_HEADLESS", ""),
                getattr(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", ""),
            )
        except Exception as exc:
            logger.warning("[Codex 补跑] 配置热加载失败，将继续使用当前内存配置：%s: %s", type(exc).__name__, exc)

        if batch_label:
            logger.info("[Codex 补跑] 批量任务：%s", batch_label)
        logger.info("[Codex 补跑] 开始：%s", email)
        check_stop_requested(email)
        account = db.get_account_by_email(email)
        oauth_kwargs = {"force": True}
        if oauth_driver:
            oauth_kwargs["oauth_driver"] = str(oauth_driver).strip()
        login_mode = str((account or {}).get("codex_login_mode") or "").strip().lower()
        has_saved_credentials = bool(
            str((account or {}).get("email") or "").strip()
            and str((account or {}).get("registration_password") or "")
            and str((account or {}).get("totp_secret") or "").strip()
        )
        if account and (login_mode == "credentials" or has_saved_credentials):
            from core.codex_login_credentials import CodexLoginCredentials

            oauth_kwargs["credentials"] = CodexLoginCredentials.from_account(account)
            logger.info("[Codex 补跑] 阶段说明：获取授权地址 → 密码登录 → authenticator 2FA → 手机验证 → 捕获 callback → 提交/保存凭证")
        else:
            logger.info("[Codex 补跑] 阶段说明：获取授权地址 → 登录邮箱 → 邮箱 OTP → 手机验证 → 捕获 callback → 提交/保存凭证")
        from config import codex as codex_cfg
        max_attempts = max(1, int(getattr(codex_cfg, "CODEX_RETRY_NETWORK_ATTEMPTS", 2) or 2))
        retry_delay = max(0.0, float(getattr(codex_cfg, "CODEX_RETRY_NETWORK_DELAY", 3.0) or 3.0))
        from core.nordvpn_wireguard import is_per_profile_proxy_enabled, proxy_for_registration

        rotating_proxy = resolve_rotating_proxy(
            proxy,
            scope=CODEX_RETRY_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        if rotating_proxy is None and is_per_profile_proxy_enabled():
            proxy_context = (
                proxy_for_registration(owner_id=lease_owner_id)
                if lease_owner_id is not None
                else proxy_for_registration()
            )
        else:
            proxy_context = nullcontext(rotating_proxy)
        callback_scope = (
            sub2_callback_override(
                str(sub2_callback_context.get("path") or ""),
                {
                    "request_id": str(sub2_callback_context.get("request_id") or ""),
                    "event_id": str(sub2_callback_context.get("event_id") or ""),
                    "account_id": int(sub2_callback_context.get("account_id") or 0),
                    "email": str(sub2_callback_context.get("email") or email),
                },
            )
            if isinstance(sub2_callback_context, dict)
            else nullcontext()
        )
        with callback_scope:
            with proxy_context as active_proxy:
                if active_proxy:
                    oauth_kwargs["proxy"] = active_proxy
                    if rotating_proxy is not None:
                        logger.info(
                            "[Codex 补跑] 使用 rotating proxy lease，lane=%s",
                            proxy_lane_id if proxy_lane_id is not None else "thread",
                        )
                    else:
                        logger.info("[Codex 补跑] 使用 NordVPN WireGuard SOCKS5 lease，OAuth 不使用 PROXY_POOL")
                result = run_codex_oauth(email, **oauth_kwargs)
                for attempt in range(2, max_attempts + 1):
                    if not _is_retryable_browser_network_result(result):
                        break
                    check_stop_requested(email)
                    logger.warning(
                        "[Codex 补跑] 浏览器临时网络错误，重新开启 OAuth：attempt=%s/%s delay=%.1fs error=%s",
                        attempt,
                        max_attempts,
                        retry_delay,
                        str(result.get("message") or result.get("error") or "")[:180],
                    )
                    if retry_delay:
                        time.sleep(retry_delay)
                    result = run_codex_oauth(email, **oauth_kwargs)
        check_stop_requested(email)
        logger.info(
            "[Codex 补跑] 结果：status=%s ok=%s file=%s callback=%s",
            result.get("status"), result.get("ok"), result.get("file_path"), result.get("callback_url"),
        )
        result_status = result.get("status", "failed")
        if result.get("ok"):
            db.update_account_codex_status(email, "success", None)
            logger.info("[Codex 补跑] %s 成功", email)
        elif result_status == "deactivated":
            db.update_account_codex_status(email, "deactivated", result.get("message"))
            logger.warning("[Codex Chạy bù] %s: %s", email, result.get("message") or "Tài khoản OpenAI không khả dụng")
        else:
            db.update_account_codex_status(email, result_status, result.get("message"))
            logger.warning("[Codex 补跑] %s 失败: %s", email, result.get("message"))
        return result
    except CodexRetryStopped as exc:
        result = {"status": "stopped", "ok": False, "message": str(exc) or "用户手动停止 Codex 补跑"}
        db.update_account_codex_status(email, "stopped", result["message"])
        logger.warning("[Codex 补跑] %s 已停止: %s", email, result["message"])
        return result
    except Exception as exc:
        if is_stop_requested(email):
            result = {"status": "stopped", "ok": False, "message": "用户手动停止 Codex 补跑"}
            db.update_account_codex_status(email, "stopped", result["message"])
            logger.warning("[Codex 补跑] %s 已停止", email)
            return result
        result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}
        db.update_account_codex_status(email, "failed", result["message"])
        logger.exception("[Codex 补跑] %s 异常", email)
        logger.error("[Codex 补跑] 已结束：异常失败")
        return result
    finally:
        try:
            logger.info("[Codex 补跑] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            release(email)
            with _RETRYING_LOCK:
                if key:
                    _STOP_REQUESTED.discard(key)
