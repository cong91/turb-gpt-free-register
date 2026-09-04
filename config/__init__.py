from config.env_loader import load_env

load_env(override=False)

# -*- coding: utf-8 -*-
"""
config 包的统一入口。

为保留 `from config import USER_AGENT` 这种历史用法，本文件把所有子模块的常量
重新导出到包顶层。新代码推荐按子模块直接导入：
    from config.email import EMAIL_SOURCE
    from config.proxy import pick_proxy

子模块清单：
    config.browser           浏览器指纹 / curl_cffi impersonate / HTTP 超时
    config.openai_protocol   OpenAI OAuth 固定参数 / Sentinel 版本
    config.proxy             代理池 + 随机抽取
    config.register          注册默认信息（邮箱、密码、名称、生日）
    config.email             Outlook 邮箱账号池 + OTP 轮询
    config.twofa             2FA 开关和 re-auth OTP 等待
"""

# ---------- 浏览器 / HTTP ----------
# ---------- 热加载支持 ----------
# WebUI 改配置后调 reload_all() 即可让所有运行时代码看到新值，无需重启进程。
# 前提：运行时代码读配置时用 `config.<子模块>.KEY` 形式（而不是 `from config.子模块 import KEY` 把值绑死）。
# 比如 `from config import codex; ... codex.SMS_COUNTRY`，reload 后 codex 模块对象原地更新，
# 引用 codex.SMS_COUNTRY 立即看到新值。
import importlib as _importlib

from config.browser import (
    ACCEPT_LANGUAGE,
    AUTO_BROWSER_LOCALE_FROM_IP,
    BROWSER_LOCALE_PROFILE,
    BROWSER_LOCALE_PROFILES,
    BROWSER_OS,
    BROWSER_PROFILE_POOL,
    CHROME_FULL_VERSION,
    CHROME_MAJOR,
    CLOUD_PROXY_ORG_KEYWORDS,
    COUNTRY_LOCALE_PROFILE_MAP,
    DEVICE_MEMORY,
    DOCUMENT_KEY_SAMPLES,
    HARDWARE_CONCURRENCY,
    IMPERSONATE,
    IP_GEO_ENDPOINTS,
    IP_GEO_TIMEOUT,
    JS_HEAP_SIZE_LIMIT,
    NAVIGATOR_LANGUAGE,
    NAVIGATOR_LANGUAGES,
    NAVIGATOR_PLATFORM,
    NAVIGATOR_PROTO_SAMPLES,
    NAVIGATOR_VENDOR,
    REJECT_CLOUD_PROXY,
    REQUEST_TIMEOUT,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SEC_CH_UA,
    SEC_CH_UA_ARCH,
    SEC_CH_UA_BITNESS,
    SEC_CH_UA_FULL_VERSION_LIST,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_MODEL,
    SEC_CH_UA_PLATFORM,
    SEC_CH_UA_PLATFORM_VERSION,
    SEND_HIGH_ENTROPY_CLIENT_HINTS,
    TIMEZONE_IANA,
    TIMEZONE_NAME,
    TIMEZONE_OFFSET_MINUTES,
    USER_AGENT,
    USER_AGENT_DATA_PLATFORM,
    WINDOW_FEATURE_FLAGS,
    WINDOW_KEY_SAMPLES,
    build_browser_environment,
    pick_browser_profile,
    validate_browser_profile,
)

# ---------- 邮箱服务 ----------
from config.email import (
    CLOUDFLARE_API_BASE,
    CLOUDFLARE_API_KEY,
    CLOUDFLARE_AUTH_MODE,
    CLOUDFLARE_CUSTOM_AUTH,
    CLOUDFLARE_DEFAULT_DOMAINS,
    CLOUDFLARE_NAME_LENGTH,
    CLOUDFLARE_PATH_ACCOUNTS,
    CLOUDFLARE_PATH_DOMAINS,
    CLOUDFLARE_PATH_MESSAGES,
    CLOUDFLARE_PATH_TOKEN,
    CLOUDFLARE_REQUEST_TIMEOUT,
    CLOUDMAIL_ADMIN_EMAIL,
    CLOUDMAIL_API_BASE,
    CLOUDMAIL_AUTH_TOKEN,
    CLOUDMAIL_AUTO_ADD_USER,
    CLOUDMAIL_DOMAINS,
    CLOUDMAIL_PASSWORD,
    CLOUDMAIL_RANDOM_LOCAL_LENGTH,
    CLOUDMAIL_TOKEN_PATH,
    EMAIL_DOMAIN,
    EMAIL_SOURCE,
    GMAIL_123452026_ACCOUNTS_PER_CDK,
    GMAIL_123452026_ALLOW_INSECURE_HTTP,
    GMAIL_123452026_API_BASE,
    GMAIL_123452026_REQUEST_TIMEOUT,
    GPTMAIL_API_KEY,
    MAIL_NEST_API_KEY,
    MAIL_NEST_PROJECT_CODE,
    OTP_MAX_WAIT,
    OTP_POLL_INTERVAL,
    OTP_SETTLE_SECONDS,
    OUTLOOK_ACCOUNTS_FILE,
    OUTLOOK_API_BASE,
    PAYMESH_ACCOUNTS_PER_CDK,
    PAYMESH_API_BASE,
    PAYMESH_REQUEST_TIMEOUT,
    PAYMESH_ROUTED_DOMAINS,
    QAN8_ALIASES_PER_SOURCE,
    QAN8_API_BASE,
    QAN8_API_KEY,
    QAN8_GMAIL_SKU_ID,
    QAN8_ORDER_TIMEOUT,
    QAN8_REQUEST_TIMEOUT,
    QQ_EMAIL,
    QQ_IMAP_PASSWORD,
    QQ_IMAP_PORT,
    QQ_IMAP_SERVER,
    USE_EMAIL_SERVICE,
)

# ---------- OpenAI 协议 ----------
from config.openai_protocol import (
    AB_CLIENT_KEY,
    AB_SDK_VERSION,
    CHATGPT_ANON_BOOTSTRAP_ENABLED,
    CHATGPT_AUTH_BOOTSTRAP_ENABLED,
    CHATGPT_BOOTSTRAP_STRICT,
    OAI_CLIENT_BUILD_NUMBER,
    OAI_CLIENT_VERSION,
    OPENAI_AUDIENCE,
    OPENAI_BUILD_ID,
    OPENAI_CLIENT_ID,
    OPENAI_REDIRECT_URI,
    OPENAI_SCOPE,
    SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE,
    SENTINEL_SV,
    STATSIG_CLIENT_KEY,
    STATSIG_SDK_TYPE,
    STATSIG_SDK_VERSION,
)

# ---------- 代理池 ----------
from config.proxy import (
    PLAN_CHECK_JITTER,
    PLAN_CHECK_MAX_ATTEMPTS,
    PLAN_CHECK_MIN_INTERVAL,
    PLAN_CHECK_PROXY,
    PLAN_CHECK_PROXY_MODE,
    PLAN_CHECK_QUEUE_LIMIT,
    PLAN_CHECK_REGISTRATION_RECHECK_DELAY,
    PLAN_CHECK_RETRY_DELAY,
    PLAN_CHECK_TIMEOUT,
    PLAN_CHECK_WORKERS,
    PROXY,
    PROXY_POOL,
    pick_proxy,
)

# ---------- 注册默认信息 ----------
from config.register import (
    AUTO_CODEX_FOR_FREE_AFTER_REGISTER,
    AUTO_PLAN_CHECK_AFTER_REGISTER,
    POST_REGISTER_DWELL_SECONDS_RANGE,
    REGISTER_EMAIL,
    REGISTER_NAME,
    REGISTER_PASSWORD,
)

# ---------- 2FA ----------
from config.twofa import ENABLE_2FA, TWOFA_OTP_MAX_WAIT

_RELOADABLE_SUBMODULES = (
    "config.browser",
    "config.openai_protocol",
    "config.proxy",
    "config.register",
    "config.email",
    "config.twofa",
    "config.roxybrowser",
    "config.roxy_profile_manager",
    "config.cloakbrowser",
    "config.browser_use",
    "config.skyvern",
    "config.flow_trigger",
    "config.codex",
    "config.extract_link",
    "config.sub2api",
    "config.humanize",
    "config.nordvpn",
    "config.nordvpn_account",
    "config.nordvpn_wireguard",
)


def reload_all() -> list[str]:
    """
    热重载所有 config 子模块，返回成功 reload 的模块名列表。
    任何子模块 reload 失败（语法错等）会抛 ImportError，调用方自行处理。
    """
    from config.env_loader import load_env
    load_env(override=True)

    import sys
    reloaded = []
    for name in _RELOADABLE_SUBMODULES:
        mod = sys.modules.get(name)
        if mod is None:
            mod = _importlib.import_module(name)
        else:
            _importlib.reload(mod)
        reloaded.append(name)
    # 同步刷新 config 包顶层的"被绑死"常量（兼容历史 `from config import X` 用法）
    # 注意：通过这些名字读到的是 reload 前的值，但子模块属性方式不受影响。
    _refresh_top_level_constants()
    return reloaded


def _refresh_top_level_constants() -> None:
    """把刚 reload 的子模块的常量重新拷一份到 config 包顶层。"""
    import config as _self
    from config import (
        browser,
        browser_use,
        cloakbrowser,
        codex,
        email,
        extract_link,
        flow_trigger,
        humanize,
        nordvpn,
        openai_protocol,
        register,
        roxybrowser,
        skyvern,
        sub2api,
        twofa,
    )
    from config import proxy as _proxy
    # 简单粗暴：枚举一遍重要常量，覆盖到 _self
    for src in (browser, openai_protocol, _proxy, register, email, twofa, roxybrowser, cloakbrowser, browser_use, skyvern, codex, extract_link, sub2api, humanize, flow_trigger, nordvpn):
        for k in dir(src):
            if k.isupper() or k in ("pick_proxy", "pick_browser_profile", "build_browser_environment", "validate_browser_profile"):
                setattr(_self, k, getattr(src, k))


__all__ = [
    "AB_CLIENT_KEY",
    "AB_SDK_VERSION",
    "ACCEPT_LANGUAGE",
    "AUTO_BROWSER_LOCALE_FROM_IP",
    "AUTO_CODEX_FOR_FREE_AFTER_REGISTER",
    "AUTO_PLAN_CHECK_AFTER_REGISTER",
    "BROWSER_LOCALE_PROFILE",
    "BROWSER_LOCALE_PROFILES",
    "BROWSER_OS",
    "BROWSER_PROFILE_POOL",
    "CHATGPT_ANON_BOOTSTRAP_ENABLED",
    "CHATGPT_AUTH_BOOTSTRAP_ENABLED",
    "CHATGPT_BOOTSTRAP_STRICT",
    "CHROME_FULL_VERSION",
    "CHROME_MAJOR",
    "CLOUDFLARE_API_BASE",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_AUTH_MODE",
    "CLOUDFLARE_CUSTOM_AUTH",
    "CLOUDFLARE_DEFAULT_DOMAINS",
    "CLOUDFLARE_NAME_LENGTH",
    "CLOUDFLARE_PATH_ACCOUNTS",
    "CLOUDFLARE_PATH_DOMAINS",
    "CLOUDFLARE_PATH_MESSAGES",
    "CLOUDFLARE_PATH_TOKEN",
    "CLOUDFLARE_REQUEST_TIMEOUT",
    "CLOUDMAIL_ADMIN_EMAIL",
    "CLOUDMAIL_API_BASE",
    "CLOUDMAIL_AUTH_TOKEN",
    "CLOUDMAIL_AUTO_ADD_USER",
    "CLOUDMAIL_DOMAINS",
    "CLOUDMAIL_PASSWORD",
    "CLOUDMAIL_RANDOM_LOCAL_LENGTH",
    "CLOUDMAIL_TOKEN_PATH",
    "CLOUD_PROXY_ORG_KEYWORDS",
    "COUNTRY_LOCALE_PROFILE_MAP",
    "DEVICE_MEMORY",
    "DOCUMENT_KEY_SAMPLES",
    "EMAIL_DOMAIN",
    "EMAIL_SOURCE",
    # twofa
    "ENABLE_2FA",
    "GMAIL_123452026_ACCOUNTS_PER_CDK",
    "GMAIL_123452026_ALLOW_INSECURE_HTTP",
    "GMAIL_123452026_API_BASE",
    "GMAIL_123452026_REQUEST_TIMEOUT",
    "GPTMAIL_API_KEY",
    "HARDWARE_CONCURRENCY",
    "IMPERSONATE",
    "IP_GEO_ENDPOINTS",
    "IP_GEO_TIMEOUT",
    "JS_HEAP_SIZE_LIMIT",
    "MAIL_NEST_API_KEY",
    "MAIL_NEST_PROJECT_CODE",
    "NAVIGATOR_LANGUAGE",
    "NAVIGATOR_LANGUAGES",
    "NAVIGATOR_PLATFORM",
    "NAVIGATOR_PROTO_SAMPLES",
    "NAVIGATOR_VENDOR",
    "NORDVPN_AUTO_ROTATE_COUNTRY_GROUP",
    "NORDVPN_AUTO_ROTATE_ENABLED",
    "NORDVPN_AUTO_ROTATE_INTERVAL",
    "NORDVPN_CLI_TIMEOUT",
    "NORDVPN_COUNTRY_GROUPS",
    # nordvpn
    "NORDVPN_ENABLED",
    "NORDVPN_INSTALL_DIR",
    "NORDVPN_POST_CONNECT_DELAY",
    "NORDVPN_SERVICE_HOST",
    "NORDVPN_SERVICE_PORT",
    "OAI_CLIENT_BUILD_NUMBER",
    "OAI_CLIENT_VERSION",
    "OPENAI_AUDIENCE",
    "OPENAI_BUILD_ID",
    # openai_protocol
    "OPENAI_CLIENT_ID",
    "OPENAI_REDIRECT_URI",
    "OPENAI_SCOPE",
    "OTP_MAX_WAIT",
    "OTP_POLL_INTERVAL",
    "OTP_SETTLE_SECONDS",
    "OUTLOOK_ACCOUNTS_FILE",
    "OUTLOOK_API_BASE",
    "PAYMESH_ACCOUNTS_PER_CDK",
    "PAYMESH_API_BASE",
    "PAYMESH_REQUEST_TIMEOUT",
    "PAYMESH_ROUTED_DOMAINS",
    "PLAN_CHECK_JITTER",
    "PLAN_CHECK_MAX_ATTEMPTS",
    "PLAN_CHECK_MIN_INTERVAL",
    "PLAN_CHECK_PROXY",
    "PLAN_CHECK_PROXY_MODE",
    "PLAN_CHECK_QUEUE_LIMIT",
    "PLAN_CHECK_REGISTRATION_RECHECK_DELAY",
    "PLAN_CHECK_RETRY_DELAY",
    "PLAN_CHECK_TIMEOUT",
    "PLAN_CHECK_WORKERS",
    "POST_REGISTER_DWELL_SECONDS_RANGE",
    "PROXY",
    # proxy
    "PROXY_POOL",
    "QAN8_ALIASES_PER_SOURCE",
    "QAN8_API_BASE",
    "QAN8_API_KEY",
    "QAN8_GMAIL_SKU_ID",
    "QAN8_ORDER_TIMEOUT",
    "QAN8_REQUEST_TIMEOUT",
    "QQ_EMAIL",
    "QQ_IMAP_PASSWORD",
    "QQ_IMAP_PORT",
    "QQ_IMAP_SERVER",
    # register
    "REGISTER_EMAIL",
    "REGISTER_NAME",
    "REGISTER_PASSWORD",
    "REJECT_CLOUD_PROXY",
    "REQUEST_TIMEOUT",
    "SCREEN_HEIGHT",
    "SCREEN_WIDTH",
    "SEC_CH_UA",
    "SEC_CH_UA_ARCH",
    "SEC_CH_UA_BITNESS",
    "SEC_CH_UA_FULL_VERSION_LIST",
    "SEC_CH_UA_MOBILE",
    "SEC_CH_UA_MODEL",
    "SEC_CH_UA_PLATFORM",
    "SEC_CH_UA_PLATFORM_VERSION",
    "SEND_HIGH_ENTROPY_CLIENT_HINTS",
    "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE",
    "SENTINEL_SV",
    "STATSIG_CLIENT_KEY",
    "STATSIG_SDK_TYPE",
    "STATSIG_SDK_VERSION",
    "TIMEZONE_IANA",
    "TIMEZONE_NAME",
    "TIMEZONE_OFFSET_MINUTES",
    "TWOFA_OTP_MAX_WAIT",
    # browser
    "USER_AGENT",
    "USER_AGENT_DATA_PLATFORM",
    # email
    "USE_EMAIL_SERVICE",
    "WINDOW_FEATURE_FLAGS",
    "WINDOW_KEY_SAMPLES",
    "build_browser_environment",
    "pick_browser_profile",
    "pick_proxy",
    "validate_browser_profile",
]
