"""sub2api 对接配置。"""
import ast
import re

from config.env_loader import apply_env_overrides

# 生成 Codex Agent Token 成功后，是否自动同步到 sub2api。
SUB2API_AUTO_EXPORT: bool = True

# 同步模式：
# api  = 直接调用 sub2api 接口上传
# file = 只追加/更新本地 sub2api.json
# both = 接口上传成功/失败不影响本地文件同步
SUB2API_SYNC_MODE: str = "api"

# sub2api API 基址；Agent Token 上传和 Codex OAuth 都复用这个地址。
SUB2API_API_BASE: str = ""

# 兼容旧配置：Agent Token 直接上传完整 URL。
SUB2API_API_URL: str = ""

# sub2api 管理接口 API Key；为空则不带鉴权头。
SUB2API_API_KEY: str = ""

# 兼容旧配置名：SUB2API_API_TOKEN。
SUB2API_API_TOKEN: str = ""

# sub2api 管理接口鉴权头：x-api-key: <your-admin-api-key>。
SUB2API_API_AUTH_HEADER: str = "x-api-key"

# x-api-key 不需要 Bearer 前缀。
SUB2API_API_AUTH_PREFIX: str = ""

# 上传超时秒数。
SUB2API_API_TIMEOUT: int = 20

# 本地 sub2api 配置文件输出路径；相对路径按项目根目录解析。
SUB2API_OUTPUT_PATH: str = "sub2api.json"

# 可选代理键；写入 account.proxy_key，并在 sub2api.json proxies 为空时初始化 proxies[0].proxy_key。
SUB2API_PROXY_KEY: str = ""

# 新建/更新 sub2api 账号时使用的默认分组、调度优先级和模型。
# 模型为空时不写入 credentials.model_mapping，保留 sub2api 的默认模型列表。
SUB2API_GROUP_IDS: list[int] = [14]
SUB2API_PRIORITY: int = 1
SUB2API_MODEL: list[str] = []

# Webhook secret used when this worker reports automation events back to sub2api.
# Keep this separate from the sub2api admin API key.
SUB2API_AUTOMATION_CALLBACK_SECRET: str = ""
SUB2API_AUTOMATION_CALLBACK_TIMEOUT: int = 20


# ============================================================
# Codex OAuth 授权对接 sub2
# 当 config.codex.CODEX_AUTH_URL_SOURCE="sub2" 时使用：
#   1) 从 sub2 获取 Codex 授权链接
#   2) 浏览器/协议流程拿到 localhost callback 后回传给 sub2
# ============================================================

# 兼容旧配置：sub2 Codex 管理 API 基址；为空时使用 SUB2API_API_BASE。
SUB2_CODEX_API_BASE: str = ""

# 获取 Codex 授权链接接口路径。
# sub2api 当前接口：POST /api/v1/admin/openai/generate-auth-url
SUB2_CODEX_AUTH_URL_PATH: str = "/api/v1/admin/openai/generate-auth-url"

# 上传/提交 OAuth callback 并创建账号接口路径。
# sub2api 当前创建账号接口：POST /api/v1/admin/openai/create-from-oauth
SUB2_CODEX_CALLBACK_PATH: str = "/api/v1/admin/openai/create-from-oauth"

# 兼容旧配置：sub2 Codex API 鉴权 Token；为空时复用 SUB2API_API_KEY / SUB2API_API_TOKEN。
SUB2_CODEX_API_TOKEN: str = ""

# 鉴权头名称/前缀；为空时复用 SUB2API_API_AUTH_HEADER / SUB2API_API_AUTH_PREFIX。
SUB2_CODEX_AUTH_HEADER: str = ""
SUB2_CODEX_AUTH_PREFIX: str = ""

# callback 上传 payload：
# create_from_oauth => sub2api 原生创建账号：{"session_id","code","state","redirect_uri","name","concurrency","priority","group_ids"}
# exchange_code     => 只换 token，不创建账号（兼容旧逻辑）
SUB2_CODEX_CALLBACK_PAYLOAD_MODE: str = "create_from_oauth"


def _parse_group_ids(value: object) -> list[int] | None:
    """解析 WebUI/.env 可能产生的分组 ID 列表；None 表示配置无效。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            value = parsed
        else:
            value = re.split(r"[\s,]+", text)
    elif isinstance(value, (list, tuple, set)):
        value = list(value)
    else:
        value = [value]

    group_ids: list[int] = []
    items = list(value)
    if not items:
        return []
    for item in items:
        try:
            group_id = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if group_id > 0 and group_id not in group_ids:
            group_ids.append(group_id)
    return group_ids or None


def _parse_models(value: object) -> list[str]:
    """解析逗号/换行分隔的模型 ID，并保持输入顺序去重。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            value = parsed
        else:
            value = re.split(r"[,\r\n]+", text)
    elif isinstance(value, (list, tuple, set)):
        value = list(value)
    else:
        value = [value]

    models: list[str] = []
    for item in value:
        for candidate in re.split(r"[,\r\n]+", str(item or "")):
            model = candidate.strip()
            if model and model not in models:
                models.append(model)
    return models


def get_sub2api_account_defaults() -> dict[str, object]:
    """返回所有导出路径共用的 sub2api 账号默认值。"""
    group_ids = _parse_group_ids(SUB2API_GROUP_IDS)
    if group_ids is None:
        group_ids = [14]

    try:
        priority = int(SUB2API_PRIORITY)
    except (TypeError, ValueError):
        priority = 1
    if priority < 0:
        priority = 1

    models = _parse_models(SUB2API_MODEL)
    return {
        "group_ids": group_ids,
        "priority": priority,
        "model_mapping": {model: model for model in models},
    }

apply_env_overrides(globals(), {
    'SUB2API_AUTO_EXPORT': 'bool',
    'SUB2API_SYNC_MODE': 'str',
    'SUB2API_API_BASE': 'str',
    'SUB2API_API_URL': 'str',
    'SUB2API_API_KEY': 'str',
    'SUB2API_API_TOKEN': 'str',
    'SUB2API_API_AUTH_HEADER': 'str',
    'SUB2API_API_AUTH_PREFIX': 'str',
    'SUB2API_API_TIMEOUT': 'int',
    'SUB2API_OUTPUT_PATH': 'str',
    'SUB2API_PROXY_KEY': 'str',
    'SUB2API_GROUP_IDS': 'list_str_multiline',
    'SUB2API_PRIORITY': 'int',
    'SUB2API_MODEL': 'list_str_multiline',
    'SUB2API_AUTOMATION_CALLBACK_SECRET': 'str',
    'SUB2API_AUTOMATION_CALLBACK_TIMEOUT': 'int',
    'SUB2_CODEX_API_BASE': 'str',
    'SUB2_CODEX_AUTH_URL_PATH': 'str',
    'SUB2_CODEX_CALLBACK_PATH': 'str',
    'SUB2_CODEX_API_TOKEN': 'str',
    'SUB2_CODEX_AUTH_HEADER': 'str',
    'SUB2_CODEX_AUTH_PREFIX': 'str',
    'SUB2_CODEX_CALLBACK_PAYLOAD_MODE': 'str',
})
