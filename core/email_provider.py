"""
邮箱来源调度层。

EMAIL_SOURCE 支持单个或多个来源：
    "outlook"
    "cloudflare_domain"   # 自有域名 + QQ IMAP
    "cloudflare"          # Cloudflare Worker 临时邮箱
    "generic_api"
    "gptmail"
    "mailnest"
    "cloudmail"
    "tinyhost"
    "outlook,generic_api,mailnest,cloudmail"          # 按顺序兜底
    ["outlook", "generic_api", "mailnest", "cloudmail"]  # 也兼容列表写法
"""
import logging
import re
from collections.abc import Iterable

from core.qan8_gmail_api_allocator import Qan8GmailApiAllocator

logger = logging.getLogger(__name__)
_BEFORE_CODE_UNSET = object()
_PROVIDER_602_RE = re.compile(
    r"(?:\bcode|\bstatus|\bhttp(?:\s+status)?|\berror)\s*[:=]?\s*602\b",
    re.IGNORECASE,
)


def _is_provider_code_602(value: object) -> bool:
    """Recognize provider 602 responses across API/error message formats."""
    return bool(_PROVIDER_602_RE.search(str(value or "")))


def _current_otp_job_id() -> int | None:
    """Return the active registration job ID for correlated OTP logs."""
    try:
        from core import registration_service as _registration_service

        value = getattr(_registration_service._THREAD_CTX, "job_id", None)
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _get_code_url_account(email: str, source: str):
    """Resolve a Gmail API URL account for either URL-backed provider."""
    if source in {"gmail_api_url", "qan8_gmail_api"}:
        from core.gmail_api_url_client import (
            get_account_context,
            get_batch_account_context,
        )

        job_id = _current_otp_job_id()
        batch_account = (
            get_batch_account_context(email, job_id=job_id)
            if job_id is not None
            else get_batch_account_context(email)
        )
        account = batch_account or get_account_context(email)
    else:
        return None
    if not account:
        raise ValueError(f"Gmail API URL account not found: {email}")
    return account


def _gmail_api_url_runtime_path():
    """Return the canonical Gmail batch database used by this process."""
    try:
        from core.gmail_api_url_client import _batch_store

        path = getattr(_batch_store(), "path", None)
        if path:
            return path
    except (AttributeError, ImportError, TypeError):
        pass
    from core.app_state_db import APP_STATE_DB_PATH

    return APP_STATE_DB_PATH


def _quarantine_code_url_after_provider_error(account, *, source: str, error: Exception) -> None:
    """Persist a terminal provider URL failure before a driver releases its alias."""
    if not _is_provider_code_602(error):
        return
    try:
        from core import registration_service

        registration_service.quarantine_provider_lane(
            job_id=_current_otp_job_id(),
            source=source,
            code_url=str(getattr(account, "code_url", "") or ""),
            provider_batch_id=getattr(account, "batch_id", None),
            provider_lane_id=getattr(account, "lane_id", None),
            reason=str(error),
        )
    except Exception:
        logger.exception(
            "[EmailProvider] Không thể quarantine provider source sau lỗi code=602: source=%s",
            source,
        )


def _raise_if_code_url_is_quarantined(account) -> None:
    """Prevent polling a Gmail root/alias that is disabled or retired."""
    from core import db

    code_url = str(getattr(account, "code_url", "") or "")
    email = str(getattr(account, "email", "") or "")
    runtime_path = _gmail_api_url_runtime_path()
    url_quarantined = db.is_gmail_api_url_code_url_failed(
        code_url,
        sqlite_path=runtime_path,
    )
    if url_quarantined:
        from core.gmail_api_url_client import GmailApiUrlError

        raise GmailApiUrlError("Provider error code=602: Gmail API URL source is quarantined")
    source_blocked = db.is_gmail_api_url_account_blocked(
        email,
        sqlite_path=runtime_path,
    )
    if not source_blocked:
        return
    from core.gmail_api_url_client import GmailApiUrlError

    raise GmailApiUrlError("Gmail API URL source is disabled or terminally retired")


def snapshot_verification_code(email: str, *, stage: str | None = None) -> str | None:
    """Capture a URL provider's current code before triggering a new OTP."""
    source = resolve_email_source(email)
    if source not in {"gmail_api_url", "qan8_gmail_api"}:
        return None

    from core.gmail_api_url_client import snapshot_verification_code as snapshot_code

    account = _get_code_url_account(email, source)
    try:
        _raise_if_code_url_is_quarantined(account)
        code = snapshot_code(account)
    except Exception as exc:
        _quarantine_code_url_after_provider_error(account, source=source, error=exc)
        raise
    logger.info(
        "[EmailProvider][OTP] pre-request snapshot source=%s present=%s job=%s stage=%s",
        source,
        bool(code),
        _current_otp_job_id() or "-",
        stage or "-",
    )
    return code


def acknowledge_verification_code(
    email: str,
    otp: str,
    *,
    stage: str | None = None,
) -> None:
    """Persist a URL provider OTP only after its remote validation succeeds."""
    source = resolve_email_source(email)
    if source not in {"gmail_api_url", "qan8_gmail_api"}:
        return

    from core.gmail_api_url_client import (
        acknowledge_verification_code as acknowledge_code,
    )

    account = _get_code_url_account(email, source)
    acknowledge_code(account, otp)
    logger.info(
        "[EmailProvider][OTP] validation acknowledged source=%s job=%s stage=%s",
        source,
        _current_otp_job_id() or "-",
        stage or "-",
    )

_VALID_SOURCES = (
    "outlook", "generic_api", "gmail_api_url", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "tinyhost",
    "gmail_123452026", "paymesh", "qan8_gmail_api", "remail",
)

_SOURCE_ALIASES = {
    "sms_paymesh": "paymesh",
    "sms.paymesh": "paymesh",
    "sms.paymesh.cn": "paymesh",
    "paymesh.cn": "paymesh",
    "mail": "paymesh",
}


def normalize_email_source(value: str) -> str:
    source = str(value or "").strip().strip('"\'')
    return _SOURCE_ALIASES.get(source, source)


def is_valid_email_source(value: str) -> bool:
    return normalize_email_source(value) in _VALID_SOURCES


def _normalize_explicit_email_source(value: str | None) -> str | None:
    """规范化调用方明确指定的邮箱来源。"""
    if value is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    for item in raw.replace(";", ",").replace("|", ",").split(","):
        source = str(item or "").strip().strip("\"'").lower()
        source = normalize_email_source(source)
        if source in _VALID_SOURCES:
            return source
    return None


def _canonicalize_runtime_source(source: str | None) -> str | None:
    """Map QAN8 provenance to the single Gmail API runtime provider."""
    normalized = _normalize_explicit_email_source(source)
    if normalized == "qan8_gmail_api":
        return "gmail_api_url"
    return normalized


def _registered_email_source(email: str) -> str | None:
    """读取已注册账号落库的邮箱来源。"""
    try:
        from core import db
        account = db.get_account_by_email(email)
    except Exception:  # noqa: BLE001
        return None
    return _canonicalize_runtime_source((account or {}).get("email_source"))


def _active_job_email_source(email: str) -> str | None:
    """优先使用当前注册 job 的来源，避免不同 provider 生成同名 alias 时串源。"""
    job_id = _current_otp_job_id()
    if job_id is None:
        return None
    try:
        from core import db

        job = db.get_job(job_id) or {}
        job_email = str(job.get("email") or "").strip().casefold()
        if job_email and job_email == str(email or "").strip().casefold():
            return _canonicalize_runtime_source(job.get("email_source"))
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def parse_email_sources(value=None) -> list[str]:
    """把 EMAIL_SOURCE 解析为有序来源列表，去重并过滤空值。"""
    if value is None:
        from config import email as _email_cfg
        value = _email_cfg.EMAIL_SOURCE
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]

    out: list[str] = []
    for item in raw:
        s = normalize_email_source(str(item or ""))
        if not s:
            continue
        if s not in _VALID_SOURCES:
            logger.warning(f"[EmailProvider] 未知邮箱来源 {s!r}，已忽略")
            continue
        if s not in out:
            out.append(s)
    return out or ["outlook"]


def _pick_from_source(
    source: str,
    job_id: int | str | None = None,
    gmail_cdks: list[str] | None = None,
    paymesh_cdks: list[str] | None = None,
    gmail_routed_domains: list[str] | None = None,
    gmail_batch_id: str | None = None,
    gmail_inventory_ids: list[str] | None = None,
    paymesh_inventory_id: str | None = None,
    paymesh_inventory_ids: list[str] | None = None,
    paymesh_routed_domains: list[str] | None = None,
    gmail_api_url_batch_id: str | None = None,
    qan8_gmail_api_batch_id: str | None = None,
    qan8_gmail_api_lane_id: int | None = None,
) -> str:
    if source == "gmail_123452026":
        if gmail_batch_id:
            from core.gmail_123452026_client import pick_account_by_batch
            return pick_account_by_batch(
                job_id=str(job_id or "standalone"),
                batch_id=gmail_batch_id,
                routed_domains=gmail_routed_domains or [],
            ).email
        if gmail_inventory_ids:
            from core.gmail_123452026_client import pick_account_by_inventory
            return pick_account_by_inventory(
                job_id=str(job_id or "standalone"),
                inventory_ids=gmail_inventory_ids,
                routed_domains=gmail_routed_domains or [],
            ).email
        from core.gmail_123452026_client import pick_account
        kwargs = {"job_id": str(job_id or "standalone"), "cdks": gmail_cdks or []}
        if gmail_routed_domains:
            kwargs["routed_domains"] = list(gmail_routed_domains)
        return pick_account(**kwargs).email
    if source == "paymesh":
        routed = paymesh_routed_domains
        if paymesh_inventory_id:
            from core.paymesh_mail_client import pick_account_for_inventory
            return pick_account_for_inventory(
                job_id=str(job_id or "standalone"),
                inventory_id=paymesh_inventory_id,
                routed_domains=routed or (),
            ).email
        if paymesh_inventory_ids:
            from core.paymesh_mail_client import pick_account_by_inventory
            return pick_account_by_inventory(
                job_id=str(job_id or "standalone"),
                inventory_ids=paymesh_inventory_ids,
                routed_domains=routed or (),
            ).email
        from core.paymesh_mail_client import pick_account
        return pick_account(
            job_id=str(job_id or "standalone"),
            cdks=paymesh_cdks or [],
            routed_domains=routed or (),
        ).email
    if source == "gptmail":
        from core.gptmail_client import pick_account
        return pick_account().email
    if source == "cloudflare":
        from core.cf_temp_mail_client import pick_account
        return pick_account().email
    if source == "tinyhost":
        from core.tinyhost_mail_client import create_account
        return create_account().email
    if source == "cloudflare_domain":
        from core.qqmail_client import pick_domain_email
        return pick_domain_email()
    if source == "generic_api":
        from core.generic_api_mail_client import pick_account
        return pick_account().email
    if source == "gmail_api_url":
        if gmail_api_url_batch_id:
            from core.gmail_api_url_client import get_email_from_batch
            return get_email_from_batch(
                batch_id=gmail_api_url_batch_id,
                job_id=str(job_id or "standalone"),
            ).email
        from core.gmail_api_url_client import pick_account
        return pick_account().email
    if source == "qan8_gmail_api":
        if (
            not qan8_gmail_api_batch_id
            or qan8_gmail_api_lane_id is None
            or not gmail_api_url_batch_id
        ):
            raise ValueError(
                "QAN8 Gmail API requires QAN8 batch/lane and canonical Gmail batch_id"
            )
        from core.registration_service import check_stop_requested

        return Qan8GmailApiAllocator().acquire_gmail_api_account(
            batch_id=qan8_gmail_api_batch_id,
            gmail_batch_id=gmail_api_url_batch_id,
            job_id=job_id or "standalone",
            lane_id=int(qan8_gmail_api_lane_id),
            stop_check=check_stop_requested,
        ).email
    if source == "mailnest":
        from core.mailnest_client import pick_account
        return pick_account().email
    if source == "cloudmail":
        from core.cloudmail_client import pick_account
        return pick_account().email
    if source == "remail":
        from core.remail_client import pick_account
        return pick_account().email
    from core.outlook_client import pick_account
    return pick_account().email


def acquire_email(
    job_id: int | str | None = None,
    gmail_cdks: list[str] | None = None,
    paymesh_cdks: list[str] | None = None,
    gmail_routed_domains: list[str] | None = None,
    gmail_batch_id: str | None = None,
    email_source: str | None = None,
    gmail_inventory_ids: list[str] | None = None,
    paymesh_inventory_id: str | None = None,
    paymesh_inventory_ids: list[str] | None = None,
    paymesh_routed_domains: list[str] | None = None,
    gmail_api_url_batch_id: str | None = None,
    qan8_gmail_api_batch_id: str | None = None,
    qan8_gmail_api_lane_id: int | None = None,
) -> str:
    """领取注册邮箱；显式 source 仅使用该 provider，否则按全局配置兜底。

    Supports both raw CDK lists (backward-compatible) and managed inventory IDs.
    """
    if email_source is not None:
        source = normalize_email_source(str(email_source or ""))
        if not is_valid_email_source(source):
            raise ValueError(f"未知邮箱来源: {source}")
        sources = [source]
    else:
        sources = parse_email_sources()
    if paymesh_routed_domains is None:
        try:
            from config import email as _email_cfg
            paymesh_routed_domains = list(getattr(_email_cfg, "PAYMESH_ROUTED_DOMAINS", []) or [])
        except (AttributeError, ImportError, TypeError):
            paymesh_routed_domains = []
    last_exc: Exception | None = None
    for source in sources:
        try:
            email = _pick_from_source(
                source, job_id=job_id,
                gmail_cdks=gmail_cdks, paymesh_cdks=paymesh_cdks,
                gmail_routed_domains=gmail_routed_domains,
                gmail_batch_id=gmail_batch_id,
                gmail_inventory_ids=gmail_inventory_ids,
                paymesh_inventory_id=paymesh_inventory_id,
                paymesh_inventory_ids=paymesh_inventory_ids,
                paymesh_routed_domains=paymesh_routed_domains,
                gmail_api_url_batch_id=gmail_api_url_batch_id,
                qan8_gmail_api_batch_id=qan8_gmail_api_batch_id,
                qan8_gmail_api_lane_id=qan8_gmail_api_lane_id,
            )
            logger.info(f"[EmailProvider] 使用邮箱来源: {source}, email={email}")
            return email
        except Exception as exc:  # noqa: BLE001 - try the next configured provider.
            last_exc = exc
            logger.warning(f"[EmailProvider] 来源 {source} 领取邮箱失败: {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(f"所有邮箱来源均领取失败: {sources}; last={last_exc}")


def acquire_email_after_input(email: str | None = None) -> str:
    """在浏览器已找到邮箱输入框后领取邮箱。

    浏览器驱动把“找到输入框”和“领取邮箱”拆成两个阶段，避免页面加载、风控
    或入口识别失败时提前消耗邮箱。传入已有邮箱时不重复领取，兼容固定邮箱模式。
    """
    current = str(email or "").strip()
    if current:
        return current

    from config import email as _email_cfg

    if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", False)):
        raise RuntimeError("页面已找到邮箱输入框，但自动取邮箱未启用且未配置 REGISTER_EMAIL")
    allocated = str(acquire_email() or "").strip()
    if not allocated:
        raise RuntimeError("邮箱服务返回了空邮箱地址")
    logger.info("[EmailProvider] 已找到邮箱输入框，开始分配邮箱: %s", allocated)
    return allocated


def resolve_email_source(email: str) -> str:
    """根据邮箱判断实际来源，已注册账号优先使用落库来源。"""
    registered_source = _registered_email_source(email)
    if registered_source:
        return registered_source
    active_job_source = _active_job_email_source(email)
    if active_job_source:
        return active_job_source
    # Canonical Gmail API inventory wins before the other provider contexts.
    # QAN8 aliases are materialized here too, so no second runtime source can
    # steal an alias that the Gmail ledger already owns.
    from core import db
    if db.get_gmail_api_url_email_by_email(email):
        return "gmail_api_url"
    from core.gmail_api_url_client import get_batch_account_context
    if get_batch_account_context(email):
        return "gmail_api_url"
    from core.gmail_123452026_client import get_account_context as get_gmail_cdk_context
    if get_gmail_cdk_context(email):
        return "gmail_123452026"
    from core.paymesh_mail_client import get_account_context as get_paymesh_context
    if get_paymesh_context(email):
        return "paymesh"
    from core.gptmail_client import get_account_context as get_gptmail_context
    if get_gptmail_context(email):
        return "gptmail"
    from core.cf_temp_mail_client import get_account_context as get_cf_context
    if get_cf_context(email):
        return "cloudflare"
    from core.tinyhost_mail_client import get_account_context as get_tinyhost_context
    if get_tinyhost_context(email):
        return "tinyhost"
    from core.mailnest_client import get_account_context as get_mailnest_context
    if get_mailnest_context(email):
        return "mailnest"
    from core.cloudmail_client import get_account_context as get_cloudmail_context
    if get_cloudmail_context(email):
        return "cloudmail"
    from core.remail_client import get_account_context as get_remail_context
    if get_remail_context(email):
        return "remail"

    if db.get_generic_api_email_by_email(email):
        return "generic_api"
    if db.get_outlook_by_email(email):
        return "outlook"
    if db._find_domain_email(db._load_domain_pool(), email):  # 内部轻量查询，仅本项目使用
        return "cloudflare_domain"
    # 兜底：如果域名匹配 EMAIL_DOMAIN，则按域名邮箱处理
    try:
        from config import email as _email_cfg
        domain = (_email_cfg.EMAIL_DOMAIN or "").lower().strip()
        if domain and domain != "-" and email.lower().endswith("@" + domain):
            return "cloudflare_domain"
    except (AttributeError, TypeError, ValueError):
        logger.debug("[EmailProvider] EMAIL_DOMAIN fallback unavailable", exc_info=True)
    return parse_email_sources()[0]


def otp_max_wait_for_source(source: str, max_wait: int | None = None) -> int:
    """返回邮箱来源对应的 OTP 等待上限；显式值始终优先。"""
    if max_wait is not None:
        return int(max_wait)
    from config import email as _email_cfg

    if str(source or "").strip().lower() == "paymesh":
        return int(getattr(_email_cfg, "PAYMESH_OTP_MAX_WAIT", 60) or 60)
    return int(getattr(_email_cfg, "OTP_MAX_WAIT", 60) or 60)


def _wait_for_code_url_otp(
    account,
    *,
    source: str,
    after_ts: float,
    max_wait: int | None,
    poll_interval: int | None,
    before_code: str | None | object,
    stage: str | None,
) -> str:
    """Poll one mailbox code URL with the shared Gmail API stale guard.

    QAN8 aliases and imported Gmail API URL records are different allocation
    records, but both resolve to the same ``email----code_url`` mailbox
    contract. Normalize both account types before polling so OTP persistence
    is always keyed by the original mailbox URL, never by an alias.
    """
    from config import email as _email_cfg
    from core.gmail_api_url_client import (
        GmailApiUrlAccount,
        GmailApiUrlError,
        poll_verification_code,
    )

    normalized_account = GmailApiUrlAccount(
        email=str(account.email),
        code_url=str(account.code_url),
    )
    configured_interval = int(getattr(_email_cfg, "OTP_POLL_INTERVAL", 2) or 2)
    poll_kwargs = {
        "max_wait": max_wait if max_wait is not None else otp_max_wait_for_source(source),
        "poll_interval": poll_interval if poll_interval is not None else configured_interval,
        "after_ts": after_ts,
        "job_id": _current_otp_job_id(),
        "stage": stage,
    }
    if before_code is not _BEFORE_CODE_UNSET:
        poll_kwargs["before_code"] = before_code
    try:
        _raise_if_code_url_is_quarantined(account)
        return poll_verification_code(normalized_account, **poll_kwargs)
    except GmailApiUrlError as exc:
        _quarantine_code_url_after_provider_error(account, source=source, error=exc)
        raise


def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    before_code: str | None | object = _BEFORE_CODE_UNSET,
    stage: str | None = None,
    email_source: str | None = None,
) -> str:
    """等待并返回该邮箱最新的 ChatGPT OTP（6 位数字字符串）。

    ``before_code`` 用于重发后的 stale guard；Gmail API URL 与 QAN8
    provider 都按共享的原始 mailbox ``code_url`` 处理，其他 provider
    保持原有参数行为。
    ``stage`` 仅用于关联并发任务的 OTP 日志。

    USE_EMAIL_SERVICE=False 时走手动验证码通道（WebUI 提交 / CLI 输入），
    不再强制要求 Outlook clientId/refreshToken。
    """
    try:
        from config import email as _email_cfg
        use_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
    except (AttributeError, ImportError, TypeError):
        use_service = True

    if not use_service:
        from config import email as _email_cfg
        from core.manual_otp import wait_for_manual_otp
        timeout = int(max_wait if max_wait is not None else (getattr(_email_cfg, "OTP_MAX_WAIT", 60) or 60))
        job_id = None
        try:
            from core import registration_service as svc
            job_id = getattr(svc._THREAD_CTX, "job_id", None)
        except (AttributeError, ImportError, TypeError, ValueError):
            job_id = None
        return wait_for_manual_otp(email, timeout=timeout, job_id=job_id)

    # Resolve through the single provider resolver so registered-account,
    # active-job, canonical Gmail inventory, and QAN8 provenance all follow
    # the same precedence rules.
    source = _canonicalize_runtime_source(email_source) or _canonicalize_runtime_source(
        resolve_email_source(email)
    )
    extra_kwargs = {}
    if max_wait is not None or source == "paymesh":
        extra_kwargs["max_wait"] = otp_max_wait_for_source(source, max_wait)
    if poll_interval is not None:
        extra_kwargs["poll_interval"] = poll_interval
    if settle_seconds is not None:
        extra_kwargs["settle_seconds"] = settle_seconds

    if source == "gmail_123452026":
        from core.gmail_123452026_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "paymesh":
        from core.paymesh_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "gptmail":
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare":
        from core.cf_temp_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "tinyhost":
        from core.tinyhost_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudflare_domain":
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "generic_api":
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "gmail_api_url":
        # Email gốc trong pool trước; nếu là alias batch thì tra batch context
        # (mọi alias share code_url của email gốc).
        account = _get_code_url_account(email, source)
        return _wait_for_code_url_otp(
            account,
            source=source,
            after_ts=after_ts,
            max_wait=extra_kwargs.get("max_wait"),
            poll_interval=extra_kwargs.get("poll_interval"),
            before_code=before_code,
            stage=stage,
        )
    if source == "mailnest":
        from core.mailnest_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "cloudmail":
        from core.cloudmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    if source == "remail":
        from core.remail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)
    from core.outlook_client import fetch_latest_otp
    return fetch_latest_otp(email, after_ts=after_ts, **extra_kwargs)


def release_email(email: str, status: str = "available", note: str | None = None) -> str:
    """按邮箱实际来源回收状态，返回来源名。"""
    resolved_source = resolve_email_source(email)
    source = _canonicalize_runtime_source(resolved_source) or resolved_source
    provider_failed = _is_provider_code_602(note)
    if source == "gmail_api_url" and provider_failed:
        try:
            account = _get_code_url_account(email, source)
            _quarantine_code_url_after_provider_error(
                account,
                source=source,
                error=RuntimeError(str(note or "Provider error code=602")),
            )
        except Exception:
            logger.exception(
                "[EmailProvider] Không thể resolve Gmail API URL source khi release lỗi code=602"
            )
    if source == "gmail_api_url" and provider_failed:
        status = "failed"
    if source == "gmail_123452026":
        from core.gmail_123452026_client import release_account
        release_account(email, status=status, note=note)
    elif source == "paymesh":
        from core.paymesh_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "gptmail":
        from core.gptmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare":
        from core.cf_temp_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "tinyhost":
        from core.tinyhost_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudflare_domain":
        from core.qqmail_client import release_domain_email
        release_domain_email(email, status=status, note=note)
    elif source == "generic_api":
        from core.generic_api_mail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "gmail_api_url":
        from core.gmail_api_url_client import release_account
        release_kwargs = {"status": status, "note": note}
        job_id = _current_otp_job_id()
        if job_id is not None:
            release_kwargs["job_id"] = job_id
        release_account(email, **release_kwargs)
    elif source == "mailnest":
        from core.mailnest_client import release_account
        release_account(email, status=status, note=note)
    elif source == "cloudmail":
        from core.cloudmail_client import release_account
        release_account(email, status=status, note=note)
    elif source == "remail":
        from core.remail_client import release_account
        release_account(email, status=status, note=note)
    else:
        from core.outlook_client import release_account
        release_account(email, status=status, note=note)
    return source


def release_email_if_unconsumed(
    email: str,
    note: str | None = None,
    *,
    discard_on_failure: bool = False,
) -> bool:
    """回收未消耗的任务邮箱；注册失败时废弃 Gmail API/QAN8 alias。"""
    if not (email or "").strip():
        return False

    resolved_source = resolve_email_source(email)
    source = _canonicalize_runtime_source(resolved_source) or resolved_source
    from core import db

    if source == "gmail_123452026":
        from core.gmail_123452026_client import release_account
        changed = release_account(email, status="available", note=note)
    elif source == "paymesh":
        from core.paymesh_mail_client import release_account
        changed = release_account(email, status="available", note=note)
    elif source == "outlook":
        changed = db.release_unconsumed_outlook(email, note=note)
    elif source == "generic_api":
        changed = db.release_unconsumed_generic_api_email(email, note=note)
    elif source == "gmail_api_url":
        from core.gmail_api_url_client import get_batch_account_context, release_account

        job_id = _current_otp_job_id()
        batch_context = (
            get_batch_account_context(email, job_id=job_id)
            if job_id is not None
            else get_batch_account_context(email)
        )
        provider_failed = _is_provider_code_602(note)
        if batch_context:
            release_kwargs = {
                "status": "failed" if discard_on_failure or provider_failed else "available",
                "note": note or "",
            }
            if job_id is not None:
                release_kwargs["job_id"] = job_id
            changed = release_account(email, **release_kwargs)
            if provider_failed:
                _quarantine_code_url_after_provider_error(
                    batch_context,
                    source=source,
                    error=RuntimeError(str(note or "Provider error code=602")),
                )
                changed = True
            if (discard_on_failure or provider_failed) and changed:
                logger.warning(
                    "[EmailProvider] Đã loại bỏ Gmail API alias sau lỗi đăng ký: %s",
                    email,
                )
            return bool(changed)

        if provider_failed:
            existing = db.get_gmail_api_url_email_by_email(email)
            if existing is not None:
                db.fail_gmail_api_url_sources_for_code_url(
                    str(existing.get("code_url") or ""),
                    note=note,
                )
            changed = existing is not None
        else:
            changed = db.release_unconsumed_gmail_api_url_email(email, note=note)
    elif source == "cloudflare_domain":
        changed = db.release_unconsumed_domain_email(email, note=note)
    else:
        # 临时邮箱不重新进入本地池，只清理进程上下文；已有本地账号时保留上下文。
        if db.get_account_by_email(email) is not None:
            return False
        release_email(email, status="available", note=note)
        changed = True

    if changed:
        logger.info("[EmailProvider] 已回收未消耗邮箱: source=%s, email=%s", source, email)
    return changed


def mark_email_consumed(email: str) -> bool:
    """提交需要显式消费的 provider reservation；其他来源无需处理。"""
    if not (email or "").strip():
        return False
    resolved_source = resolve_email_source(email)
    source = _canonicalize_runtime_source(resolved_source) or resolved_source
    if source == "gmail_123452026":
        from core.gmail_123452026_client import mark_account_consumed
        return mark_account_consumed(email)
    if source == "paymesh":
        from core.paymesh_mail_client import mark_account_consumed
        return mark_account_consumed(email)
    if source == "gmail_api_url":
        from core.gmail_api_url_client import release_account
        job_id = _current_otp_job_id()
        release_kwargs = {"status": "used", "note": ""}
        if job_id is not None:
            release_kwargs["job_id"] = job_id
        return bool(release_account(email, **release_kwargs))
    if source == "tinyhost":
        from core.tinyhost_mail_client import mark_domain_supported
        return mark_domain_supported(email)
    return False
