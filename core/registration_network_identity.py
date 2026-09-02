"""Public network identity probes for registration tunnels and browsers."""
from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from datetime import datetime, timezone


class NetworkIdentityError(RuntimeError):
    """The registration egress identity could not be measured or verified."""


_IP_ENDPOINTS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.co/json",
    "https://api64.ipify.org?format=json",
)


def normalize_public_ip(value: object) -> str:
    """Return one canonical public IP string or raise for invalid/private values."""
    candidate = str(value or "").strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise NetworkIdentityError(f"公共 IP 响应无效: {candidate[:80]!r}") from exc
    if not address.is_global:
        raise NetworkIdentityError(f"出口 IP 不是公网地址: {address.compressed}")
    return address.compressed


def _extract_ip(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("ip", "address", "query"):
            if payload.get(key):
                return normalize_public_ip(payload[key])
    return normalize_public_ip(payload)


def _normalize_geo_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    timezone = payload.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or timezone.get("name")
    connection = payload.get("connection")
    if not isinstance(connection, dict):
        connection = {}
    return {
        "ip": payload.get("ip") or payload.get("query"),
        "country": str(
            payload.get("country")
            or payload.get("country_code")
            or payload.get("countryCode")
            or ""
        ).upper(),
        "region": payload.get("region") or payload.get("regionName"),
        "city": payload.get("city"),
        "timezone": timezone or "",
        "org": payload.get("org") or payload.get("isp") or connection.get("org"),
    }


def _geo_endpoints() -> list[str]:
    try:
        from config import browser as browser_config

        return list(getattr(browser_config, "IP_GEO_ENDPOINTS", []) or [])
    except Exception:  # noqa: BLE001
        return []


def probe_browser_geo(driver, *, timeout: float = 6.0) -> dict:
    """Probe country metadata inside a Selenium-compatible browser profile."""
    urls = _geo_endpoints()
    if not urls:
        return {}
    script = """
const done = arguments[arguments.length - 1];
const urls = arguments[0];
const timeoutMs = arguments[1];
(async () => {
  for (const url of urls) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) continue;
      const payload = await response.json();
      if (payload && (payload.country || payload.country_code || payload.countryCode)) {
        done(payload);
        return;
      }
    } catch (_) {
      // Try the next bounded provider.
    } finally {
      clearTimeout(timer);
    }
  }
  done({});
})();
"""
    try:
        driver.set_script_timeout(max(1, int(timeout * len(urls) + 2)))
        result = driver.execute_async_script(script, urls, int(timeout * 1000))
    except Exception:  # noqa: BLE001
        return {}
    return _normalize_geo_payload(result)


def probe_playwright_geo(page, *, timeout: float = 6.0) -> dict:
    """Probe country metadata inside a Playwright page."""
    urls = _geo_endpoints()
    if not urls:
        return {}
    script = """
async ({urls, timeoutMs}) => {
  for (const url of urls) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) continue;
      const payload = await response.json();
      if (payload && (payload.country || payload.country_code || payload.countryCode)) return payload;
    } catch (_) {
      // Try the next bounded provider.
    } finally {
      clearTimeout(timer);
    }
  }
  return {};
}
"""
    try:
        return _normalize_geo_payload(page.evaluate(script, {"urls": urls, "timeoutMs": int(timeout * 1000)}))
    except Exception:  # noqa: BLE001
        return {}


def probe_socks_public_ip(
    proxy_url: str,
    *,
    timeout: float = 10.0,
    endpoints: Iterable[str] = _IP_ENDPOINTS,
    retries: int = 2,
) -> str:
    """Measure public egress through a SOCKS proxy using bounded fallbacks.
    
    Args:
        proxy_url: SOCKS5 proxy URL (e.g., socks5://127.0.0.1:25000)
        timeout: Per-request timeout in seconds
        endpoints: IP detection endpoints to try
        retries: Number of retry attempts per endpoint
        
    Returns:
        Public IP address as string
        
    Raises:
        NetworkIdentityError: All endpoints failed after retries
    """
    import time

    from curl_cffi import requests

    errors: list[str] = []
    for endpoint in endpoints:
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    endpoint,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=timeout,
                    impersonate="chrome",
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except (ValueError, json.JSONDecodeError):
                    payload = response.text
                return _extract_ip(payload)
            except Exception as exc:  # noqa: BLE001
                error_msg = f"{type(exc).__name__}: {str(exc)[:100]}"
                if attempt < retries:
                    # Backoff before retry
                    time.sleep(0.5 * (attempt + 1))
                else:
                    errors.append(f"{endpoint} (尝试{retries + 1}次): {error_msg}")
    raise NetworkIdentityError(
        "无法通过 WireGuard SOCKS5 测量公网 IP: " + " | ".join(errors)
    )


def probe_browser_public_ip(
    driver,
    *,
    timeout: float = 12.0,
    endpoints: Iterable[str] = _IP_ENDPOINTS,
) -> str:
    """Measure egress from inside the attached browser profile."""
    urls = list(endpoints)
    script = """
const done = arguments[arguments.length - 1];
const urls = arguments[0];
const timeoutMs = arguments[1];
(async () => {
  const errors = [];
  for (const url of urls) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      let payload;
      try { payload = JSON.parse(text); } catch (_) { payload = text; }
      const ip = typeof payload === 'string'
        ? payload.trim()
        : String(payload.ip || payload.address || payload.query || '').trim();
      if (ip) { done({ip}); return; }
      errors.push('empty response');
    } catch (error) {
      errors.push(String(error));
    } finally {
      clearTimeout(timer);
    }
  }
  done({error: errors.join(' | ')});
})();
"""
    try:
        driver.set_script_timeout(max(1, int(timeout * len(urls) + 2)))
        result = driver.execute_async_script(script, urls, int(timeout * 1000))
    except Exception as exc:
        raise NetworkIdentityError(
            f"无法从 Roxy 浏览器测量公网 IP: {type(exc).__name__}: {str(exc)[:160]}"
        ) from exc
    if not isinstance(result, dict) or not result.get("ip"):
        detail = result.get("error") if isinstance(result, dict) else result
        raise NetworkIdentityError(f"Roxy 浏览器公网 IP 探测失败: {str(detail)[:200]}")
    return normalize_public_ip(result["ip"])


def verify_browser_tunnel_identity(driver, tunnel_ip: str) -> str:
    """Require browser and tunnel egress to be the same public address."""
    expected = normalize_public_ip(tunnel_ip)
    observed = probe_browser_public_ip(driver)
    if observed != expected:
        raise NetworkIdentityError(
            f"Roxy 代理出口不匹配: tunnel_ip={expected}, browser_ip={observed}"
        )
    return observed


def network_identity_for_tunnel(tunnel, profile_id: str) -> dict:
    """Build persisted metadata for one profile/tunnel correlation."""
    return {
        **tunnel.network_identity(),
        "profile_id": profile_id,
        "verified": False,
    }


def verify_profile_network_identity(driver, identity: dict) -> dict:
    """Verify a browser against an existing tunnel identity and return evidence."""
    browser_ip = verify_browser_tunnel_identity(
        driver, str(identity.get("tunnel_egress_ip") or "")
    )
    result = dict(identity)
    result.update({
        "browser_egress_ip": browser_ip,
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return result
