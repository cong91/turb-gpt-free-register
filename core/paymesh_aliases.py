"""Domain routing cho Paymesh MAIL card alias.

Paymesh provider mặc định sinh N alias `local+<5hex>@<domain-gốc>` từ mailbox
do CDK trả về. Module này bổ sung routed domain tùy chỉnh để test local: cùng
local-part nhưng thay domain, tạo thêm `local+<5hex>@<routed-domain>`.

Khác `core.gmail_aliases`, Paymesh routed cho phép `.test/.invalid/.example/
localhost` vì mục đích chính là test local chống giả mạo email; chỉ cấm IP
numeric và domain trùng mailbox gốc.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass

MAX_PAYMESH_ROUTED_DOMAINS = 2
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class PaymeshAliasError(ValueError):
    """Paymesh routed domain hoặc địa chỉ gốc không hợp lệ."""


@dataclass(frozen=True)
class PaymeshAliasCandidate:
    email: str
    phase: str
    domain: str
    ordinal: int


@dataclass(frozen=True)
class PaymeshAliasPlan:
    configured_limit: int
    source_domain: str
    routed_domains: tuple[str, ...]
    candidates: tuple[PaymeshAliasCandidate, ...]

    @property
    def original_candidates(self) -> tuple[PaymeshAliasCandidate, ...]:
        return tuple(c for c in self.candidates if c.phase == "original")

    @property
    def routed_candidates(self) -> tuple[PaymeshAliasCandidate, ...]:
        return tuple(c for c in self.candidates if c.phase == "routed")


def _split_email(email: str) -> tuple[str, str]:
    value = str(email or "").strip().lower()
    if value.count("@") != 1:
        raise PaymeshAliasError("Địa chỉ email Paymesh không hợp lệ")
    local, domain = value.split("@", 1)
    if not local or not domain:
        raise PaymeshAliasError("Địa chỉ email Paymesh không hợp lệ")
    return local, domain


def alias_suffix(email: str, index: int) -> str:
    """Hash 5 hex ký tự, dùng chung với `paymesh_mail_client._alias_variants`."""
    value = str(email or "").strip().lower()
    return hashlib.sha256(f"paymesh:{value}:{index}".encode()).hexdigest()[:5]


def _normalize_domain(domain: str) -> str:
    value = str(domain or "").strip().lower().rstrip(".")
    if not value or "://" in value or any(char in value for char in "/:*@"):
        raise PaymeshAliasError("Domain routing Paymesh không hợp lệ")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise PaymeshAliasError("Domain routing Paymesh phải là tên miền, không phải IP")
    # localhost và domain test được phép cho Paymesh (test local).
    labels = value.split(".")
    if len(labels) < 2 and value != "localhost":
        raise PaymeshAliasError("Domain routing Paymesh không hợp lệ")
    if len(value) > 253:
        raise PaymeshAliasError("Domain routing Paymesh quá dài")
    if value != "localhost" and any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise PaymeshAliasError("Domain routing Paymesh không hợp lệ")
    return value


def normalize_paymesh_routed_domains(
    domains: Sequence[str],
    source_domain: str | None = None,
) -> tuple[str, ...]:
    """Chuẩn hóa danh sách routed domain; cho phép test TLD/localhost.

    `source_domain` (domain mailbox gốc) nếu có sẽ bị loại trùng để tránh
    tạo alias trùng mailbox gốc.
    """
    if isinstance(domains, (str, bytes)) or not isinstance(domains, Sequence):
        raise PaymeshAliasError("Domain routing Paymesh phải được gửi dưới dạng danh sách")
    source = _normalize_domain(source_domain) if source_domain else None
    normalized: list[str] = []
    for domain in domains:
        value = _normalize_domain(domain)
        if source and value == source:
            raise PaymeshAliasError("Domain routing Paymesh không được trùng domain mailbox gốc")
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > MAX_PAYMESH_ROUTED_DOMAINS:
        raise PaymeshAliasError(
            f"Chỉ được nhập tối đa {MAX_PAYMESH_ROUTED_DOMAINS} domain routing Paymesh"
        )
    return tuple(normalized)


def build_paymesh_alias_plan(
    email: str,
    limit: int,
    routed_domains: Sequence[str] = (),
) -> PaymeshAliasPlan:
    """Danh sách alias Paymesh gồm N original + N*R routed.

    Original phase giữ nguyên `local+<5hex>@<source>` khớp với
    `paymesh_mail_client._alias_variants` để backward compat. Routed phase
    thay domain, giữ cùng local-part và cùng ordinal.
    """
    local, source_domain = _split_email(email)
    count = max(0, int(limit))
    domains = normalize_paymesh_routed_domains(routed_domains, source_domain=source_domain)
    candidates: list[PaymeshAliasCandidate] = []
    for ordinal in range(count):
        suffix = alias_suffix(email, ordinal)
        candidates.append(PaymeshAliasCandidate(
            email=f"{local}+{suffix}@{source_domain}",
            phase="original",
            domain=source_domain,
            ordinal=ordinal,
        ))
    for domain in domains:
        for ordinal in range(count):
            suffix = alias_suffix(email, ordinal)
            candidates.append(PaymeshAliasCandidate(
                email=f"{local}+{suffix}@{domain}",
                phase="routed",
                domain=domain,
                ordinal=ordinal,
            ))
    return PaymeshAliasPlan(
        configured_limit=count,
        source_domain=source_domain,
        routed_domains=domains,
        candidates=tuple(candidates),
    )
