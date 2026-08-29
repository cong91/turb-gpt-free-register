# -*- coding: utf-8 -*-
"""TinyHost temporary mailbox client."""
from __future__ import annotations

import json
import logging
import random
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)


class TinyHostError(RuntimeError):
    """TinyHost request or mailbox polling error."""


@dataclass
class TinyHostAccount:
    email: str
    domain: str
    user: str


_CONTEXT_CACHE: dict[str, TinyHostAccount] = {}
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$", re.IGNORECASE)
_UNSUPPORTED_DOMAINS: set[str] = set()
_SUPPORTED_DOMAINS: set[str] = set()
_TESTING_DOMAINS: set[str] = set()
_DOMAIN_HEALTH_LOADED = False
_DOMAIN_HEALTH_LOCK = threading.RLock()
_DOMAIN_HEALTH_PATH = Path(__file__).resolve().parents[1] / "data" / "tinyhost_domain_health.json"


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url() -> str:
    value = str(getattr(_email_cfg, "TINYHOST_API_BASE", "https://tinyhost.shop") or "").strip()
    if not value:
        raise TinyHostError("TinyHost API 地址未配置")
    return value.rstrip("/")


def _request_get(path: str, *, params: dict[str, int] | None = None):
    timeout = max(1, int(getattr(_email_cfg, "TINYHOST_REQUEST_TIMEOUT", 20) or 20))
    try:
        response = requests.get(
            f"{_base_url()}{path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise TinyHostError(f"TinyHost 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TinyHostError(
            f"TinyHost 响应不是 JSON ({path}): HTTP {response.status_code}"
        ) from exc

    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise TinyHostError(f"TinyHost 请求失败 ({path}): HTTP {response.status_code}; {detail}")
    return payload


def _normalize_domain(value: object) -> str:
    domain = str(value or "").strip().lower().lstrip("@").rstrip(".")
    if not _DOMAIN_RE.fullmatch(domain):
        return ""
    return domain


def _load_domain_health() -> None:
    global _DOMAIN_HEALTH_LOADED
    with _DOMAIN_HEALTH_LOCK:
        if _DOMAIN_HEALTH_LOADED:
            return
        try:
            payload = json.loads(_DOMAIN_HEALTH_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                unsupported = payload.get("unsupported_domains") or []
                supported = payload.get("supported_domains") or []
                if isinstance(unsupported, list):
                    _UNSUPPORTED_DOMAINS.update(
                        domain for domain in (_normalize_domain(item) for item in unsupported) if domain
                    )
                if isinstance(supported, list):
                    _SUPPORTED_DOMAINS.update(
                        domain
                        for domain in (_normalize_domain(item) for item in supported)
                        if domain and domain not in _UNSUPPORTED_DOMAINS
                    )
        except (OSError, ValueError, TypeError):
            pass
        _DOMAIN_HEALTH_LOADED = True


def _save_domain_health() -> None:
    with _DOMAIN_HEALTH_LOCK:
        try:
            _DOMAIN_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "supported_domains": sorted(_SUPPORTED_DOMAINS),
                "unsupported_domains": sorted(_UNSUPPORTED_DOMAINS),
            }
            _DOMAIN_HEALTH_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("[TinyHost] 保存 domain health 失败：%s", exc)


def _mark_domain_unsupported(domain: str) -> None:
    normalized = _normalize_domain(domain)
    if not normalized:
        return
    _load_domain_health()
    with _DOMAIN_HEALTH_LOCK:
        if normalized in _UNSUPPORTED_DOMAINS:
            return
        _UNSUPPORTED_DOMAINS.add(normalized)
        _save_domain_health()
    logger.warning("[TinyHost] domain bị ChatGPT từ chối ở about-you, đã disabled: %s", normalized)


def mark_domain_supported(email: str) -> bool:
    """Persist a domain after a registration reached the account save checkpoint."""
    domain = _normalize_domain(str(email or "").rsplit("@", 1)[-1])
    if not domain:
        return False
    _load_domain_health()
    with _DOMAIN_HEALTH_LOCK:
        if domain in _UNSUPPORTED_DOMAINS:
            return False
        _TESTING_DOMAINS.discard(domain)
        changed = domain not in _SUPPORTED_DOMAINS
        _SUPPORTED_DOMAINS.add(domain)
        if changed:
            _save_domain_health()
    logger.info("[TinyHost] domain 已通过 ChatGPT about-you 并保存为 supported: %s", domain)
    return True


def _filter_domains(raw_domains: object) -> list[str]:
    _load_domain_health()
    domains = []
    for item in raw_domains if isinstance(raw_domains, list) else []:
        domain = _normalize_domain(item)
        if domain and domain not in _UNSUPPORTED_DOMAINS and domain not in domains:
            domains.append(domain)
    return domains


def get_all_domains() -> list[str]:
    """Return every online TinyHost domain except domains rejected by ChatGPT."""
    payload = _request_get("/api/all-domains/")
    raw_domains = payload.get("domains") if isinstance(payload, dict) else None
    domains = _filter_domains(raw_domains)
    if not domains:
        raise TinyHostError("TinyHost 没有可用域名（全部 domain 已被拒绝或 API 返回为空）")
    return domains


def _random_local_part() -> str:
    length = int(getattr(_email_cfg, "TINYHOST_RANDOM_LOCAL_LENGTH", 12) or 12)
    length = max(6, min(32, length))
    alphabet = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(alphabet) for _ in range(length - 1)
    )


def create_account() -> TinyHostAccount:
    """Create a TinyHost address, testing each unclassified domain once first."""
    domains = get_all_domains()
    _load_domain_health()
    with _DOMAIN_HEALTH_LOCK:
        candidates = [
            domain for domain in domains
            if domain not in _SUPPORTED_DOMAINS and domain not in _TESTING_DOMAINS
        ]
        if not candidates:
            candidates = [domain for domain in domains if domain not in _TESTING_DOMAINS] or domains
        domain = random.choice(candidates)
        _TESTING_DOMAINS.add(domain)
    user = _random_local_part()
    account = TinyHostAccount(email=f"{user}@{domain}", domain=domain, user=user)
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[TinyHost] 已生成临时邮箱: %s", account.email)
    return account


def get_account_context(email: str) -> TinyHostAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """Drop local context; TinyHost has no documented delete-user endpoint."""
    account = _CONTEXT_CACHE.pop(_cache_key(email), None)
    domain = account.domain if account else str(email or "").rsplit("@", 1)[-1]
    normalized_domain = _normalize_domain(domain)
    if normalized_domain:
        with _DOMAIN_HEALTH_LOCK:
            _TESTING_DOMAINS.discard(normalized_domain)
    if str(status or "").strip().lower() == "disabled":
        _mark_domain_unsupported(domain)
    logger.info("[TinyHost] 已释放邮箱上下文: %s（status=%s, note=%s）", email, status, note or "")


def _timestamp(item: dict) -> float | None:
    raw = item.get("timestamp") or item.get("date")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
        return value / 1000 if value > 10_000_000_000 else value
    except (TypeError, ValueError):
        pass

    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _otp_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "from": item.get("sender") or item.get("from") or "",
        "subject": item.get("subject") or "",
        "text": item.get("body") or item.get("text") or "",
        "html": item.get("html_body") or item.get("html") or "",
    }


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """Poll a TinyHost inbox and return the newest OpenAI six-digit code."""
    target = str(email or "").strip()
    if not target:
        raise TinyHostError("TinyHost 取码缺少邮箱地址")
    account = get_account_context(target)
    if account is None:
        raise TinyHostError(f"TinyHost 邮箱上下文缺失: {target}")

    wait_seconds = int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 60))
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[TinyHost] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        try:
            payload = _request_get(
                f"/api/email/{quote(account.domain, safe='')}/{quote(account.user, safe='')}/",
                params={"page": 1, "limit": 20},
            )
            emails = payload.get("emails") if isinstance(payload, dict) else None
            if not isinstance(emails, list):
                raise TinyHostError("TinyHost 收件箱响应缺少 emails 数组")

            for mail in sorted(
                (item for item in emails if isinstance(item, dict)),
                key=lambda item: _timestamp(item) or float("-inf"),
                reverse=True,
            ):
                message_time = _timestamp(mail)
                if after_ts is not None and message_time is not None and message_time < after_ts - 30:
                    continue
                item = _otp_item(mail)
                if not looks_like_openai_email(item):
                    continue
                otp = extract_otp(item)
                if not otp:
                    continue

                candidate_time = float("-inf") if message_time is None else message_time
                if best_otp is None or candidate_time > best_timestamp or (
                    candidate_time == best_timestamp and otp != best_otp
                ):
                    best_otp = otp
                    best_timestamp = candidate_time
                    settle_until = time.monotonic() + settle
                    logger.info("[TinyHost] 锁定 OTP 候选，等待 %ss 确认", settle)

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return best_otp
        except TinyHostError as exc:
            last_error = str(exc)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise TinyHostError(f"等待 TinyHost 验证码超时: {target}; {last_error}")
