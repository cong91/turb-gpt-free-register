"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底  # noqa: BLE001
    curl_requests = None

from config import extract_link as cfg
from core import db
from core.rotating_proxy_runtime import (
    EXTRACT_LINK_PAYMENT_PROXY_SCOPE,
    EXTRACT_LINK_PROMOTION_PROXY_SCOPE,
    EXTRACT_LINK_PROXY_SCOPE,
    prepare_rotating_proxy_lanes,
    release_rotating_proxy,
    resolve_rotating_proxy,
)
from core.time_utils import local_now

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:  # noqa: BLE001, S110
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


REMOTE_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
LOCAL_DEFAULT_LINK_TYPE = "ph_short"
REMOTE_DEFAULT_LINK_TYPE = "pix"


def _proxy_roles(link_type: str) -> tuple[str, ...]:
    """Return the PAY.153 proxy roles required by a provider route."""
    provider = str(link_type or "").strip().lower()
    try:
        from core.pay153_provider_policy import provider_policy

        stages = provider_policy(provider).stages
    except (ImportError, KeyError, ValueError):
        stages = ()
    role_map = {
        "entry": EXTRACT_LINK_PROXY_SCOPE,
        "checkout": EXTRACT_LINK_PROXY_SCOPE,
        "chain": EXTRACT_LINK_PROXY_SCOPE,
        "provider": EXTRACT_LINK_PAYMENT_PROXY_SCOPE,
        "payment": EXTRACT_LINK_PAYMENT_PROXY_SCOPE,
        "promotion": EXTRACT_LINK_PROMOTION_PROXY_SCOPE,
    }
    scopes: list[str] = []
    for stage in stages:
        scope = role_map.get(str(stage.role).lower())
        if scope and scope not in scopes:
            scopes.append(scope)
    if scopes:
        return tuple(scopes)
    return (EXTRACT_LINK_PROXY_SCOPE,)


def _mode() -> str:
    """Select PAY.153 by default; legacy remote mode must be explicit."""
    value = str(_runtime_setting("EXTRACT_LINK_MODE", "auto") or "auto").strip().lower()
    if value not in {"auto", "remote", "local"}:
        raise ValueError("EXTRACT_LINK_MODE 无效，仅支持 auto / remote / local")
    if value == "auto":
        return "local"
    return value


def _link_type(value: str | None = None, *, mode: str | None = None) -> str:
    """Validate the selected payment method for the active backend."""
    active_mode = mode or _mode()
    selected = str(value or (LOCAL_DEFAULT_LINK_TYPE if active_mode == "local" else REMOTE_DEFAULT_LINK_TYPE)).strip().lower()
    if active_mode == "remote":
        if selected not in REMOTE_LINK_TYPES:
            raise ValueError("Remote extractor supports pix / upi / kakao_pay / ideal")
        return selected
    from core.pay153_provider_workflow import validate_provider

    return validate_provider(selected)


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


def _run_local_checkout(
    *,
    token: str,
    link_type: str,
    proxy: str | None,
    payment_proxy: str | None = None,
    promotion_proxy: str | None = None,
    log,
) -> dict:
    """Run PAY.153's direct access-token checkout flow and normalize its result."""
    from uuid import uuid4

    if link_type != "ph_short":
        from core.pay153_provider_workflow import run_provider_checkout

        provider_result = run_provider_checkout(
            token,
            link_type,
            entry_proxy=proxy,
            payment_proxy=payment_proxy or proxy,
            promotion_proxy=promotion_proxy or proxy,
            log=log,
        )
        checkout_url = str(
            provider_result.get("provider_redirect_url")
            or provider_result.get("short_link")
            or provider_result.get("checkout_url")
            or ""
        )
        qr_data = provider_result.get("qr_data")
        qr_png = provider_result.get("qr_image_png")
        qr_svg = provider_result.get("qr_image_svg")
        if not checkout_url and not (qr_data or qr_png or qr_svg):
            raise RuntimeError("PAY.153 provider workflow returned no payment material")
        copy_value = checkout_url or str(qr_data or "")
        payload = {
            "long_url": checkout_url,
            "copy_paste": copy_value,
            "payment_method": link_type,
            "payment_link_type": link_type,
            "expires_at": provider_result.get("expires_at"),
            "checkout_session_id": provider_result.get("checkout_session_id"),
            "processor_entity": provider_result.get("processor_entity"),
            "billing_country": provider_result.get("checkout_country") or provider_result.get("country"),
            "currency": provider_result.get("checkout_currency") or provider_result.get("currency"),
            "amount_verification": provider_result.get("amount_verification"),
            "amount_minor": provider_result.get("checkout_amount"),
            "amount_currency": provider_result.get("checkout_currency") or provider_result.get("currency"),
            "qr_data": qr_data,
            "image_url_png": qr_png,
            "image_url_svg": qr_svg,
            "provider_result": provider_result,
            "extractor": "pay153_provider_workflow",
        }
        return {
            "ok": True,
            "status": "success",
            "job_id": f"local-{uuid4()}",
            "link_type": link_type,
            "requested_link_type": link_type,
            "result": payload,
        }

    from core.pay153_checkout_extractor import (
        CheckoutExtractor,
        ExtractorConfig,
        parse_credentials,
    )

    country = str(_runtime_setting("EXTRACT_LINK_LOCAL_BILLING_COUNTRY", "PH") or "PH").strip().upper()
    currency = str(_runtime_setting("EXTRACT_LINK_LOCAL_CURRENCY", "PHP") or "PHP").strip().upper()
    config = ExtractorConfig(
        billing_country=country,
        currency=currency,
        checkout_proxy=str(proxy or ""),
        update_proxy=str(promotion_proxy or proxy or ""),
        plan_name=str(_runtime_setting("EXTRACT_LINK_LOCAL_PLAN_NAME", "chatgptplusplan") or "chatgptplusplan"),
        promo_campaign_id=str(_runtime_setting("EXTRACT_LINK_LOCAL_PROMO_CAMPAIGN_ID", "plus-1-month-free") or "plus-1-month-free"),
        apply_promo=str(_runtime_setting("EXTRACT_LINK_LOCAL_APPLY_PROMO", "true")).strip().lower() in {"1", "true", "yes", "on"},
        verify_proxy_country=False,
        checkout_attempts=_int_setting("EXTRACT_LINK_LOCAL_CHECKOUT_ATTEMPTS", 3, 1, 10),
        update_attempts=_int_setting("EXTRACT_LINK_LOCAL_UPDATE_ATTEMPTS", 3, 1, 10),
        full_attempts=1,
        timeout=float(_int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)),
    )
    messages: list[str] = []
    extractor = CheckoutExtractor(
        parse_credentials(token),
        config=config,
        logger=lambda message: (messages.append(str(message)[:300]), log(str(message)[:300]))[-1],
    )
    result = extractor.extract()
    payload = {
        "long_url": result.long_url,
        "copy_paste": result.long_url,
        "payment_method": "ph_short",
        "payment_link_type": "ph_short",
        "expires_at": None,
        "checkout_session_id": result.cs_id,
        "processor_entity": result.processor_entity,
        "billing_country": result.billing_country,
        "currency": result.currency,
        "amount_verification": result.amount_verification,
        "amount_minor": result.amount_minor,
        "amount_currency": result.amount_currency,
        "extractor": "pay153_direct",
    }
    return {
        "ok": True,
        "status": "success",
        "job_id": f"local-{uuid4()}",
        "link_type": "ph_short",
        "requested_link_type": link_type,
        "result": payload,
        "logs": messages,
    }


def backend_status() -> dict:
    """Return non-secret status for the active extraction backend."""
    mode = _mode()
    return {
        "mode": mode,
        "local": mode == "local",
        "remote_configured": bool(
            str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip()
            and str(_runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
        ),
    }


def payment_method_options() -> dict:
    """Expose selectable payment methods without exposing backend credentials."""
    mode = _mode()
    if mode == "local":
        from core.pay153_provider_workflow import payment_method_catalog

        methods = payment_method_catalog()
        default = LOCAL_DEFAULT_LINK_TYPE
    else:
        methods = [
            {"id": "pix", "label": "PIX"},
            {"id": "upi", "label": "UPI"},
            {"id": "kakao_pay", "label": "Kakao Pay"},
            {"id": "ideal", "label": "iDEAL"},
        ]
        default = REMOTE_DEFAULT_LINK_TYPE
    return {"mode": mode, "payment_methods": methods, "default_payment_method": default}


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_PROXY_INVENTORY_LOCK = threading.Lock()
_PROXY_INVENTORY_READY: set[str] = set()


def _prepare_proxy_inventory(link_type: str | None = None) -> None:
    from config import proxy as proxy_config

    if not bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)):
        return
    with _PROXY_INVENTORY_LOCK:
        scopes = _proxy_roles(link_type) if link_type else (
            EXTRACT_LINK_PROXY_SCOPE,
            EXTRACT_LINK_PAYMENT_PROXY_SCOPE,
            EXTRACT_LINK_PROMOTION_PROXY_SCOPE,
        )
        for scope in scopes:
            if scope not in _PROXY_INVENTORY_READY:
                prepare_rotating_proxy_lanes(1, scope=scope)
                _PROXY_INVENTORY_READY.add(scope)


def proxy_options() -> dict:
    """Return selectable configured proxies without exposing credentials."""
    from config import proxy as proxy_config
    from core.rotating_proxy_manager import mask_rotating_proxy

    items = []
    for index, raw in enumerate(getattr(proxy_config, "PROXY_POOL", []) or []):
        try:
            normalized = proxy_config.normalize_proxy_url(raw)
        except (TypeError, ValueError):
            continue
        if not normalized:
            continue
        items.append({"id": str(index), "label": mask_rotating_proxy(normalized)})
    return {
        "rotating_enabled": bool(getattr(proxy_config, "ROTATING_PROXY_ENABLED", False)),
        "pool": items,
    }


def _configured_proxy(index: object) -> str:
    from config import proxy as proxy_config

    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise ValueError("proxy pool index must be an integer") from exc
    raw_pool = getattr(proxy_config, "PROXY_POOL", []) or []
    if position < 0 or position >= len(raw_pool):
        raise ValueError("proxy pool index is out of range")
    try:
        value = proxy_config.normalize_proxy_url(raw_pool[position])
    except (TypeError, ValueError) as exc:
        raise ValueError("configured proxy is invalid") from exc
    if not value:
        raise ValueError("configured proxy is empty")
    return value


def _request_proxy(value: object, *, pool_index: object = None) -> str | None:
    """Normalize a manual proxy or resolve one configured pool entry."""
    if pool_index not in (None, ""):
        return _configured_proxy(pool_index)
    text = str(value or "").strip()
    if not text:
        return None
    from config.proxy import normalize_proxy_url

    try:
        return normalize_proxy_url(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid proxy: {exc}") from exc


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
        except Exception:  # noqa: BLE001
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001, S110
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
        except Exception:  # noqa: BLE001
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001, S110
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
                            except Exception:  # noqa: BLE001
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
                    except Exception:  # noqa: BLE001
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
                    except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001, S110
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
    reason = f"{type(exc).__name__}: {exc!s}".strip()
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
    payment_proxy: str | None = None,
    promotion_proxy: str | None = None,
    proxy_lane_id: int | None = None,
) -> dict:
    logs: list[str] = []
    last_event = None
    rotating_proxies: list[tuple[str, str | None]] = []
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        if _mode() == "local":
            roles = _proxy_roles(link_type)
            supplied = {
                EXTRACT_LINK_PROXY_SCOPE: proxy,
                EXTRACT_LINK_PAYMENT_PROXY_SCOPE: payment_proxy,
                EXTRACT_LINK_PROMOTION_PROXY_SCOPE: promotion_proxy,
            }
            fallback_proxy = next((value for value in supplied.values() if value), None)
            active_by_scope: dict[str, str | None] = {}
            for scope in roles:
                value = supplied.get(scope)
                if value is None and fallback_proxy is None:
                    value = resolve_rotating_proxy(None, scope=scope, lane_id=proxy_lane_id)
                    rotating_proxies.append((scope, value))
                elif value is None:
                    value = fallback_proxy
                active_by_scope[scope] = value
            active_proxy = next((active_by_scope.get(scope) for scope in roles if active_by_scope.get(scope)), None)
            active_payment_proxy = active_by_scope.get(EXTRACT_LINK_PAYMENT_PROXY_SCOPE) or active_proxy
            active_promotion_proxy = active_by_scope.get(EXTRACT_LINK_PROMOTION_PROXY_SCOPE) or active_proxy
            final = _run_local_checkout(
                token=access_token,
                link_type=link_type,
                proxy=active_proxy,
                payment_proxy=active_payment_proxy,
                promotion_proxy=active_promotion_proxy,
                log=lambda message: db.update_account_extract(account_id, {
                    "ok": False,
                    "status": "running",
                    "link_type": link_type,
                    "message": str(message)[:300],
                }),
            )
            db.update_account_extract(account_id, final)
            logger.info("[提链] 本地 PAY.153 成功: %s type=%s", email, link_type)
            return final

        active_proxy = resolve_rotating_proxy(
            proxy,
            scope=EXTRACT_LINK_PROXY_SCOPE,
            lane_id=proxy_lane_id,
        )
        if proxy is None:
            rotating_proxies.append((EXTRACT_LINK_PROXY_SCOPE, active_proxy))

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
            "checked_at": local_now().isoformat(timespec="seconds"),
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
        for scope, rotating_proxy in rotating_proxies:
            if rotating_proxy is None:
                continue
            release_rotating_proxy(
                scope=scope,
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
    payment_proxy: str | None = None,
    promotion_proxy: str | None = None,
    proxy_pool_index: object = None,
    payment_proxy_pool_index: object = None,
    promotion_proxy_pool_index: object = None,
    proxy_lane_id: int | None = None,
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        mode = _mode()
        lt = _link_type(link_type, mode=mode)
        code = _cdk(cdk) if mode == "remote" else ""
        entry_proxy = _request_proxy(proxy, pool_index=proxy_pool_index)
        selected_payment_proxy = _request_proxy(payment_proxy, pool_index=payment_proxy_pool_index)
        selected_promotion_proxy = _request_proxy(promotion_proxy, pool_index=promotion_proxy_pool_index)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        _prepare_proxy_inventory(lt)
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            link_type=lt,
            cdk=code,
            trigger=trigger,
            proxy=entry_proxy,
            payment_proxy=selected_payment_proxy,
            promotion_proxy=selected_promotion_proxy,
            proxy_lane_id=proxy_lane_id,
        )
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt, "mode": mode}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
