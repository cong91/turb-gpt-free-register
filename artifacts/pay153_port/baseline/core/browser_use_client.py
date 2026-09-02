# -*- coding: utf-8 -*-
"""Browser Use Cloud 客户端：构建 CDP 连接并管理 Playwright 生命周期。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

from config import browser_use as _cfg

logger = logging.getLogger(__name__)


@dataclass
class BrowserUseSession:
    connect_url: str
    api_key_present: bool
    proxy_country_code: str = ""
    profile_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BrowserUseClient:
    """最小客户端：默认用官方 connect_over_cdp websocket。"""

    def __init__(self, api_key: str | None = None, http_client: Any | None = None):
        self.api_key = (api_key if api_key is not None else getattr(_cfg, "BROWSER_USE_API_KEY", "") or "").strip()
        self._http = http_client or requests

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "BROWSER_USE_API_KEY 为空。请到 Browser Use Cloud 创建 API Key，"
                "并在 config/browser_use.py 或 WebUI 配置页填写。"
            )
        return self.api_key

    def build_connect_url(self, *, fresh_profile: bool = False) -> BrowserUseSession:
        api_key = self.require_api_key()
        base = str(getattr(_cfg, "BROWSER_USE_CDP_BASE", "wss://connect.browser-use.com") or "wss://connect.browser-use.com").rstrip("?&")
        query: dict[str, str] = {"apiKey": api_key}

        proxy_country = str(getattr(_cfg, "BROWSER_USE_PROXY_COUNTRY_CODE", "") or "").strip().lower()
        use_proxy = bool(getattr(_cfg, "BROWSER_USE_USE_PROXY", True))
        if use_proxy and proxy_country:
            query["proxyCountryCode"] = proxy_country

        profile_id = "" if fresh_profile else str(getattr(_cfg, "BROWSER_USE_PROFILE_ID", "") or "").strip()
        if profile_id:
            query["profileId"] = profile_id

        session_timeout = int(getattr(_cfg, "BROWSER_USE_SESSION_TIMEOUT", 240) or 240)
        if session_timeout > 0:
            # Browser Use Cloud connect URL 的 timeout 是 keepAlive/会话存活时间，单位为分钟。
            # 服务端会校验上限；超过会在 CDP 连接阶段返回 HTTP 422。这里统一夹到 1~240 分钟。
            query["timeout"] = str(max(1, min(240, session_timeout)))

        extra = dict(getattr(_cfg, "BROWSER_USE_EXTRA_QUERY", {}) or {})
        for key, value in extra.items():
            if fresh_profile and str(key).strip().lower() in ("profileid", "profile_id"):
                continue
            if value is None:
                continue
            text = str(value).strip()
            if text:
                query[str(key)] = text

        connect_url = f"{base}?{urlencode(query)}"
        # 日志里不要打印完整 apiKey
        safe_query = dict(query)
        if "apiKey" in safe_query:
            safe_query["apiKey"] = safe_query["apiKey"][:6] + "***"
        logger.info(
            "[BrowserUse] CDP connect params: base=%s proxyCountry=%s profileId=%s use_proxy=%s timeout=%s",
            base,
            proxy_country or "-",
            profile_id or "-",
            use_proxy,
            query.get("timeout") or "-",
        )
        logger.debug("[BrowserUse] CDP safe query=%s", safe_query)
        return BrowserUseSession(
            connect_url=connect_url,
            api_key_present=True,
            proxy_country_code=proxy_country,
            profile_id=profile_id,
            raw={"query": safe_query, "base": base},
        )

    def _create_session_url(self) -> str:
        base = str(
            getattr(_cfg, "BROWSER_USE_API_BASE", "https://api.browser-use.com/api/v2")
            or "https://api.browser-use.com/api/v2"
        ).rstrip("/")
        if base.endswith("/api/v2"):
            base = f"{base[:-len('/api/v2')]}/api/v3"
        elif not base.endswith("/api/v3"):
            base = f"{base}/api/v3"
        return f"{base}/browsers"

    def _open_custom_proxy_session(self, proxy: str, *, fresh_profile: bool = False) -> BrowserUseSession:
        from core.rotating_proxy_runtime import custom_proxy_details

        session_timeout = int(getattr(_cfg, "BROWSER_USE_SESSION_TIMEOUT", 240) or 240)
        payload: dict[str, Any] = {
            "timeout": max(1, min(240, session_timeout)),
            "customProxy": custom_proxy_details(proxy),
        }
        profile_id = "" if fresh_profile else str(getattr(_cfg, "BROWSER_USE_PROFILE_ID", "") or "").strip()
        if profile_id:
            payload["profileId"] = profile_id
        start_url = str(getattr(_cfg, "BROWSER_USE_START_URL", "") or "").strip()
        if start_url:
            payload["startUrl"] = start_url

        response = self._http.post(
            self._create_session_url(),
            headers={
                "X-Browser-Use-API-Key": self.require_api_key(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=30,
        )
        try:
            data = response.json()
        except Exception:
            data = {"text": str(getattr(response, "text", ""))[:1000]}
        if response.status_code >= 400:
            raise RuntimeError(f"Browser Use create browser HTTP {response.status_code}: {data}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Browser Use create browser 响应不是对象: {data!r}")
        connect_url = str(data.get("cdpUrl") or data.get("cdp_url") or data.get("connect_url") or "").strip()
        if not connect_url:
            raise RuntimeError(f"Browser Use create browser 响应缺少 cdpUrl: {data}")
        logger.info(
            "[BrowserUse] 已创建 custom proxy browser session: host=%s port=%s profileId=%s",
            payload["customProxy"].get("host"),
            payload["customProxy"].get("port"),
            profile_id or "-",
        )
        safe_custom_proxy = {
            key: value
            for key, value in payload["customProxy"].items()
            if key in {"host", "port"}
        }
        return BrowserUseSession(
            connect_url=connect_url,
            api_key_present=True,
            proxy_country_code="custom",
            profile_id=profile_id,
            raw={"custom_proxy": safe_custom_proxy},
        )

    def open_session(self, proxy: str | None = None, *, fresh_profile: bool = False) -> BrowserUseSession:
        mode = str(getattr(_cfg, "BROWSER_USE_CONNECT_MODE", "cdp_url") or "cdp_url").strip().lower()
        if mode not in ("cdp_url", "cdp", "websocket", "ws", "sdk"):
            raise RuntimeError(f"不支持的 BROWSER_USE_CONNECT_MODE={mode!r}，当前支持 cdp_url")
        if proxy is not None:
            return self._open_custom_proxy_session(proxy, fresh_profile=fresh_profile)
        # 目前 Browser Use 官方最稳的公开接入就是 CDP websocket。
        # sdk/rest create-session 接口若以后稳定，可在此扩展。
        return self.build_connect_url(fresh_profile=fresh_profile)
