# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import ipaddress
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass


MAX_GMAIL_VARIANTS = 6
_RESERVED_ROUTED_SUFFIXES = (".test", ".invalid", ".example")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class GmailAliasError(ValueError):
    """Gmail gốc không hợp lệ để tạo biến thể."""


@dataclass(frozen=True)
class GmailAliasCandidate:
    email: str
    phase: str
    domain: str
    ordinal: int


@dataclass(frozen=True)
class GmailAliasPlan:
    configured_limit: int
    source_domain: str
    routed_domains: tuple[str, ...]
    candidates: tuple[GmailAliasCandidate, ...]

    @property
    def original_candidates(self) -> tuple[GmailAliasCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.phase == "original")

    @property
    def routed_candidates(self) -> tuple[GmailAliasCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.phase == "routed")


def _gmail_parts(email: str) -> tuple[str, str]:
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        raise GmailAliasError("Địa chỉ Gmail không hợp lệ")
    local, domain = value.split("@", 1)
    if domain not in {"gmail.com", "googlemail.com"}:
        raise GmailAliasError("Chỉ hỗ trợ địa chỉ gmail.com hoặc googlemail.com")
    local = local.split("+", 1)[0].replace(".", "")
    if not local or any(char not in string.ascii_lowercase + string.digits for char in local):
        raise GmailAliasError("Local-part Gmail không hợp lệ")
    return local, domain


def canonical_gmail(email: str) -> str:
    local, _ = _gmail_parts(email)
    return f"{local}@gmail.com"


def _dot_locals(local: str) -> list[str]:
    positions = list(range(1, len(local)))
    if not positions:
        return []
    preferred = [positions[0], positions[-1]]
    out = []
    for position in preferred:
        value = f"{local[:position]}.{local[position:]}"
        if value not in out:
            out.append(value)
    return out


def _plus_locals(local: str, count: int) -> list[str]:
    digest = hashlib.sha256(local.encode("utf-8")).hexdigest()
    return [f"{local}+{digest[index * 5:(index + 1) * 5]}" for index in range(count)]


def _variant_locals(local: str, count: int) -> list[str]:
    variants = [local]
    variants.extend(_dot_locals(local)[: min(2, max(0, count - 1))])
    variants.extend(_plus_locals(local, count - len(variants)))
    return variants[:count]


def _dot_variants(local: str) -> list[str]:
    return [f"{value}@gmail.com" for value in _dot_locals(local)]


def _plus_variants(local: str, count: int) -> list[str]:
    return [f"{value}@gmail.com" for value in _plus_locals(local, count)]


def generate_gmail_variants(email: str, limit: int = MAX_GMAIL_VARIANTS) -> list[str]:
    """Tạo Gmail gốc, tối đa hai biến thể dấu chấm rồi bù bằng alias +."""
    count = max(0, min(MAX_GMAIL_VARIANTS, int(limit)))
    if count == 0:
        return []
    canonical = canonical_gmail(email)
    local = canonical.split("@", 1)[0]
    return [f"{value}@gmail.com" for value in _variant_locals(local, count)]


def _normalize_domain(domain: str) -> str:
    value = str(domain or "").strip().lower().rstrip(".")
    if not value or "://" in value or any(char in value for char in "/:*@"):
        raise GmailAliasError("Domain routing Gmail không hợp lệ")
    if value == "localhost" or value.endswith(_RESERVED_ROUTED_SUFFIXES):
        raise GmailAliasError("Domain routing Gmail không được dùng domain test/invalid/example")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise GmailAliasError("Domain routing Gmail phải là tên miền, không phải IP")
    labels = value.split(".")
    if len(labels) < 2 or len(value) > 253 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise GmailAliasError("Domain routing Gmail không hợp lệ")
    return value


def normalize_routed_domains(
    domains: Sequence[str],
    source_domain: str | None = None,
) -> tuple[str, ...]:
    if isinstance(domains, (str, bytes)) or not isinstance(domains, Sequence):
        raise GmailAliasError("Domain routing Gmail phải được gửi dưới dạng danh sách")
    source = _normalize_domain(source_domain) if source_domain else None
    normalized: list[str] = []
    for domain in domains:
        value = _normalize_domain(domain)
        if value == source:
            raise GmailAliasError("Domain routing Gmail không được trùng domain mailbox gốc")
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > 2:
        raise GmailAliasError("Chỉ được nhập tối đa 2 domain routing Gmail")
    return tuple(normalized)


def build_gmail_alias_plan(
    email: str,
    limit: int = MAX_GMAIL_VARIANTS,
    routed_domains: Sequence[str] = (),
) -> GmailAliasPlan:
    count = max(0, min(MAX_GMAIL_VARIANTS, int(limit)))
    local, source_domain = _gmail_parts(email)
    domains = normalize_routed_domains(routed_domains, source_domain=source_domain)
    candidates: list[GmailAliasCandidate] = []
    local_variants = _variant_locals(local, count)
    for ordinal, local_part in enumerate(local_variants):
        candidates.append(GmailAliasCandidate(
            email=f"{local_part}@{source_domain}",
            phase="original",
            domain=source_domain,
            ordinal=ordinal,
        ))
    if domains:
        first_block = (count + 1) // 2 if len(domains) == 2 else count
        for ordinal, local_part in enumerate(local_variants):
            domain = domains[0] if ordinal < first_block else domains[1]
            candidates.append(GmailAliasCandidate(
                email=f"{local_part}@{domain}",
                phase="routed",
                domain=domain,
                ordinal=ordinal,
            ))
    return GmailAliasPlan(
        configured_limit=count,
        source_domain=source_domain,
        routed_domains=domains,
        candidates=tuple(candidates),
    )
