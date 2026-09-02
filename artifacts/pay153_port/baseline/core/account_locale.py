"""Derive account-level locale metadata from the registration exit context."""
from __future__ import annotations

from typing import Any


def _country_to_locale(country: object) -> str:
    value = str(country or "").strip().upper()
    if not value:
        return ""
    try:
        from config import browser as browser_config

        return str(getattr(browser_config, "COUNTRY_LOCALE_PROFILE_MAP", {}).get(value) or value.lower())
    except Exception:
        return value.lower()


def _geo_from_extra(extra: dict[str, Any]) -> dict[str, Any]:
    cloakbrowser = extra.get("cloakbrowser")
    cloak_open = cloakbrowser.get("open_result") if isinstance(cloakbrowser, dict) else None
    cloak_locale = cloak_open.get("locale") if isinstance(cloak_open, dict) else None
    candidates: list[object] = [
        extra.get("registration_geo"),
        extra.get("network_identity", {}).get("browser_geo") if isinstance(extra.get("network_identity"), dict) else None,
        extra.get("network_identity", {}).get("geo") if isinstance(extra.get("network_identity"), dict) else None,
        extra.get("browser_profile", {}).get("geo") if isinstance(extra.get("browser_profile"), dict) else None,
        cloak_locale.get("geo") if isinstance(cloak_locale, dict) else None,
    ]
    return next((dict(item) for item in candidates if isinstance(item, dict) and item), {})


def derive_account_locale(
    *,
    extra: dict[str, Any] | None = None,
    geo: dict[str, Any] | None = None,
    proxy_country_code: str | None = None,
) -> dict[str, str]:
    """Return persisted locale fields for one account.

    GeoIP is authoritative. A provider country code is only a fallback for
    cloud sessions that expose the requested country but not the measured IP.
    """
    payload = dict(extra or {})
    proxy_country_code = proxy_country_code or str(payload.get("proxy_country_code") or "").strip()
    observed_geo = dict(geo or {}) or _geo_from_extra(payload)
    country = str(
        observed_geo.get("country")
        or observed_geo.get("country_code")
        or observed_geo.get("countryCode")
        or ""
    ).strip().upper()
    locale = str(payload.get("account_locale") or "").strip().lower()
    source = "geoip" if country else ""

    if not country:
        country = str(payload.get("account_country") or "").strip().upper()
    if not country and proxy_country_code:
        country = str(proxy_country_code).strip().upper()
        source = "proxy_country"
    locale = locale or _country_to_locale(country)
    return {
        "account_locale": locale,
        "account_country": country,
        "account_locale_source": source,
    }
