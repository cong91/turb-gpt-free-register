from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteStagePolicy:
    role: str
    required: bool
    default_country: str
    fixed_country: str | None = None


@dataclass(frozen=True)
class ProviderPolicy:
    country: str
    currency: str
    route_shape: str
    api_enabled: bool = True
    ui_visible: bool = True
    stages: tuple[RouteStagePolicy, ...] = field(default_factory=tuple)
    recommendation: str = ""


PROVIDER_POLICIES = {
    "hosted": ProviderPolicy(
        "US", "USD", "entry_only",
        stages=(RouteStagePolicy("entry", True, "US"),),
        recommendation="使用账号常用地区。",
    ),
    "ph_short": ProviderPolicy(
        "PH", "PHP", "dual",
        stages=(
            RouteStagePolicy("checkout", True, "US"),
            RouteStagePolicy("promotion", True, "TR"),
        ),
        recommendation="代理池 1 使用 US 创建 PH/PHP Checkout，代理池 2 使用 TR 应用优惠。",
    ),
    "paypal": ProviderPolicy(
        "US", "USD", "dual",
        stages=(
            RouteStagePolicy("promotion", True, "US"),
            RouteStagePolicy("payment", True, "US"),
        ),
        recommendation="使用支付地区一致的 PayPal 账单线路。",
    ),
    "ideal": ProviderPolicy(
        "NL", "EUR", "dual",
        stages=(
            RouteStagePolicy("entry", True, "NL", "NL"),
            RouteStagePolicy("payment", True, "NL", "NL"),
        ),
        recommendation="两个阶段均使用 NL。",
    ),
    "twint": ProviderPolicy(
        "CH", "CHF", "dual", ui_visible=False,
        stages=(
            RouteStagePolicy("entry", True, "CH", "CH"),
            RouteStagePolicy("payment", True, "CH", "CH"),
        ),
        recommendation="两个阶段均使用 CH。",
    ),
    "upi": ProviderPolicy(
        "IN", "INR", "dual",
        stages=(
            RouteStagePolicy("promotion", True, "VN"),
            RouteStagePolicy("provider", True, "IN", "IN"),
        ),
        recommendation="优惠阶段使用 VN，支付阶段使用 IN。",
    ),
    "pix": ProviderPolicy(
        "BR", "BRL", "single_chain",
        stages=(RouteStagePolicy("chain", True, "BR", "BR"),),
        recommendation="全程使用 BR。",
    ),
    "momo": ProviderPolicy(
        "VN", "VND", "single_chain",
        stages=(RouteStagePolicy("chain", True, "VN", "VN"),),
        recommendation="全程使用 VN。",
    ),
    "gcash": ProviderPolicy(
        "PH", "PHP", "dual",
        stages=(
            RouteStagePolicy("checkout", True, "US"),
            RouteStagePolicy("promotion", True, "VN"),
        ),
        recommendation="Checkout 使用 US，优惠阶段使用 VN。",
    ),
    "kakao": ProviderPolicy(
        "KR", "KRW", "dual",
        stages=(
            RouteStagePolicy("promotion", True, "VN"),
            RouteStagePolicy("payment", True, "KR", "KR"),
        ),
        recommendation="优惠阶段使用 VN，支付阶段使用 KR。",
    ),
}


def _normalize_country(value: str, *, field_name: str) -> str:
    country = str(value or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise ValueError(f"{field_name} must be a two-letter country code")
    return country


def provider_policy(name: str) -> ProviderPolicy:
    key = str(name or "").strip().lower()
    try:
        return PROVIDER_POLICIES[key]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {name}") from exc


def provider_defaults() -> dict[str, dict[str, str]]:
    return {
        name: {"country": policy.country, "currency": policy.currency}
        for name, policy in PROVIDER_POLICIES.items()
    }


def _stage_dict(stage: RouteStagePolicy) -> dict[str, Any]:
    return {
        "role": stage.role,
        "required": stage.required,
        "default_country": stage.default_country,
        "fixed_country": stage.fixed_country,
    }


def public_provider_config(*, upi_enabled: bool) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "country": policy.country,
            "currency": policy.currency,
            "route_shape": policy.route_shape,
            "api_enabled": policy.api_enabled and (upi_enabled or name != "upi"),
            "ui_visible": policy.ui_visible,
            "stages": [_stage_dict(stage) for stage in policy.stages],
            "recommendation": policy.recommendation,
        }
        for name, policy in PROVIDER_POLICIES.items()
    }


def route_countries(
    name: str,
    selected_country: str,
    promo_country: str,
    use_promo: bool,
) -> tuple[str, ...]:
    policy = provider_policy(name)
    selected = _normalize_country(selected_country, field_name="selected_country")
    promo = _normalize_country(promo_country, field_name="promo_country") if promo_country else ""

    if policy.route_shape == "entry_only":
        return (selected,)
    if policy.route_shape == "single_chain":
        expected = policy.stages[0].fixed_country or policy.stages[0].default_country
        if selected != expected:
            raise ValueError(f"{name} requires {expected}")
        return (expected,)
    if name == "ph_short":
        return ("US", (promo or "TR") if use_promo else "US")
    if name == "gcash":
        return ("US", promo or ("VN" if use_promo else "US"))
    if name == "upi":
        return (promo or "VN", "IN")
    if name == "paypal":
        return (selected, promo or selected)
    if name == "kakao":
        payment_country = policy.stages[-1].fixed_country or "KR"
        if selected != payment_country:
            raise ValueError(f"{name} requires {payment_country}")
        return (promo or "VN", payment_country)

    fixed = [stage.fixed_country for stage in policy.stages]
    if any(country and selected != country for country in fixed):
        raise ValueError(f"{name} requires {next(country for country in fixed if country)}")
    return tuple(fixed_country or selected for fixed_country in fixed)


# Kept as a named compatibility export for callers migrating from provider_checkout.
PROVIDER_DEFAULTS = provider_defaults()
