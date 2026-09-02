# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db
from core.rotating_proxy_runtime import (
    EXTRACT_LINK_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
    release_rotating_proxy,
    resolve_rotating_proxy,
)

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    return t


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_PROXY_INVENTORY_LOCK = threading.Lock()
_PROXY_INVENTORY_READY = False


def _prepare_proxy_inventory() -> None:
    global _PROXY_INVENTORY_READY
    from config import proxy as proxy_config

    if not bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)):
        return
    with _PROXY_INVENTORY_LOCK:
        if not _PROXY_INVENTORY_READY:
            prepare_rotating_proxy_lanes(1, scope=EXTRACT_LINK_PROXY_SCOPE)
            _PROXY_INVENTORY_READY = True


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _proxy_kwargs(proxy: str | None) -> dict[str, dict[str, str]]:
    if proxy is None:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}


def _urlopen(url: str, *, timeout: int, proxy: str | None = None, **kwargs):
    if proxy is None:
        return urlopen(url, timeout=timeout, **kwargs)
    from urllib.request import ProxyHandler, build_opener

    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(url, timeout=timeout, **kwargs)


def _session(proxy: str | None = None):
    if curl_requests is None:
        return None
    return curl_requests.Session()


def query_cdk(*, cdk: str | None = None, proxy: str | None = None, proxy_lane_id: int | None = None) -> dict:
    base = _api_base()
    code = _cdk(cdk)
    active_proxy = resolve_rotating_proxy(
        proxy,
        scope=EXTRACT_LINK_PROXY_SCOPE,
        lane_id=proxy_lane_id,
    )
    rotating_proxy = active_proxy if proxy is None else None
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session(active_proxy)
    try:
        if s is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with _urlopen(req, timeout=timeout, proxy=active_proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.get(
            f"{base}/api/cdk?{urlencode({'code': code})}",
            timeout=timeout,
            **_proxy_kwargs(active_proxy),
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass
        if rotating_proxy is not None:
            release_rotating_proxy(
                scope=EXTRACT_LINK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=rotating_proxy,
            )


def _create_extract_job(
    *,
    token: str,
    link_type: str,
    cdk: str,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    active_proxy = resolve_rotating_proxy(
        proxy,
        scope=EXTRACT_LINK_PROXY_SCOPE,
        lane_id=proxy_lane_id,
    )
    rotating_proxy = active_proxy if proxy is None else None
    s = _session(active_proxy)
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extract",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with _urlopen(req, timeout=timeout, proxy=active_proxy) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"提链服务未返回 job_id: {data}")
            return data
        resp = s.post(
            f"{base}/api/extract",
            json=payload,
            timeout=timeout,
            **_proxy_kwargs(active_proxy),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass
        if rotating_proxy is not None:
            release_rotating_proxy(
                scope=EXTRACT_LINK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=rotating_proxy,
            )


def _iter_sse_events(
    *,
    job_id: str,
    cdk: str,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    active_proxy = resolve_rotating_proxy(
        proxy,
        scope=EXTRACT_LINK_PROXY_SCOPE,
        lane_id=proxy_lane_id,
    )
    rotating_proxy = active_proxy if proxy is None else None
    s = _session(active_proxy)
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with _urlopen(req, timeout=timeout, proxy=active_proxy) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(
            url,
            timeout=timeout,
            stream=True,
            **_proxy_kwargs(active_proxy),
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass
        if rotating_proxy is not None:
            release_rotating_proxy(
                scope=EXTRACT_LINK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=rotating_proxy,
            )


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _run_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    link_type: str,
    cdk: str,
    trigger: str,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
) -> dict:
    logs: list[str] = []
    last_event = None
    rotating_proxy: str | None = None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        active_proxy = resolve_rotating_proxy(
            proxy,
            scope=EXTRACT_LINK_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        if proxy is None:
            rotating_proxy = active_proxy
        job = _create_extract_job(
            token=access_token,
            link_type=link_type,
            cdk=cdk,
            proxy=active_proxy,
        )
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk, proxy=active_proxy):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        if rotating_proxy is not None:
            release_rotating_proxy(
                scope=EXTRACT_LINK_PROXY_SCOPE,
                lane_id=proxy_lane_id,
                proxy_url=rotating_proxy,
            )
        _QUEUE_SLOTS.release()


def enqueue_account_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    link_type: str | None = None,
    cdk: str | None = None,
    proxy: str | None = None,
    proxy_lane_id: int | None = None,
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        lt = _link_type(link_type)
        code = _cdk(cdk)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        _prepare_proxy_inventory()
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            link_type=lt,
            cdk=code,
            trigger=trigger,
            proxy=proxy,
            proxy_lane_id=proxy_lane_id,
        )
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
