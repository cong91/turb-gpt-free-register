"""MoMo OAICS custom-checkout helpers ported from PAY.153's reference flow.

The reference implementation is Sunny's ``pay153_checkout`` worker: the OAICS
contract (explicit method snapshots, blocked confirmations, low-value VND
promotions) is replicated here so turb's extract pipeline can drive the same
native MoMo confirm chain.
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from core import pay153_stripe_checkout as stripe_checkout
from core.pay153_checkout_extractor import checkout_amount_minor, checkout_currency

MOMO_PROMO_AMOUNT_LIMIT_VND = 50

_MOMO_AUTHORIZE_PATH_RE = re.compile(
    r"^/authorize/acct_[A-Za-z0-9]+/(?:pa|sa)_nonce_[A-Za-z0-9]+/?$",
    re.IGNORECASE,
)

_MOMO_REBUILD_MARKERS = (
    "momo_checkout_rebuild_required",
    "momo_promotion_incompatible_rebuild_required",
    "momo_create_promotion_not_applied_rebuild_required",
    "momo_method_removed_rebuild_required",
    "momo_promo_amount_required",
    "momo_oaics_confirm_blocked",
    "custom_confirm_blocked",
    "momo_redirect_missing",
)

_METHOD_CONTAINER_KEYS = frozenset({
    "custom_payment_methods",
    "payment_methods",
    "payment_method_types",
    "available_payment_methods",
    "payment_method_specs",
})
_METHOD_TYPE_KEYS = frozenset({
    "type",
    "name",
    "label",
    "display_name",
    "provider",
    "payment_method_type",
    "method_type",
    "custom_payment_method_type",
    "method",
})
_SAVED_METHOD_PARENT_KEYS = frozenset({
    "checkout_customer",
    "customer",
    "legacy_customer",
    "customer_info",
    "customer_session",
})
_STALE_METHOD_SNAPSHOT_PARENT_KEYS = frozenset({
    "archive",
    "archived",
    "history",
    "histories",
    "old",
    "previous",
    "prior",
    "stale",
})

_UNAVAILABLE_METHOD_MARKERS = {
    "blocked",
    "deprecated",
    "disabled",
    "hidden",
    "inactive",
    "ineligible",
    "not_available",
    "not_eligible",
    "not_enabled",
    "not_supported",
    "removed",
    "unavailable",
    "unsupported",
}
_METHOD_POSITIVE_AVAILABILITY_KEYS = frozenset({
    "active",
    "available",
    "eligible",
    "enabled",
    "is_active",
    "is_available",
    "is_eligible",
    "is_enabled",
    "is_supported",
    "is_visible",
    "supported",
    "visible",
})
_METHOD_NEGATIVE_AVAILABILITY_KEYS = frozenset({
    "blocked",
    "deprecated",
    "disabled",
    "hidden",
    "inactive",
    "ineligible",
    "is_blocked",
    "is_deprecated",
    "is_disabled",
    "is_hidden",
    "is_inactive",
    "is_ineligible",
    "is_removed",
    "is_unavailable",
    "is_unsupported",
    "removed",
    "unsupported",
})
_METHOD_TRUE_MARKERS = frozenset({"1", "true", "yes", "on"})
_METHOD_FALSE_MARKERS = frozenset({
    "0", "false", "no", "off", "disabled", "unavailable", "inactive",
})


def is_momo_promo_amount(amount: Any, currency: str) -> bool:
    """Accept the low-value VND total used by native OAICS MoMo SetupIntents."""
    if str(currency or "").strip().upper() != "VND" or amount is None or isinstance(amount, bool):
        return False
    try:
        parsed = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        return False
    return parsed.is_finite() and Decimal(0) <= parsed <= Decimal(MOMO_PROMO_AMOUNT_LIMIT_VND)


def momo_requires_rebuild(exc: Any) -> bool:
    """Classify reference rebuild markers carried in an error or its text."""
    message = str(exc or "").lower()
    return any(marker in message for marker in _MOMO_REBUILD_MARKERS)


def is_valid_momo_authorization_url(value: Any) -> bool:
    """Return whether a URL is a Stripe-hosted MoMo authorization handoff."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower().rstrip(".") == "pm-redirects.stripe.com"
        and bool(_MOMO_AUTHORIZE_PATH_RE.fullmatch(parsed.path))
    )


def momo_authorization_url(*payloads: Any) -> str:
    """Find the final Stripe MoMo handoff in nested confirm/start payloads."""
    found: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested, depth + 1)
        elif isinstance(value, str):
            candidate = value.strip()
            if is_valid_momo_authorization_url(candidate) and candidate not in found:
                found.append(candidate)
                return
            decoded = unquote(candidate)
            if decoded != candidate:
                walk(decoded, depth + 1)

    for payload in payloads:
        walk(payload)
    return found[0] if found else ""


def nested_scalar(payload: Any, keys: tuple[str, ...], depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            found = nested_scalar(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = nested_scalar(value, keys, depth + 1)
            if found:
                return found
    return ""


def redact_payment_error(value: Any) -> str:
    return re.sub(
        r"\b(?:ctoken|seti|pi)_[A-Za-z0-9_\-]+",
        "[PAYMENT_SECRET]",
        str(value or ""),
    )[:300]


def checkout_confirmation_is_blocked(payload: Any, raw_text: str = "") -> bool:
    """Detect upstream ``blocked`` markers while honouring negations."""
    markers: list[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key in ("status", "result", "code"):
                candidate = value.get(key)
                if isinstance(candidate, (str, int, float)):
                    markers.append(str(candidate).strip().lower())
            for nested in value.values():
                if isinstance(nested, (dict, list, tuple)):
                    collect(nested, depth + 1)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested, depth + 1)

    def is_blocked_marker(value: str) -> bool:
        normalized = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
        if normalized in {"not_blocked", "notblocked", "unblocked"}:
            return False
        return (
            normalized == "blocked"
            or normalized.startswith("blocked_")
            or normalized.endswith("_blocked")
        )

    collect(payload)
    if any(is_blocked_marker(marker) for marker in markers):
        return True
    raw_markers = re.findall(
        r'(?i)"(?:status|result|code)"\s*:\s*"([^"]*)"',
        str(raw_text or ""),
    )
    return any(is_blocked_marker(marker) for marker in raw_markers)


def _canonical_method_key(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _normalized_method_marker(value: Any) -> str:
    return _canonical_method_key(value)


def _marker_denotes_unavailable(normalized: str) -> bool:
    return any(
        normalized == marker
        or normalized.startswith(f"{marker}_")
        or normalized.endswith(f"_{marker}")
        or f"_{marker}_" in normalized
        for marker in _UNAVAILABLE_METHOD_MARKERS
    )


def _availability_scalar(value: Any) -> bool | None:
    if value is False or value == 0:
        return False
    if value is True or value == 1:
        return True
    if not isinstance(value, str):
        return None
    normalized = _normalized_method_marker(value)
    if normalized in _METHOD_TRUE_MARKERS or normalized in {
        "active", "available", "eligible", "enabled", "supported", "visible",
    }:
        return True
    if normalized in _METHOD_FALSE_MARKERS or _marker_denotes_unavailable(normalized):
        return False
    return None


def _method_entry_is_available(entry: dict[str, Any]) -> bool:
    for raw_key, value in entry.items():
        key = _canonical_method_key(raw_key)
        normalized = _normalized_method_marker(value)
        if key in _METHOD_POSITIVE_AVAILABILITY_KEYS:
            decision = _availability_scalar(value)
            if decision is False:
                return False
        elif key in _METHOD_NEGATIVE_AVAILABILITY_KEYS:
            decision = _availability_scalar(value)
            if decision is True:
                return False
        elif key in {"status", "availability", "eligibility", "state"}:
            decision = _availability_scalar(value)
            if decision is False:
                return False
            if isinstance(value, dict) and _method_mapping_availability(value) is False:
                return False
        elif isinstance(value, dict) and key in {
            "capabilities", "config", "details", "metadata", "provider",
        }:
            if _method_mapping_availability(value) is False:
                return False
        else:
            continue
        if _marker_denotes_unavailable(normalized):
            return False
    return True


def _method_mapping_availability(value: Any) -> bool | None:
    if isinstance(value, dict):
        recognized = False
        for raw_key, nested in value.items():
            key = _canonical_method_key(raw_key)
            if key in _METHOD_POSITIVE_AVAILABILITY_KEYS:
                recognized = True
                if _availability_scalar(nested) is False:
                    return False
            elif key in _METHOD_NEGATIVE_AVAILABILITY_KEYS:
                recognized = True
                if _availability_scalar(nested) is True:
                    return False
            elif key in {"status", "availability", "eligibility", "state"}:
                recognized = True
                if _availability_scalar(nested) is False:
                    return False
                if isinstance(nested, dict) and _method_mapping_availability(nested) is False:
                    return False
        return True if recognized else None
    return _availability_scalar(value)


def _method_label_is_available(value: Any) -> bool:
    return not _marker_denotes_unavailable(_normalized_method_marker(value))


def _published_payment_method_snapshot(
    payload: Any,
    *,
    include_custom_methods: bool = True,
) -> tuple[list[str], bool]:
    """Return normalized methods plus whether the response explicitly listed them.

    A container at the nearest level is authoritative, including an explicit
    empty list; stale/history branches are read only when no current-level
    container exists.
    """
    found: list[str] = []
    explicit = False

    def includes_container(key: str) -> bool:
        return include_custom_methods or key != "custom_payment_methods"

    def is_stale_snapshot_parent(key: str) -> bool:
        if key in _STALE_METHOD_SNAPSHOT_PARENT_KEYS:
            return True
        return key.startswith((
            "archived_",
            "historical_",
            "old_",
            "previous_",
            "prior_",
            "stale_",
        )) or key.endswith(("_history", "_histories"))

    def add(value: Any) -> None:
        if not _method_label_is_available(value):
            return
        normalized = _canonical_method_key(value)
        if normalized and normalized not in found and not normalized.startswith("cpmt_"):
            found.append(normalized)

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, str):
            add(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, depth + 1)
        elif isinstance(value, dict):
            if not _method_entry_is_available(value):
                return
            has_type = False
            for raw_key, candidate in value.items():
                key = _canonical_method_key(raw_key)
                if key not in _METHOD_TYPE_KEYS:
                    continue
                if isinstance(candidate, (str, int, float)):
                    has_type = True
                    add(candidate)
            if not has_type:
                for raw_key, nested in value.items():
                    key = _canonical_method_key(raw_key)
                    if key in _METHOD_TYPE_KEYS or key in _METHOD_CONTAINER_KEYS:
                        continue
                    decision = _method_mapping_availability(nested)
                    if decision is True:
                        add(raw_key)
            for raw_key, nested in value.items():
                key = _canonical_method_key(raw_key)
                if key in _METHOD_CONTAINER_KEYS and includes_container(key):
                    collect(nested, depth + 1)

    def collect_level(nodes: list[Any], depth: int) -> bool:
        level_explicit = False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for raw_key, nested in node.items():
                key = _canonical_method_key(raw_key)
                if key not in _METHOD_CONTAINER_KEYS or not includes_container(key):
                    continue
                if not isinstance(nested, (list, tuple, set, dict)):
                    continue
                level_explicit = True
                collect(nested, depth + 1)
        return level_explicit

    def child_nodes(value: Any) -> list[Any]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, (list, tuple)):
            return [item for item in value if isinstance(item, (dict, list, tuple))]
        return []

    frontier = child_nodes(payload)
    stale_roots: list[Any] = []
    depth = 0
    while frontier and depth <= 6:
        if collect_level(frontier, depth):
            explicit = True
            return found, explicit
        next_frontier: list[Any] = []
        for node in frontier:
            if isinstance(node, dict):
                for raw_key, nested in node.items():
                    key = _canonical_method_key(raw_key)
                    if key in _SAVED_METHOD_PARENT_KEYS or key in _METHOD_CONTAINER_KEYS:
                        continue
                    if not isinstance(nested, (dict, list, tuple)):
                        continue
                    if is_stale_snapshot_parent(key):
                        stale_roots.extend(child_nodes(nested))
                    else:
                        next_frontier.extend(child_nodes(nested))
            elif isinstance(node, (list, tuple)):
                next_frontier.extend(child_nodes(node))
        frontier = next_frontier
        depth += 1

    frontier = stale_roots
    depth = 0
    while frontier and depth <= 6:
        if collect_level(frontier, depth):
            explicit = True
            return found, explicit
        next_frontier = []
        for node in frontier:
            if isinstance(node, dict):
                for raw_key, nested in node.items():
                    key = _canonical_method_key(raw_key)
                    if key in _SAVED_METHOD_PARENT_KEYS or key in _METHOD_CONTAINER_KEYS:
                        continue
                    if isinstance(nested, (dict, list, tuple)):
                        next_frontier.extend(child_nodes(nested))
            elif isinstance(node, (list, tuple)):
                next_frontier.extend(child_nodes(node))
        frontier = next_frontier
        depth += 1

    return found, explicit


def oaics_stage_native_methods(current: Any, fallback: Any = None) -> list[str]:
    """Use a stage's explicit native methods before falling back to an earlier response.

    An explicitly present empty method list is meaningful: the server may have
    withdrawn a previously published payment method. In that case falling back
    to an earlier response would make a stale MoMo method look usable.
    """
    methods, current_declares = _published_payment_method_snapshot(
        current, include_custom_methods=False,
    )
    if current_declares:
        return methods
    methods_fallback, _ = _published_payment_method_snapshot(
        fallback or {}, include_custom_methods=False,
    )
    return methods_fallback


def oaics_custom_method_items(payload: dict[str, Any]) -> list[Any]:
    found: list[Any] = []
    seen: set[str] = set()
    for key in (
        "custom_payment_methods",
        "customPaymentMethods",
        "payment_methods",
        "paymentMethods",
    ):
        methods = payload.get(key)
        if not isinstance(methods, list):
            continue
        for method in methods:
            method_id = str(method.get("id") or "") if isinstance(method, dict) else ""
            marker = (
                f"id:{method_id}"
                if method_id
                else json.dumps(method, ensure_ascii=False, sort_keys=True, default=str)
            )
            if marker in seen:
                continue
            seen.add(marker)
            found.append(method)
    return found


def _momo_custom_methods_for(payload: dict[str, Any]) -> list[dict[str, Any]]:
    def values(value: Any, depth: int = 0) -> list[str]:
        if depth > 3:
            return []
        if isinstance(value, dict):
            result: list[str] = []
            for key in (
                "id", "type", "name", "label", "display_name", "provider",
                "payment_method_type", "paymentMethodType", "method_type",
                "custom_payment_method_type", "customPaymentMethodType",
            ):
                if key in value:
                    result.extend(values(value.get(key), depth + 1))
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                result.extend(values(item, depth + 1))
            return result
        text = str(value or "").strip().lower().replace("-", "_")
        return [text] if text else []

    matched: list[dict[str, Any]] = []
    for item in oaics_custom_method_items(payload):
        if not isinstance(item, dict) or not str(item.get("id") or "").startswith("cpmt_"):
            continue
        if "momo" in set(values(item)):
            matched.append(item)
            continue
        labels = {
            str(item.get(key) or "").strip().lower().replace("-", "_")
            for key in ("name", "label", "display_name")
        }
        if any("momo" in label.replace(" ", "_") for label in labels):
            matched.append(item)
    return matched


def _cpmt_descriptors_absent(method: dict[str, Any]) -> bool:
    descriptor_keys = (
        "type", "name", "label", "display_name", "displayName", "provider",
        "payment_method_type", "paymentMethodType", "method_type", "methodType",
        "custom_payment_method_type", "customPaymentMethodType",
    )
    return all(method.get(key) in (None, "", [], {}) for key in descriptor_keys)


def _custom_method_id_for(payload: dict[str, Any], *, allow_unlabelled_sole: bool) -> str:
    methods = [
        item for item in oaics_custom_method_items(payload)
        if isinstance(item, dict) and str(item.get("id") or "").startswith("cpmt_")
    ]
    matched = _momo_custom_methods_for(payload)
    if matched:
        return str(matched[0].get("id") or "")
    if allow_unlabelled_sole and len(methods) == 1 and _cpmt_descriptors_absent(methods[0]):
        return str(methods[0].get("id") or "")
    return ""


def _nested_cpmt_walk(value: Any, *, allow_unlabelled_sole: bool, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, dict):
        method_id = _custom_method_id_for(value, allow_unlabelled_sole=allow_unlabelled_sole)
        if method_id:
            return method_id
        for item in value.values():
            method_id = _nested_cpmt_walk(
                item, allow_unlabelled_sole=allow_unlabelled_sole, depth=depth + 1,
            )
            if method_id:
                return method_id
    elif isinstance(value, (list, tuple)):
        for item in value:
            method_id = _nested_cpmt_walk(
                item, allow_unlabelled_sole=allow_unlabelled_sole, depth=depth + 1,
            )
            if method_id:
                return method_id
    return ""


def oaics_stage_custom_method_id(current: Any, fallback: Any = None) -> str:
    """Do not preserve a cpmt when the current stage explicitly replaced its list."""
    _, current_declares = _published_payment_method_snapshot(current)
    current_method_id = _nested_cpmt_walk(current, allow_unlabelled_sole=False)
    if current_method_id or current_declares:
        return current_method_id
    return _nested_cpmt_walk(fallback or {}, allow_unlabelled_sole=False)


def momo_methods_diagnostic(payload: Any) -> str:
    out: list[str] = []
    for item in oaics_custom_method_items(payload if isinstance(payload, dict) else {}):
        if not isinstance(item, dict):
            continue
        method_id = str(item.get("id") or "")
        labels = [
            str(item.get(key) or "")
            for key in ("type", "name", "payment_method_type", "paymentMethodType")
            if item.get(key)
        ]
        out.append(": ".join(part for part in (method_id, "/".join(labels)) if part))
    return ", ".join(out) or "[]"


def momo_route_decision(current: Any, fallback: Any = None) -> dict[str, Any]:
    """Decide the native/cpmt/rebuild route from one OAICS state read."""
    native_methods = oaics_stage_native_methods(current, fallback)
    custom_method_id = oaics_stage_custom_method_id(current, fallback)
    if "momo" in native_methods:
        route = "native"
    elif custom_method_id:
        route = "cpmt"
    else:
        route = "rebuild"
    return {
        "route": route,
        "native_methods": native_methods,
        "custom_method_id": custom_method_id,
        "diagnostic": momo_methods_diagnostic(current if isinstance(current, dict) else {}),
    }


def momo_promotion_action(
    native_methods: list[str],
    custom_method_id: str,
    amount: Any,
    currency: str,
    promo_requested: bool,
    promo_on_create: bool = False,
) -> str:
    """Choose the next OAICS promotion step without mutating the checkout."""
    momo_ready = (
        "momo" in native_methods
        or str(custom_method_id or "").strip().startswith("cpmt_")
    )
    if not momo_ready:
        return "rebuild"
    if promo_requested and is_momo_promo_amount(amount, currency):
        return "already_discounted"
    if promo_requested and promo_on_create:
        return "rebuild_late"
    return "refresh" if promo_requested else "continue"


def fetch_custom_checkout_state(
    http: Any,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
) -> dict[str, Any]:
    response = http.get(
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{session_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
            "User-Agent": stripe_checkout.CHROME_UA,
            "OAI-Device-Id": device_id,
        },
        timeout=45,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Custom checkout read HTTP {response.status_code}: "
            f"{redact_payment_error(response.text or '')}"
        )
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def fetch_native_ready_state(
    http: Any,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    *,
    provider: str = "momo",
    preserve_from: Any = None,
    attempts: int = 6,
    delay_seconds: float = 0.8,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Poll OAICS until a native method such as ``momo`` is published."""
    wanted = str(provider or "").strip().lower().replace("-", "_")
    last: dict[str, Any] = {}
    total = max(1, int(attempts))
    for attempt in range(total):
        last = fetch_custom_checkout_state(http, token, session_id, processor_entity, device_id)
        methods = oaics_stage_native_methods(last, preserve_from)
        if wanted in methods:
            if attempt:
                log(f"OAICS native {provider} ready on read {attempt + 1}")
            return last
        if attempt + 1 < total:
            log(
                f"OAICS native {provider} not ready (read {attempt + 1}); "
                f"available={methods or []}"
            )
            time.sleep(max(0.0, float(delay_seconds)) * (attempt + 1))
    return last


def fetch_discounted_state(
    http: Any,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    initial_state: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.9,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Allow a create-time MoMo campaign a short window to settle."""
    state = dict(initial_state or {})
    fallback = dict(state)
    total = max(1, int(attempts))
    for attempt in range(total):
        methods = oaics_stage_native_methods(state, fallback)
        custom_method_id = oaics_stage_custom_method_id(state, fallback)
        amount = checkout_amount_minor(state)
        if amount is None:
            amount = checkout_amount_minor(fallback)
        currency = checkout_currency(state) or checkout_currency(fallback) or "VND"
        if (
            ("momo" in methods or custom_method_id)
            and is_momo_promo_amount(amount, currency)
        ):
            return state
        if attempt + 1 >= total:
            break
        log(
            f"MoMo create-time discount not settled (read {attempt + 1}/{total}): "
            f"available={methods or []}, amount={amount if amount is not None else '?'} {currency}"
        )
        time.sleep(max(0.0, float(delay_seconds)))
        state = fetch_custom_checkout_state(http, token, session_id, processor_entity, device_id)
        if not isinstance(state, dict):
            state = {}
    return state


def fetch_stable_state(
    http: Any,
    token: str,
    session_id: str,
    processor_entity: str,
    device_id: str,
    initial_state: dict[str, Any] | None = None,
    *,
    attempts: int = 3,
    delay_seconds: float = 0.9,
    log=lambda _message: None,
) -> dict[str, Any]:
    """Wait for two consecutive late-promotion OAICS snapshots to agree."""
    state = dict(initial_state or {})
    fallback = dict(state)
    previous_signature: tuple[Any, ...] | None = None
    total = max(2, int(attempts))
    for attempt in range(total):
        methods = tuple(oaics_stage_native_methods(state, fallback))
        method_id = oaics_stage_custom_method_id(state, fallback)
        amount = checkout_amount_minor(state)
        if amount is None:
            amount = checkout_amount_minor(fallback)
        currency = checkout_currency(state) or checkout_currency(fallback) or "VND"
        signature = (methods, method_id, amount, str(currency or "").upper())
        if previous_signature == signature:
            return state
        previous_signature = signature
        if attempt + 1 >= total:
            break
        log(
            f"MoMo method stability check (read {attempt + 1}/{total}): "
            f"available={list(methods)}, amount={amount if amount is not None else '?'} {currency}"
        )
        time.sleep(max(0.0, float(delay_seconds)))
        state = fetch_custom_checkout_state(http, token, session_id, processor_entity, device_id)
        if not isinstance(state, dict):
            state = {}
    raise RuntimeError(
        "MOMO_CHECKOUT_REBUILD_REQUIRED: late-promotion OAICS methods or amount "
        "kept changing inside the stability window; discard the session and rebuild"
    )


def create_oaics_confirmation_token(
    stripe_http: Any,
    publishable_key: str,
    payment_method_id: str,
) -> str:
    """Create the short-lived Stripe token consumed by OAICS checkout/confirm."""
    if not str(publishable_key or "").startswith("pk_"):
        raise RuntimeError("MOMO_OAICS_PUBLISHABLE_KEY_MISSING: OAICS returned no Stripe publishable key")
    if not str(payment_method_id or "").startswith("pm_"):
        raise RuntimeError("MOMO_OAICS_PAYMENT_METHOD_INVALID: MoMo did not return a pm_* id")
    response = stripe_http.post(
        f"{stripe_checkout.STRIPE_API}/v1/confirmation_tokens",
        data={
            "payment_method": payment_method_id,
            "key": publishable_key,
            "_stripe_version": stripe_checkout.STRIPE_VERSION_FULL,
        },
        headers=stripe_checkout._stripe_headers(),
        timeout=40,
    )
    text = response.text or ""
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"OAICS MoMo confirmation_token failed: HTTP "
            f"{getattr(response, 'status_code', '?')} {redact_payment_error(text)}"
        )
    try:
        token_id = str((response.json() or {}).get("id") or "")
    except Exception as exc:
        raise RuntimeError("OAICS MoMo confirmation_token returned non-JSON") from exc
    if not token_id.startswith("ctoken_"):
        raise RuntimeError("OAICS MoMo confirmation_token did not return a ctoken_* id")
    return token_id


def confirm_oaics_native_momo(
    http: Any,
    token: str,
    session_id: str,
    processor_entity: str,
    confirmation_token_id: str,
    device_id: str,
    did: str,
    sentinel_headers: dict[str, str],
    log=lambda _message: None,
) -> dict[str, Any]:
    """Confirm a native OAICS method using Stripe's ConfirmationToken contract."""
    common_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        "User-Agent": stripe_checkout.CHROME_UA,
        "OAI-Device-Id": device_id,
        "x-openai-target-path": "/backend-api/payments/checkout/confirm",
        "x-openai-target-route": "/backend-api/payments/checkout/confirm",
    }
    if sentinel_headers:
        ping_response = http.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                **common_headers,
                **sentinel_headers,
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=40,
        )
        if getattr(ping_response, "status_code", 0) >= 400:
            raise RuntimeError(
                "MOMO_OAICS_SENTINEL_PING_FAILED: sentinel ping before the native "
                f"MoMo confirm failed; HTTP {getattr(ping_response, 'status_code', '?')}"
            )
    response = http.post(
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
        json={
            "checkout_session_id": session_id,
            "selected_payment_method_type": "momo",
            "confirm_token": confirmation_token_id,
        },
        headers={
            **common_headers,
            "OAI-Device-Id": device_id,
            **(sentinel_headers or {}),
        },
        timeout=60,
    )
    text = response.text or ""
    payload: dict[str, Any] = {}
    json_error: Exception | None = None
    try:
        value = response.json() or {}
        payload = value if isinstance(value, dict) else {}
    except Exception as exc:  # noqa: BLE001 - classified after blocked detection
        json_error = exc
    if checkout_confirmation_is_blocked(payload, text):
        raise RuntimeError(
            f"MOMO_OAICS_CONFIRM_BLOCKED: native momo confirm for {session_id} was "
            "blocked; rebuild the whole checkout"
        )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"OAICS momo checkout/confirm failed: HTTP "
            f"{getattr(response, 'status_code', '?')} {redact_payment_error(text)}"
        )
    if json_error is not None:
        raise RuntimeError(
            f"OAICS momo checkout/confirm returned non-JSON: {redact_payment_error(text)}"
        ) from json_error
    log("OAICS native momo confirm accepted")
    return payload


def confirm_oaics_momo_intent(
    stripe_http: Any,
    publishable_key: str,
    payment_method_id: str,
    confirm_payload: dict[str, Any],
    session_id: str,
    processor_entity: str,
) -> dict[str, Any]:
    """Advance a returned OAICS SetupIntent when checkout/confirm has no action yet."""
    client_secret = nested_scalar(confirm_payload, ("client_secret", "clientSecret"))
    if "_secret_" not in client_secret:
        return {}
    intent_id = client_secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("seti_", "pi_")):
        return {}
    endpoint = "setup_intents" if intent_id.startswith("seti_") else "payment_intents"
    return_url = (
        "https://chatgpt.com/checkout/verify"
        f"?stripe_session_id={quote(session_id)}&processor_entity={quote(processor_entity)}&plan_type=plus"
    )
    response = stripe_http.post(
        f"{stripe_checkout.STRIPE_API}/v1/{endpoint}/{intent_id}/confirm",
        data={
            "client_secret": client_secret,
            "payment_method": payment_method_id,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "key": publishable_key,
            "_stripe_version": stripe_checkout.STRIPE_VERSION_FULL,
        },
        headers=stripe_checkout._stripe_headers(),
        timeout=60,
    )
    text = response.text or ""
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(
            f"OAICS MoMo {endpoint} confirm failed: HTTP "
            f"{getattr(response, 'status_code', '?')} {redact_payment_error(text)}"
        )
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise RuntimeError(f"OAICS MoMo {endpoint} confirm returned non-JSON") from exc
    return payload if isinstance(payload, dict) else {}


def poll_oaics_momo_intent(
    stripe_http: Any,
    publishable_key: str,
    *payloads: dict[str, Any],
    attempts: int = 6,
    delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """Poll the same OAICS MoMo Intent after an OpenAI approval response."""
    client_secret = ""
    for payload in payloads:
        client_secret = nested_scalar(payload, ("client_secret", "clientSecret"))
        if client_secret:
            break
    if "_secret_" not in client_secret:
        return {}
    intent_id = client_secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("seti_", "pi_")):
        return {}
    endpoint = "setup_intents" if intent_id.startswith("seti_") else "payment_intents"
    last: dict[str, Any] = {}
    for attempt in range(max(1, int(attempts))):
        response = stripe_http.get(
            f"{stripe_checkout.STRIPE_API}/v1/{endpoint}/{intent_id}",
            params={
                "client_secret": client_secret,
                "key": publishable_key,
                "_stripe_version": stripe_checkout.STRIPE_VERSION_FULL,
            },
            headers=stripe_checkout._stripe_headers(),
            timeout=40,
        )
        if getattr(response, "status_code", 0) != 200:
            return last
        try:
            last = response.json() or {}
        except Exception:  # noqa: BLE001 - tolerate a malformed poll response and retry
            last = {}
        action = momo_authorization_url(last)
        if action:
            return last
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(max(0.0, float(delay_seconds)))
    return last
