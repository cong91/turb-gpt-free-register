"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（留空时自动生成；所有 browser 注册驱动都会强制使用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 注册成功后是否自动查询套餐/Plus 资格。
# 浏览器驱动会在当前注册窗口内同步完成查询；协议驱动仍使用后台查询队列。
AUTO_PLAN_CHECK_AFTER_REGISTER = False

# 注册后查到明确 Free 且没有 Free Plus 试用资格时，自动执行 Codex OAuth。
# 浏览器驱动复用当前注册浏览器；协议驱动在当前 plan worker 中直接执行 OAuth。
AUTO_CODEX_FOR_FREE_AFTER_REGISTER = False

# 注册成功并拿到 accessToken 后，在浏览器里随机停留一段时间再关闭连接。
# 格式：最小秒,最大秒。设为 "0,0" 表示不额外停留。
POST_REGISTER_DWELL_SECONDS_RANGE = "18,45"

# 账号尚未创建时，浏览器页面/OTP 的已知临时错误自动创建的新注册任务数。
# 每次会使用一个新邮箱 alias；0 表示关闭，默认只补跑一次。
REGISTRATION_AUTO_RETRY_ATTEMPTS = 1

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_NAME': 'str',
    'AUTO_PLAN_CHECK_AFTER_REGISTER': 'bool',
    'AUTO_CODEX_FOR_FREE_AFTER_REGISTER': 'bool',
    'POST_REGISTER_DWELL_SECONDS_RANGE': 'str',
    'REGISTRATION_AUTO_RETRY_ATTEMPTS': 'int',
})
