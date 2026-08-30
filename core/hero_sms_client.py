"""HeroSMS client using its SMS-Activate-compatible HTTP protocol."""
from __future__ import annotations

import logging
import threading
from decimal import Decimal, InvalidOperation

from core.hero_sms_country_store import (
    HeroSmsCountryStore,
    HeroSmsCountryStoreError,
    make_profile_key,
)

logger = logging.getLogger(__name__)

_AUTO_COUNTRY_CURSOR: dict[tuple[str, str, str], int] = {}
_AUTO_COUNTRY_LOCK = threading.Lock()
_COUNTRY_STORE = HeroSmsCountryStore()


class HeroSmsClientError(RuntimeError):
    """HeroSMS API or response error."""

    def __init__(self, message: str, *, code: str = "", raw: str = ""):
        super().__init__(message)
        self.code = code
        self.raw = raw


def _require(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise HeroSmsClientError(f"{name} 不能为空")
    return result


def _error_from_text(text: str) -> HeroSmsClientError | None:
    value = str(text or "").strip()
    if not value:
        return None
    code = value.split(":", 1)[0].strip().upper()
    known = {
        "BAD_KEY", "NO_KEY", "NO_BALANCE", "NO_NUMBERS", "BAD_ACTION",
        "BAD_SERVICE", "BAD_COUNTRY", "BAD_STATUS", "NO_ACTIVATION",
        "WRONG_ACTIVATION_ID", "EARLY_CANCEL_DENIED", "BANNED",
        "SERVICE_NOT_AVAILABLE", "SIM_OFFLINE", "CHANNELS_LIMIT",
        "WRONG_MAX_PRICE", "ACTIVATION_NOT_ACTIVE", "FINISHED",
        "REFUNDED", "CANCELED",
    }
    if code not in known:
        return None
    return HeroSmsClientError(f"HeroSMS {value}", code=code, raw=value)


def request(http, *, base_url: object, api_key: object, action: str, **params):
    base = _require(base_url, "HERO_SMS_API_BASE")
    key = _require(api_key, "HERO_SMS_API_KEY")
    query = {"api_key": key, "action": action}
    query.update({name: value for name, value in params.items() if value not in (None, "")})
    response = http.get(base, params=query)
    raw = str(getattr(response, "text", "") or "").strip()
    if getattr(response, "status_code", 200) != 200:
        raise HeroSmsClientError(
            f"HeroSMS HTTP {getattr(response, 'status_code', '?')}: {raw[:200]}"
        )
    try:
        payload = response.json()
    except ValueError:
        payload = raw
    if isinstance(payload, str):
        error = _error_from_text(payload)
        if error:
            raise error
        return payload.strip()
    if isinstance(payload, (dict, list)):
        if isinstance(payload, dict):
            title = str(payload.get("title") or payload.get("error") or "").strip().upper()
            if title:
                error = _error_from_text(title)
                if error:
                    raise error
        return payload
    raise HeroSmsClientError(f"HeroSMS 响应格式异常：{raw[:200]}")


def _number_candidates(
    prices: object,
    *,
    service: str,
    max_price: str = "",
    limit: int | None = None,
) -> list[tuple[str, Decimal]]:
    if not isinstance(prices, dict):
        raise HeroSmsClientError("HeroSMS getPrices 响应不是对象")
    price_limit = None
    if max_price:
        try:
            price_limit = Decimal(str(max_price))
        except InvalidOperation as exc:
            raise HeroSmsClientError(f"SMS_MAX_PRICE 无效：{max_price}") from exc
    candidates: list[tuple[Decimal, str]] = []
    for country, country_prices in prices.items():
        if not isinstance(country_prices, dict):
            continue
        offer = country_prices.get(service)
        if not isinstance(offer, dict):
            continue
        try:
            cost = Decimal(str(offer.get("cost")))
            count = Decimal(str(offer.get("count", 0)))
        except (InvalidOperation, TypeError):
            continue
        if count <= 0 or cost < 0 or (price_limit is not None and cost > price_limit):
            continue
        candidates.append((cost, str(country)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    if limit is not None:
        candidates = candidates[:max(0, int(limit))]
    return [(country, cost) for cost, country in candidates]


def _rotate_candidates(
    candidates: list[tuple[str, Decimal]],
    *,
    cursor_key: tuple[str, str, str],
) -> list[tuple[str, Decimal]]:
    if len(candidates) <= 1:
        return list(candidates)
    with _AUTO_COUNTRY_LOCK:
        start = _AUTO_COUNTRY_CURSOR.get(cursor_key, 0) % len(candidates)
        _AUTO_COUNTRY_CURSOR[cursor_key] = (start + 1) % len(candidates)
    return candidates[start:] + candidates[:start]


def _price_ordered_candidates(
    candidates: list[tuple[str, Decimal]],
    *,
    sticky_candidate: tuple[str, Decimal] | None,
    api_base: object,
    service: str,
    price: str,
) -> tuple[list[tuple[str, Decimal]], str]:
    """Sort every live offer by cost, rotating only equal-cost offers."""
    ordered_by_cost = sorted(candidates, key=lambda item: (item[1], item[0]))
    if not ordered_by_cost:
        return [], "-"

    lowest_cost = ordered_by_cost[0][1]
    sticky_mode = (
        "preferred"
        if sticky_candidate is not None and sticky_candidate[1] == lowest_cost
        else "deferred_for_price"
        if sticky_candidate is not None
        else "-"
    )
    ordered: list[tuple[str, Decimal]] = []
    index = 0
    while index < len(ordered_by_cost):
        cost = ordered_by_cost[index][1]
        end = index + 1
        while end < len(ordered_by_cost) and ordered_by_cost[end][1] == cost:
            end += 1
        same_cost_candidates = ordered_by_cost[index:end]
        same_cost_sticky = (
            sticky_candidate if sticky_candidate in same_cost_candidates else None
        )
        if same_cost_sticky is not None:
            ordered.append(same_cost_sticky)
            same_cost_candidates = [
                item for item in same_cost_candidates if item != same_cost_sticky
            ]
        ordered.extend(
            _rotate_candidates(
                same_cost_candidates,
                cursor_key=(str(api_base).strip(), service, f"{price}:cost:{cost}"),
            )
        )
        index = end
    return ordered, sticky_mode


def acquire_number_with_metadata(
    http,
    *,
    api_base: object,
    api_key: object,
    service: object,
    country: object,
    max_price: object = "",
    lane_key: object = "",
) -> tuple[str, str, dict]:
    service_code = _require(service, "HERO_SMS_SERVICE")
    country_code = str(country or "").strip()
    price = str(max_price or "").strip()
    normalized_lane_key = str(lane_key or "").strip()
    profile_key = make_profile_key(
        api_base,
        service_code,
        price,
        lane_key=normalized_lane_key,
    )
    if country_code.lower() == "auto":
        prices = request(
            http,
            base_url=api_base,
            api_key=api_key,
            action="getPrices",
            service=service_code,
        )
        all_countries = _number_candidates(
            prices,
            service=service_code,
            max_price=price,
            limit=None,
        )
        try:
            blocked = _COUNTRY_STORE.blocked_countries_for_provider(api_base, service_code)
            sticky_countries = _COUNTRY_STORE.sticky_countries(profile_key)
        except HeroSmsCountryStoreError as exc:
            logger.warning("HeroSMS 国家记忆读取失败，继续按实时价格/库存选择：%s", exc)
            blocked = set()
            sticky_countries = []
        healthy_countries = [item for item in all_countries if item[0] not in blocked]
        risky_countries = [item for item in all_countries if item[0] in blocked]
        if not all_countries:
            raise HeroSmsClientError(
                f"HeroSMS 没有可用的 {service_code} 号码（getPrices）", code="NO_NUMBERS"
            )
        sticky_candidate = next(
            (
                item
                for country_id in sticky_countries
                for item in healthy_countries
                if item[0] == country_id
            ),
            None,
        )
        primary_selected: list[tuple[str, Decimal]] = []
        fallback_countries: list[tuple[str, Decimal]] = []
        sticky_mode = "-"
        if healthy_countries:
            primary_selected, sticky_mode = _price_ordered_candidates(
                healthy_countries,
                sticky_candidate=sticky_candidate,
                api_base=api_base,
                service=service_code,
                price=price,
            )
        if risky_countries:
            lowest_risk_price = risky_countries[0][1]
            recovery_pool = [
                item for item in risky_countries if item[1] == lowest_risk_price
            ]
            recovery_key = (str(api_base).strip(), service_code, f"{price}:recovery")
            fallback_countries = [
                _rotate_candidates(recovery_pool, cursor_key=recovery_key)[0]
            ]
        ordered_countries = [*primary_selected, *fallback_countries]
        logger.info(
            "[SMS:HeroSMS] auto 国家候选：lane=%s maxPrice=%s pool=%s sticky=%s stickyMode=%s primary=%s fallback=%s selected=%s",
            normalized_lane_key or "-",
            price or "-",
            len(all_countries),
            sticky_candidate[0] if sticky_candidate is not None else "-",
            sticky_mode,
            ",".join(f"{country}:{cost}" for country, cost in primary_selected) or "-",
            ",".join(f"{country}:{cost}" for country, cost in fallback_countries) or "-",
            ordered_countries[0][0],
        )
    else:
        ordered_countries = None
        countries = [(country_code or "0", None)]

    last_no_numbers: HeroSmsClientError | None = None
    for candidate_index, (country_id, candidate_cost) in enumerate(ordered_countries or countries):
        request_max_price = price or (str(candidate_cost) if candidate_cost is not None else "")
        if country_code.lower() == "auto":
            logger.info(
                "[SMS:HeroSMS] auto 取号尝试：lane=%s country=%s offerCost=%s maxPrice=%s candidate=%s/%s",
                normalized_lane_key or "-",
                country_id,
                str(candidate_cost) if candidate_cost is not None else "-",
                request_max_price or "-",
                candidate_index + 1,
                len(ordered_countries or countries),
            )
        try:
            response = request(
                http,
                base_url=api_base,
                api_key=api_key,
                action="getNumber",
                service=service_code,
                country=country_id,
                maxPrice=request_max_price,
            )
        except HeroSmsClientError as exc:
            if exc.code in ("NO_NUMBERS", "WRONG_MAX_PRICE") and country_code.lower() == "auto":
                last_no_numbers = exc
                try:
                    _COUNTRY_STORE.mark_unusable(
                        profile_key,
                        country_id,
                        f"getNumber {exc.code}",
                    )
                except HeroSmsCountryStoreError as store_exc:
                    logger.warning("HeroSMS 暂时不可用 country 记忆写入失败：%s", store_exc)
                logger.info(
                    "[SMS:HeroSMS] auto lane=%s country=%s offerCost=%s 取号失败 code=%s，继续尝试下一个 country",
                    normalized_lane_key or "-",
                    country_id,
                    str(candidate_cost) if candidate_cost is not None else "-",
                    exc.code,
                )
                continue
            raise
        if not isinstance(response, str) or not response.startswith("ACCESS_NUMBER:"):
            raise HeroSmsClientError(f"HeroSMS getNumber 响应异常：{str(response)[:200]}")
        parts = response.split(":", 2)
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            raise HeroSmsClientError(f"HeroSMS getNumber 响应格式异常：{response[:200]}")
        phone = "".join(ch for ch in parts[2] if ch.isdigit())
        if not phone:
            raise HeroSmsClientError(f"HeroSMS getNumber 号码异常：{response[:200]}")
        return parts[1].strip(), phone, {
            "remember_country": country_code.lower() == "auto",
            "profile_key": profile_key,
            "country": country_id,
            "price": str(candidate_cost or ""),
            "lane_key": normalized_lane_key,
        }
    raise last_no_numbers or HeroSmsClientError("HeroSMS 暂无可用号码", code="NO_NUMBERS")


def acquire_number(
    http,
    *,
    api_base: object,
    api_key: object,
    service: object,
    country: object,
    max_price: object = "",
    lane_key: object = "",
) -> tuple[str, str]:
    """Acquire a number while preserving the original two-value API."""
    activation_id, phone, _metadata = acquire_number_with_metadata(
        http,
        api_base=api_base,
        api_key=api_key,
        service=service,
        country=country,
        max_price=max_price,
        lane_key=lane_key,
    )
    return activation_id, phone


def record_country_unusable(metadata: dict | None, reason: str = "") -> None:
    if not isinstance(metadata, dict) or not metadata.get("remember_country"):
        return
    try:
        _COUNTRY_STORE.mark_unusable(
            str(metadata.get("profile_key") or ""),
            str(metadata.get("country") or ""),
            reason,
        )
    except HeroSmsCountryStoreError as exc:
        logger.warning("HeroSMS 失败国家记忆写入失败：%s", exc)


def record_country_verified(metadata: dict | None) -> None:
    if not isinstance(metadata, dict) or not metadata.get("remember_country"):
        return
    try:
        _COUNTRY_STORE.mark_verified(
            str(metadata.get("profile_key") or ""),
            str(metadata.get("country") or ""),
            metadata.get("price"),
        )
    except HeroSmsCountryStoreError as exc:
        logger.warning("HeroSMS 成功国家记忆写入失败：%s", exc)


def get_status(http, *, api_base: object, api_key: object, activation_id: str) -> str:
    response = request(
        http,
        base_url=api_base,
        api_key=api_key,
        action="getStatus",
        id=activation_id,
    )
    if not isinstance(response, str):
        raise HeroSmsClientError(f"HeroSMS getStatus 响应异常：{str(response)[:200]}")
    return response


def set_status(http, *, api_base: object, api_key: object, activation_id: str, status: int) -> str:
    response = request(
        http,
        base_url=api_base,
        api_key=api_key,
        action="setStatus",
        id=activation_id,
        status=str(status),
    )
    return str(response)
