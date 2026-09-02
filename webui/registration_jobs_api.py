from __future__ import annotations

from config import email as email_config
from config import register as register_config
from core.email_provider import (
    is_valid_email_source,
    normalize_email_source,
    parse_email_sources,
)
from core.gmail_aliases import GmailAliasError, normalize_routed_domains
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


def _pool_warning(database, sources: list[str], count: int) -> str:
    if any(source in sources for source in (
        "gptmail", "mailnest", "cloudmail", "tinyhost", "cloudflare", "gmail_123452026", "paymesh", "remail",
        "qan8_gmail_api",
    )):
        return ""
    if sources == ["gmail_api_url"]:
        available = database.gmail_api_url_email_pool_summary().get("available", 0)
        return (
            f"Kho Gmail API URL còn {available} bản ghi, ít hơn số bản ghi yêu cầu {count}，bản ghi thiếu sẽ thất bại"
            if available < count else ""
        )
    if "cloudflare_domain" in sources:
        pool = database.domain_email_pool_summary()
        if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
            return f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        return ""
    if sources == ["generic_api"]:
        available = database.generic_api_email_pool_summary().get("available", 0)
        return f"通用 API 邮箱池仅 {available} 个可用，少于任务数 {count}，不足的会失败" if available < count else ""
    if len(sources) > 1:
        available = 0
        if "outlook" in sources:
            available += database.outlook_pool_summary().get("available", 0)
        if "generic_api" in sources:
            available += database.generic_api_email_pool_summary().get("available", 0)
        return f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败" if available < count else ""
    available = database.outlook_pool_summary().get("available", 0)
    return f"可用邮箱仅 {available} 个，少于任务数 {count}，不足的会失败" if available < count else ""


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
    requested_source = normalize_email_source(str(data.get("email_source") or "")) or None
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
    if requested_source is not None and not is_valid_email_source(requested_source):
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
    # by Sub2API. The manual WebUI form may instead enter source Gmail count
    # and expand it into aliases, so keep that legacy UI behavior scoped there.
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
        # Số alias mỗi email gốc (1..12): 6 gmail.com + 6 googlemail.com,
        # tất cả share code_url của email gốc.
        try:
            aliases_per_email = int(data.get("gmail_api_url_alias_count", 1) or 1)
        except (TypeError, ValueError):
            return {"ok": False, "error": "gmail_api_url_alias_count 非法"}, 400
        aliases_per_email = max(1, min(12, aliases_per_email))
        available = int(database.gmail_api_url_email_pool_summary().get("available", 0) or 0)
        if available < 1:
            return {"ok": False, "error": "Kho Gmail API URL không còn bản ghi chưa dùng"}, 400
        if count > available:
            count = available
            submit_kwargs["count"] = count
        if aliases_per_email > 1 and not automation_registration:
            # count = số email gốc; mỗi email gốc sinh aliases_per_email tài khoản.
            job_count = count * aliases_per_email
            if job_count > MAX_REGISTRATION_TASKS:
                return {
                    "ok": False,
                    "error": (
                        f"Gmail API URL: {count} email × {aliases_per_email} alias = "
                        f"{job_count} task, vượt {MAX_REGISTRATION_TASKS}"
                    ),
                }, 400
            submit_kwargs["count"] = job_count
            submit_kwargs["gmail_api_url_aliases_per_email"] = aliases_per_email
    if "qan8_gmail_api" in sources:
        try:
            aliases_per_source = int(
                data.get(
                    "qan8_alias_count",
                    getattr(email_config, "QAN8_ALIASES_PER_SOURCE", 12),
                )
                or 12
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "qan8_alias_count 非法"}, 400
        if not 1 <= aliases_per_source <= 12:
            return {"ok": False, "error": "qan8_alias_count 需在 1~12 之间"}, 400
        submit_kwargs["qan8_aliases_per_source"] = aliases_per_source
        if not automation_registration:
            job_count = count * aliases_per_source
            if job_count > MAX_REGISTRATION_TASKS:
                return {
                    "ok": False,
                    "error": (
                        f"QAN8 Gmail API: {count} source × {aliases_per_source} alias = "
                        f"{job_count} task, vượt {MAX_REGISTRATION_TASKS}"
                    ),
                }, 400
            count = job_count
            submit_kwargs["count"] = count
    if automation_context:
        submit_kwargs["automation_context"] = automation_context
    jobs = service.submit_registration(**submit_kwargs)
    effective_workers = service.effective_registration_workers(workers)
    response = {
        "ok": True,
        "submitted": len(jobs),
        "jobs": jobs,
        "warning": _pool_warning(database, sources, count),
        "workers": effective_workers,
    }, 200
    if "qan8_gmail_api" in sources:
        context = (jobs[0].get("provider_context") if jobs else {}) or {}
        response[0]["qan8"] = {
            "batch_id": context.get("qan8_gmail_api_batch_id"),
            "target_count": count,
            "effective_workers": effective_workers,
            "aliases_per_source": int(submit_kwargs.get("qan8_aliases_per_source", 12)),
            "active_sources": 0,
            "orders_placed": 0,
            "lifetime_sources_purchased": 0,
        }
    return response
