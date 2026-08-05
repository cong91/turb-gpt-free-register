# -*- coding: utf-8 -*-
"""Deterministic aliases for reserved test domains."""
from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Sequence

MAX_RESERVED_TEST_ALIASES = 200
MAX_RESERVED_TEST_DOMAINS = 2
MAX_RESERVED_TEST_BASE_LENGTH = 32
_RESERVED_TEST_TLDS = (".example", ".invalid", ".test")
_DOMAIN_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_BASE_PATTERN = re.compile(r"[a-z0-9]+")


class ReservedTestAliasError(ValueError):
    """Raised when reserved test alias input is invalid."""


def _normalize_domains(domains: Sequence[str]) -> list[str]:
    if isinstance(domains, (str, bytes)):
        raise ReservedTestAliasError("Domain test phải được gửi dưới dạng danh sách")

    normalized: list[str] = []
    for raw_domain in domains:
        domain = str(raw_domain or "").strip().lower().rstrip(".")
        if domain not in normalized:
            normalized.append(domain)

    if not normalized:
        raise ReservedTestAliasError("Hãy nhập ít nhất một domain test")
    if len(normalized) > MAX_RESERVED_TEST_DOMAINS:
        raise ReservedTestAliasError("Chỉ được nhập tối đa hai domain test")
    return normalized


def _normalize_limit(limit: int) -> int:
    try:
        count = int(limit)
    except (TypeError, ValueError) as exc:
        raise ReservedTestAliasError("Số alias phải là số nguyên") from exc
    if not 1 <= count <= MAX_RESERVED_TEST_ALIASES:
        raise ReservedTestAliasError(
            f"Số alias phải từ 1 đến {MAX_RESERVED_TEST_ALIASES}"
        )
    return count


def generate_reserved_test_aliases(
    base: str,
    domains: Sequence[str],
    limit: int = 6,
) -> list[str]:
    """Generate deterministic fake mailbox aliases for reserved test domains."""
    normalized_domains = _normalize_domains(domains)
    value = str(base or "").strip().lower()
    if not _BASE_PATTERN.fullmatch(value):
        raise ReservedTestAliasError("Base chỉ được chứa chữ cái thường và chữ số")
    if len(value) > MAX_RESERVED_TEST_BASE_LENGTH:
        raise ReservedTestAliasError(
            f"Base không được dài quá {MAX_RESERVED_TEST_BASE_LENGTH} ký tự"
        )
    count = _normalize_limit(limit)

    gaps = list(range(1, len(value)))
    seed = hashlib.sha256(f"reserved-test-dot:{value}".encode("utf-8")).digest()
    ordered_gaps = sorted(gaps, key=lambda gap: (seed[gap % len(seed)], gap)) if gaps else []
    forms = [value]
    for size in range(1, len(value)):
        for combo in itertools.combinations(ordered_gaps, size):
            selected = set(combo)
            forms.append(
                "".join(
                    ("." if index in selected else "") + char
                    for index, char in enumerate(value)
                )
            )
            if len(forms) * len(normalized_domains) >= count:
                break
        if len(forms) * len(normalized_domains) >= count:
            break

    plus_index = 0
    while len(forms) * len(normalized_domains) < count:
        tag = hashlib.sha256(
            f"reserved-test-plus:{value}:{plus_index}".encode("utf-8")
        ).hexdigest()[:10]
        forms.append(f"{value}+{tag}")
        plus_index += 1

    aliases: list[str] = []
    for form in forms:
        for domain in normalized_domains:
            aliases.append(f"{form}@{domain}")
            if len(aliases) >= count:
                return aliases
    return aliases
