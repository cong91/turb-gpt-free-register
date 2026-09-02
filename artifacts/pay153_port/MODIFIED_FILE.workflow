"""Direct PAY.153 provider checkout workflow for local link extraction."""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any, Callable

from core import pay153_provider_checkout as provider_checkout
from core import pay153_stripe_checkout as stripe_checkout
from core.pay153_checkout_extractor import (
    checkout_amount_minor,
    checkout_currency,
    checkout_state_from_html,
)
from core.pay153_provider_policy import PROVIDER_POLICIES, public_provider_config, provider_policy


PAY153_LOCAL_LINK_TYPES = frozenset(PROVIDER_POLICIES)
_PROVIDER_LABELS = {
    "hosted": "Official Checkout",
    "ph_short": "Philippines short checkout",
    "paypal": "PayPal",
    "ideal": "iDEAL",
    "twint": "TWINT",
    "upi": "UPI",
    "pix": "PIX",
    "momo": "MoMo",
    "gcash": "GCash",
    "kakao": "Kakao Pay",
}
_OPENAI_CHECKOUT_CURRENCIES = {
    "USD", "AUD", "CAD", "GBP", "EUR", "CLP", "JPY", "INR", "IDR", "PKR",
    "THB", "MYR", "TWD", "VND", "PHP", "NGN", "ZAR", "KZT", "TZS", "EGP",
    "BRL", "SEK", "CZK", "PLN", "DKK", "NOK", "KRW", "COP", "MXN", "PEN",
    "HUF", "QAR", "RON", "ILS", "AED", "SGD", "NZD", "CHF", "SAR",
}


def _upi_enabled() -> bool:
    from core.pay153_upi_go_runner import available

    return available()


def payment_method_catalog() -> list[dict[str, Any]]:
    """Return the PAY.153 provider catalog for the WebUI selector."""
    providers = public_provider_config(upi_enabled=_upi_enabled())
    methods = []
    for method_id in (
        "hosted", "ph_short", "paypal", "ideal", "twint", "upi", "pix", "momo", "gcash", "kakao",
    ):
        item = providers[method_id]
        if not item["api_enabled"] or not item["ui_visible"]:
            continue
        methods.append({
            "id": method_id,
            "label": _PROVIDER_LABELS[method_id],
            "country": item["country"],
            "currency": item["currency"],
            "route_shape": item["route_shape"],
            "recommendation": item["recommendation"],
        })
    return methods


def validate_provider(value: str) -> str:
    """Validate a local PAY.153 provider identifier without UI aliases."""
    provider = str(value or "").strip().lower()
    if provider not in PAY153_LOCAL_LINK_TYPES:
        raise ValueError("Unsupported PAY.153 payment method")
    if provider == "upi" and not _upi_enabled():
        raise ValueError("UPI checkout binary is not available")
    return provider


def _decode_jwt(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_access_token(raw: str) -> tuple[str, dict[str, Any]]:
    """Parse the stored account token exactly as PAY.153 accepts it."""
    source = str(raw or "").strip()
    if not source:
        raise ValueError("Access token is empty")
    token = ""
    metadata: dict[str, Any] = {}
    if source.startswith("{"):
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as exc:
            raise ValueError("Access token JSON is invalid") from exc
        if isinstance(payload, dict):
            token = str(payload.get("accessToken") or payload.get("access_token") or "")
            account = payload.get("account")
            if isinstance(account, dict):
                metadata.update(account)
    if not token:
        match = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", source)
        token = match.group(0) if match else source.splitlines()[0].strip()
    if token.count(".") < 2:
        raise ValueError("Access token format is not recognized")
    claims = _decode_jwt(token)
    metadata.update({
        "email": claims.get("email") or metadata.get("email") or "",
        "exp": claims.get("exp"),
        "account_id": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        or metadata.get("id") or "",
    })
    expiry = metadata.get("exp")
    if expiry:
        try:
            expired = int(expiry) <= int(time.time())
        except (TypeError, ValueError) as exc:
            raise ValueError("Access token expiry is invalid") from exc
        if expired:
            raise ValueError("Access token has expired")
    return token, metadata


def _checkout_currency(country: str, requested_currency: str) -> str:
    value = str(requested_currency or "").upper()
    if value in _OPENAI_CHECKOUT_CURRENCIES:
        return value
    fallback = stripe_checkout.currency_for_country(country)
    return fallback if fallback in _OPENAI_CHECKOUT_CURRENCIES else "USD"


def _sentinel_headers(proxy: str | None, flow: str, device_id: str, did: str) -> dict[str, str]:
    """Use Turb's Sentinel runner with PAY.153's checkout request sequence."""
    from core.sentinel import build_sentinel_request_body, generate_requirements_token
    from core.sentinel_runner import generate_sentinel_token

    sentinel_sid = str(uuid.uuid4())
    http = stripe_checkout.build_http(proxy)
    try:
        response = http.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=build_sentinel_request_body(generate_requirements_token(sentinel_sid), device_id, flow),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
                "User-Agent": stripe_checkout.CHROME_UA,
            },
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Sentinel request HTTP {response.status_code}")
        challenge = response.json()
        if not isinstance(challenge, dict):
            raise RuntimeError("Sentinel returned an invalid challenge")
    finally:
        try:
            http.close()
        except Exception:
            pass
    token_text = generate_sentinel_token(
        challenge=challenge,
        flow=flow,
        device_id=device_id,
        user_agent=stripe_checkout.CHROME_UA,
        page_url="https://chatgpt.com/",
        sentinel_sid=sentinel_sid,
        cookie=f"oai-did={did}",
    )
    headers = {"OpenAI-Sentinel-Token": token_text}
    try:
        token_payload = json.loads(token_text)
        so_value = token_payload.get("so") if isinstance(token_payload, dict) else ""
        if so_value:
            headers["OpenAI-Sentinel-SO-Token"] = json.dumps({
                "so": so_value,
                "c": token_payload.get("c") or challenge.get("token") or "",
                "id": device_id,
                "flow": flow,
            }, separators=(",", ":"))
    except (TypeError, ValueError):
        pass
    return headers


def _checkout_payload(provider: str, country: str, currency: str, campaign_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "redirect" if provider == "hosted" else "custom",
        "check_card_proxy": True,
    }
    if provider not in {"pix", "momo", "gcash", "paypal", "upi", "ideal", "twint"}:
        payload["promo_campaign"] = {
            "promo_campaign_id": campaign_id,
            "is_coupon_from_query_param": False,
        }
    return payload


def _create_checkout(
    token: str,
    payload: dict[str, Any],
    proxy: str | None,
    device_id: str,
    did: str,
    log: Callable[[str], None],
) -> tuple[dict[str, Any], Any]:
    http = stripe_checkout.build_http(proxy)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
        http.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"User-Agent": stripe_checkout.CHROME_UA, "Accept": "application/json,text/plain,*/*"},
            timeout=20,
        )
    except Exception as exc:
        log(f"Checkout warmup note: {type(exc).__name__}")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": stripe_checkout.CHROME_UA,
        "OAI-Language": "en-US",
        "OAI-Device-Id": device_id,
        **_sentinel_headers(proxy, "chatgpt_checkout", device_id, did),
    }
    response = http.post(stripe_checkout.OPENAI_CHECKOUT_URL, json=payload, headers=headers, timeout=60)
    text = response.text or ""
    if response.status_code != 200:
        raise RuntimeError(f"OpenAI Checkout HTTP {response.status_code}: {text[:300]}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("OpenAI Checkout returned non-JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("OpenAI Checkout returned an invalid payload")
    raw_session_id = "\n".join((str(data.get("checkout_session_id") or ""), str(data.get("url") or ""), text))
    stripe_match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", raw_session_id)
    custom_match = re.search(r"oaics_[A-Za-z0-9]+", raw_session_id)
    if stripe_match:
        session_id = stripe_match.group(0)
        data["checkout_session_id"] = session_id
        data["checkout_url"] = _normalize_hosted_url(str(data.get("url") or ""), session_id)
    elif custom_match:
        session_id = custom_match.group(0)
        processor = str(data.get("processor_entity") or "openai_ie").strip() or "openai_ie"
        data.update({
            "checkout_session_id": session_id,
            "processor_entity": processor,
            "is_custom_checkout": True,
            "checkout_url": f"https://chatgpt.com/checkout/{processor}/{session_id}",
        })
    else:
        raise RuntimeError("Checkout did not return a session ID")
    return data, http


def _normalize_hosted_url(url: str, session_id: str) -> str:
    value = str(url or "").strip() or f"https://pay.openai.com/c/pay/{session_id}"
    if value.startswith("https://checkout.stripe.com/c/pay/"):
        return "https://pay.openai.com" + value[len("https://checkout.stripe.com"):]
    return value


def _preflight_campaign(
    token: str,
    account_id: str,
    proxy: str | None,
    device_id: str,
    did: str,
    log: Callable[[str], None],
) -> str:
    if not account_id:
        return ""
    http = stripe_checkout.build_http(proxy)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
        response = http.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "OAI-Device-Id": device_id,
                "ChatGPT-Account-ID": account_id,
            },
            timeout=35,
        )
        if response.status_code != 200:
            log(f"Promotion catalog HTTP {response.status_code}")
            return ""
        payload = response.json()
        accounts = payload.get("accounts") if isinstance(payload, dict) else {}
        account = accounts.get(account_id) or accounts.get("default") or {}
        campaign = ((account.get("eligible_promo_campaigns") or {}).get("plus") or {})
        campaign_id = str(campaign.get("id") or campaign.get("campaign_id") or "").strip()
        if campaign_id:
            log("Account promotion catalog matched a Plus campaign")
        return campaign_id
    except Exception as exc:
        log(f"Promotion catalog note: {type(exc).__name__}")
        return ""
    finally:
        try:
            http.close()
        except Exception:
            pass


def _update_promotion(
    http: Any,
    token: str,
    session_id: str,
    processor: str,
    campaign_id: str,
    device_id: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    response = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/update",
        json={
            "checkout_session_id": session_id,
            "processor_entity": processor,
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "discount_code": None,
            "promo_campaign": {
                "promo_campaign_id": campaign_id,
                "is_coupon_from_query_param": False,
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=45,
    )
    text = response.text or ""
    log(f"Promotion update: HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"Promotion update HTTP {response.status_code}: {text[:300]}")
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _approve_checkout(
    token: str,
    session_id: str,
    processor: str,
    proxy: str | None,
    device_id: str,
    did: str,
    http: Any,
    log: Callable[[str], None],
) -> dict[str, Any]:
    response = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": session_id, "processor_entity": processor},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "OAI-Device-Id": device_id,
            "User-Agent": stripe_checkout.CHROME_UA,
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
            **_sentinel_headers(proxy, "checkout_session_approval", device_id, did),
        },
        timeout=45,
    )
    text = response.text or ""
    log(f"Checkout approval: HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"Checkout approval HTTP {response.status_code}: {text[:300]}")
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and str(payload.get("result") or "").lower() not in {"", "approved"}:
        raise RuntimeError("Checkout approval was not accepted")
    return payload if isinstance(payload, dict) else {}


def _custom_checkout_state(http: Any, token: str, session_id: str, processor: str, device_id: str) -> dict[str, Any]:
    response = http.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{session_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
        },
        timeout=45,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Custom checkout read HTTP {response.status_code}: {(response.text or '')[:300]}")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _custom_checkout_taxes(
    http: Any,
    token: str,
    session_id: str,
    processor: str,
    billing: dict[str, Any],
    currency: str,
    device_id: str,
) -> dict[str, Any]:
    address = dict(billing.get("address") or {})
    response = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        json={
            "checkout_session_id": session_id,
            "checkout_email": str(billing.get("email") or ""),
            "billing_country": str(address.get("country") or "PH").upper(),
            "billing_name": str(billing.get("name") or ""),
            "currency": str(currency or "PHP").upper(),
            "tax_id": str(billing.get("tax_id") or "") or None,
            "processor_entity": processor,
            "billing_address": {
                "country": str(address.get("country") or "PH").upper(),
                "line1": str(address.get("line1") or ""),
                "line2": str(address.get("line2") or ""),
                "city": str(address.get("city") or ""),
                "state": str(address.get("state") or ""),
                "postal_code": str(address.get("postal_code") or ""),
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=50,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Custom checkout tax update HTTP {response.status_code}: {(response.text or '')[:300]}")
    payload = response.json()
    checkout = payload.get("checkout_session") if isinstance(payload, dict) else {}
    return checkout if isinstance(checkout, dict) else {}


def _custom_confirm_and_start(
    http: Any,
    token: str,
    session_id: str,
    processor: str,
    method_id: str,
    proxy: str | None,
    device_id: str,
    did: str,
) -> dict[str, Any]:
    confirm = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={"checkout_session_id": session_id, "selected_payment_method_type": method_id},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
            **_sentinel_headers(proxy, "checkout_session_approval", device_id, did),
        },
        timeout=50,
    )
    if confirm.status_code != 200:
        raise RuntimeError(f"Custom checkout confirm HTTP {confirm.status_code}: {(confirm.text or '')[:300]}")
    confirmed = confirm.json()
    if not isinstance(confirmed, dict) or str(confirmed.get("status") or "").lower() != "success":
        raise RuntimeError("Custom checkout payment method was not accepted")
    start = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start",
        json={"checkout_session_id": session_id, "custom_payment_method_type_id": method_id},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": f"https://chatgpt.com/checkout/{processor}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
            "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/start",
            "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/start",
        },
        timeout=60,
    )
    if start.status_code != 200:
        raise RuntimeError(f"Custom checkout start HTTP {start.status_code}: {(start.text or '')[:300]}")
    payload = start.json()
    action = payload.get("next_action") if isinstance(payload, dict) else {}
    if str(payload.get("status") or "").lower() != "requires_action" or not isinstance(action, dict) or not action.get("url"):
        raise RuntimeError("Custom checkout did not return a redirect URL")
    return {"confirmed": confirmed, "started": payload}


def _run_custom_provider(
    provider: str,
    token: str,
    session_id: str,
    processor: str,
    country: str,
    currency: str,
    proxy: str | None,
    device_id: str,
    did: str,
    http: Any,
    promo_http: Any,
    campaign_id: str,
    email: str,
) -> dict[str, Any]:
    if provider not in {"gcash", "paypal"}:
        raise RuntimeError(f"CUSTOM_CHECKOUT_REBUILD_REQUIRED: received {session_id}; {provider} requires a Stripe cs_* checkout")
    state = _custom_checkout_state(http, token, session_id, processor, device_id)
    amount = checkout_amount_minor(state)
    actual_currency = checkout_currency(state) or currency
    if amount not in {None, 0}:
        _update_promotion(promo_http, token, session_id, processor, campaign_id, device_id, lambda _message: None)
        state = _custom_checkout_state(http, token, session_id, processor, device_id)
        amount = checkout_amount_minor(state)
        actual_currency = checkout_currency(state) or actual_currency
    billing_country = "PH" if provider == "gcash" else country
    billing = provider_checkout.default_billing(billing_country, email, real_random=True)
    taxed = _custom_checkout_taxes(http, token, session_id, processor, billing, actual_currency, device_id)
    if taxed:
        state = taxed
        amount = checkout_amount_minor(state)
        actual_currency = checkout_currency(state) or actual_currency
    methods = [item for item in state.get("custom_payment_methods") or [] if isinstance(item, dict)]
    if provider == "paypal":
        methods.sort(key=lambda item: 0 if "paypal" in json.dumps(item).lower() else 1)
    method = next((item for item in methods if str(item.get("id") or "").startswith("cpmt_")), None)
    if not method:
        raise RuntimeError(f"{provider.upper()} custom payment method is not available")
    custom = _custom_confirm_and_start(http, token, session_id, processor, str(method["id"]), proxy, device_id, did)
    action = custom["started"].get("next_action") or {}
    redirect = str(action.get("url") or "")
    if not redirect:
        raise RuntimeError(f"{provider.upper()} custom checkout returned no redirect")
    if amount not in {None, 0}:
        raise RuntimeError(f"{provider.upper()} promotion is not applied: amount={amount} {actual_currency}")
    return {
        "provider": provider,
        "provider_redirect_url": redirect,
        "checkout_url": redirect,
        "short_link": redirect,
        "custom_payment_method_id": str(method["id"]),
        "payment_method_type": str(action.get("paymentMethodType") or provider),
        "checkout_amount": amount,
        "checkout_currency": actual_currency,
        "amount_verification": "verified_zero" if amount == 0 else "pending",
        "promo_applied": amount == 0 if amount is not None else None,
        "expires_at": int(time.time()) + 1800,
    }


def _run_hosted_checkout(
    token: str,
    checkout: dict[str, Any],
    http: Any,
    proxy: str | None,
    device_id: str,
    country: str,
    currency: str,
    campaign_id: str,
    email: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    session_id = str(checkout.get("checkout_session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    if checkout.get("is_custom_checkout"):
        update = _update_promotion(http, token, session_id, processor, campaign_id, device_id, log)
        amount = checkout_amount_minor(update)
        return {
            "checkout_url": str(checkout.get("checkout_url") or ""),
            "short_link": str(checkout.get("checkout_url") or ""),
            "checkout_amount": amount,
            "checkout_currency": checkout_currency(update) or currency,
            "amount_verification": "verified_zero" if amount == 0 else "pending",
            "promo_applied": amount == 0 if amount is not None else None,
        }
    profile = stripe_checkout._profile(country)
    hosted_http = stripe_checkout.build_http(proxy)
    publishable_key = str(checkout.get("publishable_key") or "") or stripe_checkout.verify_pk(hosted_http, session_id, log)
    _, version, context = stripe_checkout.init_checkout(hosted_http, session_id, publishable_key, profile, log)
    amount = context.get("checkout_amount")
    if amount not in {0, "0", "0.0", "0.00"}:
        _update_promotion(http, token, session_id, processor, campaign_id, device_id, log)
        _, version, context = stripe_checkout.init_checkout(hosted_http, session_id, publishable_key, profile, log)
        amount = context.get("checkout_amount")
    billing = provider_checkout.default_billing(country, email)
    stripe_checkout.update_tax_region(hosted_http, session_id, publishable_key, version, context, billing, profile, log)
    amount = context.get("checkout_amount")
    if amount not in {0, "0", "0.0", "0.00"}:
        raise RuntimeError(f"Hosted Checkout promotion is not applied: amount={amount}")
    hosted_url = _normalize_hosted_url(str(context.get("stripe_hosted_url") or checkout.get("checkout_url") or ""), session_id)
    return {
        "checkout_url": hosted_url,
        "short_link": hosted_url,
        "checkout_amount": amount,
        "checkout_currency": str(context.get("currency") or currency).upper(),
        "payment_method_types": context.get("payment_method_types") or [],
        "processor_entity": processor,
        "stripe_publishable_key": publishable_key,
        "amount_verification": "verified_zero",
        "promo_applied": True,
    }


def run_provider_checkout(
    raw_token: str,
    provider_name: str,
    *,
    entry_proxy: str | None,
    payment_proxy: str | None,
    promotion_proxy: str | None,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Run PAY.153's direct provider flow and return its normalized result."""
    provider = validate_provider(provider_name)
    token, metadata = extract_access_token(raw_token)
    policy = provider_policy(provider)
    country = policy.country
    currency = _checkout_currency(country, policy.currency)
    device_id = str(uuid.uuid4())
    did = str(uuid.uuid4())
    campaign_id = _preflight_campaign(token, str(metadata.get("account_id") or ""), promotion_proxy or entry_proxy, device_id, did, log)
    campaign_id = campaign_id or "plus-1-month-free"
    checkout_proxy = payment_proxy if provider in {"paypal", "upi", "ideal", "twint"} else entry_proxy
    checkout, chatgpt_http = _create_checkout(
        token,
        _checkout_payload(provider, country, currency, campaign_id),
        checkout_proxy,
        device_id,
        did,
        log,
    )
    session_id = str(checkout.get("checkout_session_id") or "")
    processor = str(checkout.get("processor_entity") or "openai_llc")
    result: dict[str, Any] = {
        "plan": "plus",
        "link_type": provider,
        "checkout_session_id": session_id,
        "checkout_url": str(checkout.get("checkout_url") or ""),
        "processor_entity": processor,
        "account_email": str(metadata.get("email") or ""),
        "account_id": str(metadata.get("account_id") or ""),
        "country": country,
        "currency": currency,
        "checkout_country": country,
        "checkout_currency": currency,
        "proxy_mode": policy.route_shape,
        "promo_requested": True,
        "promo_campaign_used": campaign_id,
    }
    try:
        if provider == "hosted":
            result.update(_run_hosted_checkout(token, checkout, chatgpt_http, entry_proxy, device_id, country, currency, campaign_id, str(metadata.get("email") or ""), log))
            return result
        if session_id.startswith("oaics_"):
            promo_http = stripe_checkout.build_http(promotion_proxy or entry_proxy)
            result.update(_run_custom_provider(provider, token, session_id, processor, country, currency, checkout_proxy, device_id, did, chatgpt_http, promo_http, campaign_id, str(metadata.get("email") or "")))
            return result
        if provider == "upi":
            from core.pay153_upi_go_runner import run_upi

            go_result = run_upi(
                token=token,
                proxy=payment_proxy or entry_proxy or "",
                provider_proxy=payment_proxy or "",
                promotion_proxy=promotion_proxy or entry_proxy or "",
                billing=provider_checkout.default_billing("IN", str(metadata.get("email") or "")),
                log=log,
            )
            result.update(go_result)
            return result
        promo_http = chatgpt_http if provider in {"pix", "momo"} else stripe_checkout.build_http(promotion_proxy or entry_proxy)
        provider_http = stripe_checkout.build_http(payment_proxy or entry_proxy)

        def apply_promo(active_processor: str) -> dict[str, Any]:
            return _update_promotion(promo_http, token, session_id, active_processor, campaign_id, device_id, log)

        def approve(active_processor: str) -> dict[str, Any]:
            return _approve_checkout(token, session_id, active_processor, checkout_proxy, device_id, did, chatgpt_http, log)

        billing = provider_checkout.default_billing(
            country,
            str(metadata.get("email") or ""),
            real_random=provider in {"paypal", "gcash"},
        )
        provider_result = provider_checkout.stripe_to_provider(
            provider_http,
            session_id,
            provider,
            billing=billing,
            country=country,
            chatgpt_http=chatgpt_http,
            access_token=token,
            stage1=checkout,
            approve_callback=None if provider == "paypal" else approve,
            apply_promo_callback=apply_promo,
            require_zero_due=True,
            local_method_strategy="standalone",
            log=log,
        )
        result.update(provider_result)
        if provider_result.get("checkout_currency"):
            result["currency"] = str(provider_result["checkout_currency"]).upper()
            result["checkout_currency"] = result["currency"]
        return result
    finally:
        try:
            chatgpt_http.close()
        except Exception:
            pass
