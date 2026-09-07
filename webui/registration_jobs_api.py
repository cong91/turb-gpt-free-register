from __future__ import annotations

from urllib.parse import urlparse

from config import email as email_config
from config import register as register_config
from core.email_provider import (
    is_valid_email_source,
    normalize_email_source,
    parse_email_sources,
)
from core.gmail_aliases import GmailAliasError, normalize_routed_domains
from core.gmail_batch_store_base import GmailBatchError
from core.paymesh_aliases import PaymeshAliasError, normalize_paymesh_routed_domains
from core.registration_limits import MAX_REGISTRATION_TASKS
from webui.email_source_validation import validate_email_sources


def _normalize_cdks(value) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        raw = value
    else:
        raw = []

    cdks: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cdk = str(item or "").strip()
        if cdk and cdk not in seen:
            seen.add(cdk)
            cdks.append(cdk)
    return cdks


def _provider_error(
    sources: list[str],
    gmail_cdks: list[str],
    paymesh_cdks: list[str],
    gmail_routed_domains: list[str],
    paymesh_routed_domains: list[str] | None = None,
) -> str | None:
    source_error = validate_email_sources(
        sources,
        email_config,
        gmail_cdks=gmail_cdks,
        paymesh_cdks=paymesh_cdks,
        gmail_routed_domains=gmail_routed_domains,
        paymesh_routed_domains=paymesh_routed_domains,
    )
    if source_error:
        return source_error
    if "gptmail" in sources and not str(getattr(email_config, "GPTMAIL_API_KEY", "") or "").strip():
        return "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。"
    if "cloudflare" in sources:
        api_base = str(getattr(email_config, "CLOUDFLARE_API_BASE", "") or "").strip()
        if not api_base:
            return "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。"
        auth_mode = str(getattr(email_config, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
        accounts_path = str(
            getattr(email_config, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or ""
        ).strip().lower()
        api_key = str(getattr(email_config, "CLOUDFLARE_API_KEY", "") or "").strip()
        needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
        if needs_key and not api_key:
            return "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。"
    if "mailnest" in sources:
        if not str(getattr(email_config, "MAIL_NEST_API_KEY", "") or "").strip():
            return "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。"
        if not str(getattr(email_config, "MAIL_NEST_PROJECT_CODE", "") or "").strip():
            return "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。"
    if "cloudmail" in sources:
        if not str(getattr(email_config, "CLOUDMAIL_API_BASE", "") or "").strip():
            return "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。"
        if not str(getattr(email_config, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip():
            return "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。"
    if "remail" in sources:
        api_base = str(getattr(email_config, "REMAIL_API_BASE", "") or "").strip()
        api_key = str(getattr(email_config, "REMAIL_API_KEY", "") or "").strip()
        try:
            project_id = int(getattr(email_config, "REMAIL_PROJECT_ID", 2) or 0)
        except (TypeError, ValueError):
            project_id = 0
        suffix = str(getattr(email_config, "REMAIL_EMAIL_SUFFIX", "") or "").strip()
        service_mode = str(getattr(email_config, "REMAIL_SERVICE_MODE", "purchase") or "purchase").strip().lower()
        if not api_base:
            return "已选择 remail 邮箱来源，请填写 Remail API 地址（配置 → 邮箱 / OTP）。"
        if not api_key:
            return "已选择 remail 邮箱来源，请填写 Remail API Key（配置 → 邮箱 / OTP）。"
        if project_id <= 0:
            return "已选择 remail 邮箱来源，请填写 Remail 项目 ID（配置 → 邮箱 / OTP）。"
        if not suffix:
            return "已选择 remail 邮箱来源，请填写 Remail 邮箱后缀（例如 outlook.com）。"
        if service_mode not in ("code", "purchase"):
            return "Remail 服务模式只能填写 code 或 purchase（配置 → 邮箱 / OTP）。"
    if "tinyhost" in sources and not str(getattr(email_config, "TINYHOST_API_BASE", "") or "").strip():
        return "已选择 tinyhost 邮箱来源，请填写 TinyHost API 地址（配置 → 邮箱 / OTP）。"
    return None


def _pool_warning(
    database,
    sources: list[str],
    count: int,
    *,
    gmail_api_url_aliases_per_email: int = 12,
) -> str:
    if any(source in sources for source in (
        "gptmail", "mailnest", "cloudmail", "tinyhost", "cloudflare", "gmail_123452026", "paymesh", "remail",
    )):
        return ""
    if sources == ["gmail_api_url"]:
        # Gmail API URL replenishes through the purchase adapter when the
        # imported canonical aliases are exhausted. A local shortage is not a
        # warning; it is only an error when the purchase adapter is unconfigured.
        return ""
    if "cloudflare_domain" in sources:
        pool = database.domain_email_pool_summary()
        if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
            return f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        return ""
    if sources == ["generic_api"]:
        available = database.generic_api_email_pool_summary().get("available", 0)
        return f"通用 API 邮箱池仅 {available} 个可用，少于任务数 {count}，不足的会失败" if available < count else ""
    if sources == ["imap"]:
        available = database.imap_email_pool_summary().get("available", 0)
        return f"通用 IMAP 邮箱池仅 {available} 个可用，少于任务数 {count}，不足的会失败" if available < count else ""
    if len(sources) > 1:
        available = 0
        if "outlook" in sources:
            available += database.outlook_pool_summary().get("available", 0)
        if "generic_api" in sources:
            available += database.generic_api_email_pool_summary().get("available", 0)
        if "imap" in sources:
            available += database.imap_email_pool_summary().get("available", 0)
        return f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败" if available < count else ""
    available = database.outlook_pool_summary().get("available", 0)
    return f"可用邮箱仅 {available} 个，少于任务数 {count}，不足的会失败" if available < count else ""


def _qan8_purchase_config_error() -> str | None:
    """Validate the internal Gmail source purchase adapter when it is needed."""
    api_base = str(getattr(email_config, "QAN8_API_BASE", "") or "").strip()
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "Kho Gmail API URL không đủ alias và địa chỉ shop.qan8.com chưa hợp lệ."
    if not str(getattr(email_config, "QAN8_API_KEY", "") or "").strip():
        return "Kho Gmail API URL không đủ alias, hãy cấu hình QAN8 API Key để mua bổ sung."
    if not str(getattr(email_config, "QAN8_GMAIL_SKU_ID", "") or "").strip():
        return "Kho Gmail API URL không đủ alias, hãy cấu hình SKU Gmail của shop.qan8.com để mua bổ sung."
    return None


def create_registration_jobs(
    data: dict,
    *,
    service,
    database,
    automation_context: dict | None = None,
) -> tuple[dict, int]:
    gmail_cdks = _normalize_cdks(data.get("gmail_cdks"))
    paymesh_cdks = _normalize_cdks(data.get("paymesh_cdks"))
    raw_routed_domains = data.get("gmail_routed_domains", [])
    try:
        gmail_routed_domains = list(normalize_routed_domains(raw_routed_domains))
    except GmailAliasError as exc:
        return {"ok": False, "error": str(exc)}, 400
    raw_paymesh_routed = data.get("paymesh_routed_domains")
    if raw_paymesh_routed is None:
        raw_paymesh_routed = list(getattr(email_config, "PAYMESH_ROUTED_DOMAINS", []) or [])
    if isinstance(raw_paymesh_routed, str):
        raw_paymesh_routed = [
            part.strip() for part in raw_paymesh_routed.replace(",", "\n").splitlines()
            if part.strip()
        ]
    try:
        paymesh_routed_domains = list(normalize_paymesh_routed_domains(raw_paymesh_routed))
    except PaymeshAliasError as exc:
        return {"ok": False, "error": str(exc)}, 400
    raw_requested_source = str(data.get("email_source") or "").strip().strip('"\'').lower()
    requested_source = normalize_email_source(raw_requested_source) or None
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "count 非法"}, 400
    if count < 1 or count > MAX_REGISTRATION_TASKS:
        return {"ok": False, "error": f"count 需在 1~{MAX_REGISTRATION_TASKS} 之间"}, 400
    try:
        workers = max(1, min(16, int(data.get("workers", 3))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "workers 非法"}, 400
    if raw_requested_source and not is_valid_email_source(raw_requested_source):
        return {"ok": False, "error": "邮箱来源不支持"}, 400

    if not bool(getattr(email_config, "USE_EMAIL_SERVICE", True)):
        reg_email = str(getattr(register_config, "REGISTER_EMAIL", "") or "").strip()
        if not reg_email:
            return {"ok": False, "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。"}, 400
        if count > 1:
            return {"ok": False, "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。"}, 400
        jobs = service.submit_registration(
            count=count,
            workers=workers,
            automation_context=automation_context,
        )
        return {
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
            "workers": workers,
        }, 200

    sources = [requested_source] if requested_source else parse_email_sources(email_config.EMAIL_SOURCE)
    provider_error = _provider_error(
        sources,
        gmail_cdks,
        paymesh_cdks,
        gmail_routed_domains,
        paymesh_routed_domains,
    )
    if provider_error:
        return {"ok": False, "error": provider_error}, 400

    submit_kwargs = {"count": count, "workers": workers}
    # Automation callers already provide the number of account jobs required
    # by Sub2API. The manual WebUI form enters source-group count and expands
    # each source to the fixed twelve-alias registration capacity.
    automation_registration = (
        isinstance(automation_context, dict)
        and automation_context.get("sub2api_automation_kind") == "registration"
    )
    if requested_source is not None:
        submit_kwargs["email_source"] = requested_source
    if "gmail_123452026" in sources:
        submit_kwargs["gmail_cdks"] = gmail_cdks
        if gmail_routed_domains:
            submit_kwargs["gmail_routed_domains"] = gmail_routed_domains
    if "paymesh" in sources:
        submit_kwargs["paymesh_cdks"] = paymesh_cdks
        if paymesh_routed_domains:
            submit_kwargs["paymesh_routed_domains"] = paymesh_routed_domains
    if "gmail_api_url" in sources:
        # Manual WebUI count is the number of source purchases/groups. Each
        # source contributes up to 12 aliases, so the service receives the
        # expanded registration-job count. Automation already sends account
        # count and must never be multiplied here.
        aliases_per_email = 12
        requested_job_count = (
            count if automation_registration else count * aliases_per_email
        )
        if requested_job_count > MAX_REGISTRATION_TASKS:
            return {
                "ok": False,
                "error": (
                    f"Gmail API URL: {count} source × {aliases_per_email} alias = "
                    f"{requested_job_count} task, vượt {MAX_REGISTRATION_TASKS}"
                ),
            }, 400
        summary = database.gmail_api_url_email_pool_summary()
        alias_available = int(summary.get("alias_available", 0) or 0)
        submit_kwargs["count"] = requested_job_count
        submit_kwargs["gmail_api_url_aliases_per_email"] = aliases_per_email
        purchase_error = (
            _qan8_purchase_config_error()
            if alias_available < requested_job_count
            else None
        )
        if purchase_error:
            return {
                "ok": False,
                "error": purchase_error,
            }, 400
    if automation_context:
        submit_kwargs["automation_context"] = automation_context
    try:
        jobs = service.submit_registration(**submit_kwargs)
    except GmailBatchError as exc:
        return {"ok": False, "error": str(exc)}, 400
    effective_workers = service.effective_registration_workers(workers)
    response = {
        "ok": True,
        "submitted": len(jobs),
        "jobs": jobs,
        "warning": _pool_warning(
            database,
            sources,
            count,
            gmail_api_url_aliases_per_email=aliases_per_email
            if "gmail_api_url" in sources
            else 1,
        ),
        "workers": effective_workers,
    }, 200
    return response
