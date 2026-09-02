"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入 `turb.sqlite3`，不再修改 `config/*.py`。
    3. `.env` / Docker secret 只作为只读 bootstrap；运行时通过
       config.env_loader 叠加 SQLite 中保存的 settings。
    4. 读取时优先 SQLite settings，再读 `.env`，最后回退到源码默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {"PROXY_POOL"}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表
# ============================================================

EDITABLE_FIELDS = [
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权；浏览器驱动复用当前注册窗口，协议驱动使用独立 session，落盘 codex-邮箱.json",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "默认推荐 roxy；protocol=纯协议，容易封号不建议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright；skyvern=Skyvern Browser Sessions+Playwright",
    },
    {
        "key": "AUTO_PLAN_CHECK_AFTER_REGISTER", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册后自动查套餐", "help": "浏览器驱动在当前注册窗口内同步查询套餐；协议驱动使用后台队列",
    },
    {
        "key": "AUTO_CODEX_FOR_FREE_AFTER_REGISTER", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "Free无Plus试用自动 Codex OAuth", "help": "注册后先查套餐；仅明确为 Free 且没有 Free Plus 试用资格时，直接执行 Codex OAuth。浏览器驱动复用当前注册浏览器。",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Khóa bản quyền Cloak", "help": "Bản quyền Pro; để trống để dùng binary miễn phí",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_NAVIGATION_RETRIES", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak导航重试次数", "help": "页面遇到 ERR_EMPTY_RESPONSE、连接重置等临时网络错误时的页面内重试次数",
    },
    {
        "key": "CLOAK_NAVIGATION_RETRY_DELAY", "file": "cloakbrowser.py", "type": "float", "group": "CloakBrowser",
        "label": "Cloak导航重试间隔", "help": "页面导航重试前等待的秒数",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Khóa API Browser Use", "help": "Lưu trong .env (BROWSER_USE_API_KEY), không ghi lại vào config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "ID profile", "help": "Tùy chọn. Nếu nhập sẽ dùng lại cookies/localStorage của profile Browser Use; nên để trống khi chạy hàng loạt",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "Skyvern",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "ID profile trình duyệt", "help": "Tùy chọn, dùng lại profile trình duyệt của Skyvern",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Khóa API Roxy", "help": "Lưu trong .env (ROXY_API_TOKEN), không ghi lại vào config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_SCRIPT_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Selenium 脚本超时", "help": "execute_async_script/fetch 的超时时间，秒；独立于页面加载超时",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "ROXY_PROFILE_MANAGER_ENABLED", "file": "roxy_profile_manager.py", "type": "bool", "group": "Quản lý profile Roxy",
        "label": "Bật quản lý profile", "help": "Quản lý profile Roxy độc lập, không ảnh hưởng luồng đăng ký",
    },
    {
        "key": "ROXY_PROFILE_MANAGER_OWNER_PREFIX", "file": "roxy_profile_manager.py", "type": "str", "group": "Quản lý profile Roxy",
                    "label": "Tiền tố nhận diện", "help": "Ghi dấu nhận diện của trình quản lý vào remark của Roxy",
    },
    {
        "key": "ROXY_PROFILE_ARCHIVE_DIR", "file": "roxy_profile_manager.py", "type": "str", "group": "Quản lý profile Roxy",
        "label": "Thư mục lưu trữ", "help": "Thư mục cục bộ chứa artifact thư mục/siêu dữ liệu đã mã hóa",
    },
    {
        "key": "ROXY_PROFILE_ARCHIVE_MAX_BYTES", "file": "roxy_profile_manager.py", "type": "int", "group": "Quản lý profile Roxy",
        "label": "Giới hạn lưu trữ siêu dữ liệu", "help": "Số byte tối đa của artifact siêu dữ liệu v1",
    },
    {
        "key": "ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES", "file": "roxy_profile_manager.py", "type": "int", "group": "Quản lý profile Roxy",
        "label": "Giới hạn lưu trữ đầy đủ", "help": "Số byte tối đa của artifact thư mục",
    },
    {
        "key": "ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED", "file": "roxy_profile_manager.py", "type": "bool", "group": "Quản lý profile Roxy",
        "label": "Bật mở cục bộ thử nghiệm", "help": "Chỉ bật sau khi đã kiểm tra thủ công trên phiên bản Roxy mục tiêu",
    },
    {
        "key": "ROXY_PROFILE_ROXY_CHROME_PATH", "file": "roxy_profile_manager.py", "type": "str", "group": "Quản lý profile Roxy",
        "label": "Đường dẫn RoxyChrome", "help": "RoxyChrome.exe dùng để khởi động thử nghiệm cục bộ",
    },
    {
        "key": "ROXY_PROFILE_CACHE_ROOT", "file": "roxy_profile_manager.py", "type": "str", "group": "Quản lý profile Roxy",
        "label": "Thư mục bộ nhớ đệm Roxy", "help": "Chỉ đọc để lấy thư mục trình duyệt của profile từ xa đã đóng",
    },
    {
        "key": "ROXY_PROFILE_OFFLINE_STAGING_DIR", "file": "roxy_profile_manager.py", "type": "str", "group": "Quản lý profile Roxy",
        "label": "Thư mục chuẩn bị cục bộ", "help": "Thư mục cô lập để giải mã artifact cho trình duyệt thử nghiệm cục bộ",
    },
    {
        "key": "ROXY_PROFILE_OFFLINE_TIMEOUT", "file": "roxy_profile_manager.py", "type": "int", "group": "Quản lý profile Roxy",
        "label": "Thời gian chờ CDP cục bộ", "help": "Số giây chờ CDP của RoxyChrome cục bộ sẵn sàng",
    },
    {
        "key": "ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH", "file": "roxy_profile_manager.py", "type": "bool", "group": "Quản lý profile Roxy",
        "label": "Cho phép lệch phiên bản Core", "help": "Tùy chọn thử nghiệm; mặc định tắt, phiên bản Core của bản chụp và RoxyChrome cục bộ phải giống nhau",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "默认推荐 roxy；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "CODEX_RETRY_NETWORK_ATTEMPTS", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "Codex网络重试次数", "help": "补跑遇到 ERR_EMPTY_RESPONSE、连接重置等临时浏览器网络错误时的整轮重试次数",
    },
    {
        "key": "CODEX_RETRY_NETWORK_DELAY", "file": "codex.py", "type": "float", "group": "Codex",
        "label": "Codex网络重试间隔", "help": "补跑整轮网络重试前等待的秒数",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail,tinyhost,gmail_123452026,paymesh,qan8_gmail_api",
    },
    {
        "key": "QAN8_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QAN8 API 地址", "help": "默认 https://shop.qan8.com；QAN8 Gmail provider API 根地址", "storage": "env",
    },
    {
        "key": "QAN8_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QAN8 API Key", "help": "QAN8 Open API Key；保存在 .env，不会写入 config 源码", "storage": "env", "secret": True,
    },
    {
        "key": "QAN8_GMAIL_SKU_ID", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QAN8 Gmail SKU", "help": "从 QAN8 products API 选择 Gmail API URL 商品的 sku_id", "storage": "env",
    },
    {
        "key": "QAN8_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "QAN8 请求超时", "help": "单次 QAN8 HTTP 请求超时秒数",
    },
    {
        "key": "QAN8_ORDER_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "QAN8 订单等待上限", "help": "processing 订单轮询的最大秒数；超时不重复下单",
    },
    {
        "key": "QAN8_ALIASES_PER_SOURCE", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "每个 QAN8 source 的 alias 数", "help": "默认 12；单次任务仍可在注册页覆盖，范围 1-12",
    },
    {
        "key": "GMAIL_123452026_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Gmail CDK API", "help": "默认 http://gmail.123452026.xyz/api", "storage": "env",
    },
    {
        "key": "GMAIL_123452026_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Gmail CDK 请求超时", "help": "单次 API 请求超时秒数",
    },
    {
        "key": "GMAIL_123452026_ACCOUNTS_PER_CDK", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "每个 CDK 账号数", "help": "范围 1-6，实际还受 API remainingUses 限制",
    },
    {
        "key": "PAYMESH_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Paymesh MAIL API", "help": "默认 https://sms.paymesh.cn；使用 /api/v1/redeem 与 /api/v1/order/lookup", "storage": "env",
    },
    {
        "key": "PAYMESH_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Paymesh 请求超时", "help": "单次 API 请求超时秒数，不是等待验证码的总时长",
    },
    {
        "key": "PAYMESH_OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Paymesh OTP 最长等待", "help": "每轮等待 Paymesh 验证码的最长秒数，默认 180",
    },
    {
        "key": "PAYMESH_ACCOUNTS_PER_CDK", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "每个 Paymesh card 账号数", "help": "范围 1-6；同一 MAIL card 复用别名",
    },
    {
        "key": "PAYMESH_ROUTED_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Paymesh routed domain (test local)",
        "help": "每个域名一行（最多 2）；为同一 card 額外生成 xxx+hash@<domain> 别名用于本地防伪测试",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "TINYHOST_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "TinyHost API 地址", "help": "默认 https://tinyhost.shop；TinyHost 不需要 API Key", "storage": "env",
    },
    {
        "key": "TINYHOST_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "TinyHost 请求超时", "help": "单次 TinyHost HTTP 请求超时秒数",
    },
    {
        "key": "TINYHOST_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "TinyHost 邮箱名前缀长度", "help": "随机 local-part 长度，范围 6-32",
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "转发域名(cloudflare_domain)", "help": "仅 cloudflare_domain 使用：Email Routing 的域名，如 mydomain.com；与 EMAIL_SOURCE=cloudflare 无关",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "仅 cloudflare_domain：接收 Email Routing 转发的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "仅 cloudflare_domain：QQ IMAP 授权码，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "选择 mailnest 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "项目代码 默认 chatgpt001 获取页面 mailnest.top/buy-email",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail API 地址", "help": "Cloud Mail Worker/API 地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail管理员邮箱", "help": "用于生成 Token；域名被平台隐藏时也会用它登录读取域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 密码", "help": "用于自动获取 Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "CloudMail/Cloud Mail API Authorization Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 域名列表", "help": "可留空；运行时会自动从平台获取。也可点“获取 CloudMail 域名”缓存到这里",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    {
        "key": "REMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API 地址", "help": "默认 https://remail.aishop6.com；也可填写文档地址 https://remail.aishop6.com/docs",
        "external_url": "https://remail.aishop6.com/register?aff=AFFLGYQMTYIXH",
        "external_label": "打开 Remail 官网",
    },
    {
        "key": "REMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API Key", "help": "Remail 控制台生成的 rk- 开头 API Key；选择 remail 来源时必填，保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "REMAIL_PROJECT_ID", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 项目 ID", "help": "Remail API 项目列表中的 projectId，用于匹配 ChatGPT/OpenAI 验证码项目",
    },
    {
        "key": "REMAIL_EMAIL_SUFFIX", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 邮箱后缀", "help": "下单时使用的邮箱后缀，默认 outlook.com；不要填写完整邮箱",
    },
    {
        "key": "REMAIL_SERVICE_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 服务模式", "help": "code=短效接码；purchase=长效购买（可重复收件，默认）",
    },
    {
        "key": "REMAIL_SUPPLY_POLICY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 库存策略", "help": "private_first 优先自有库存；public_only 只使用公开库存（默认）",
    },
    {
        "key": "REMAIL_ORDER_WAIT_SECONDS", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 订单等待(秒)", "help": "下单后未立即返回 service token 时等待订单补齐凭证，默认 30 秒",
    },
    {
        "key": "REMAIL_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 请求超时(秒)", "help": "Remail API 单次 HTTP 请求超时，默认 20 秒",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "支持 http(s)://user:pass@host:port 或 host:port:user:pass；留空行会被忽略；为空则不使用代理",
    },
    {
        "key": "ROTATING_PROXY_ENABLED", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "Bật proxy xoay Proxy.vn", "help": "Đăng ký, Codex, kiểm tra tài khoản, kiểm tra gói, lấy link, 2FA, đổi email và Agent dùng lease bền vững theo từng lane; cùng scope + lane sẽ dùng lại proxy, key xoay không trùng giữa các lane đang hoạt động",
    },
    {
        "key": "ROTATING_PROXY_API_KEY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "API Key chính của Proxy.vn", "help": "Dùng để xem danh sách, mua và gia hạn key xoay; chỉ lưu vào .env, không ghi vào mã nguồn cấu hình",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROTATING_PROXY_PROTOCOL", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Giao thức proxy", "help": "http dùng proxyhttp; socks5 dùng proxysocks5",
    },
    {
        "key": "ROTATING_PROXY_NHAMANG", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Nhà mạng", "help": "Giá trị nhamang gửi tới proxy.vn; mặc định random, có thể nhập giá trị nhà cung cấp hỗ trợ",
    },
    {
        "key": "ROTATING_PROXY_TINHTHANH", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Mã tỉnh/thành", "help": "Giá trị tinhthanh gửi tới proxy.vn; 0 nghĩa là random",
    },
    {
        "key": "ROTATING_PROXY_WHITELIST", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Danh sách IPv4 cho phép", "help": "Nhập các IPv4 được nhà cung cấp cho phép nếu cần; không cần thì để trống",
    },
    {
        "key": "ROTATING_PROXY_REQUEST_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Thời gian chờ API proxy (giây)", "help": "Thời gian chờ cho mỗi lần xem danh sách, mua, gia hạn hoặc lấy proxy",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Chế độ mạng khi kiểm tra gói/Agent", "help": "Dùng để kiểm tra gói và tạo Agent Token; auto dùng proxy khi có proxy cục bộ, nếu không thì kết nối trực tiếp; proxy luôn dùng proxy; direct luôn kết nối trực tiếp",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "Proxy riêng khi kiểm tra gói/Agent", "help": "Dùng để kiểm tra gói và tạo Agent Token; khi để trống, auto/proxy sẽ chọn từ kho proxy. Có thể chứa thông tin xác thực, chỉ lưu vào .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Thời gian chờ kiểm tra gói/Agent (giây)", "help": "Thời gian chờ cho mỗi lần kiểm tra gói và tạo Agent Token; nên dùng 10-20 giây, độc lập với thời gian chờ đăng ký",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "Số lần thử tối đa khi kiểm tra gói/Agent", "help": "Số lần thử lại khi kiểm tra gói và tạo Agent Token gặp lỗi mạng, 429, 5xx hoặc lỗi tạm thời khác; nên dùng 2 lần",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Khoảng chờ thử lại gói/Agent (giây)", "help": "Khoảng chờ giữa các lần thử lại khi kiểm tra gói và tạo Agent Token; tăng theo số lần thử, ưu tiên Retry-After từ máy chủ",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Độ trễ kiểm tra lại tài khoản mới (giây)", "help": "Kiểm tra lại một lần khi tài khoản free mới đăng ký chưa có thông tin dùng thử hoặc lần kiểm tra đầu thất bại; 0 nghĩa là tắt",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "Số luồng kiểm tra gói", "help": "Dùng chung cho kiểm tra tự động, thủ công và hàng loạt; tạo Agent Token dùng hàng đợi riêng; nên dùng 2-4 luồng",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "Giới hạn hàng đợi kiểm tra gói", "help": "Ngăn thao tác hàng loạt bất thường xếp hàng vô hạn; nên dùng 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Khoảng tối thiểu giữa yêu cầu gói/Agent (giây)", "help": "Giới hạn tần suất bắt đầu yêu cầu kiểm tra gói và tạo Agent Token để giảm nguy cơ 429",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "Độ trễ ngẫu nhiên của yêu cầu gói/Agent (giây)", "help": "Thêm độ trễ ngẫu nhiên vào khoảng tối thiểu giữa các yêu cầu kiểm tra gói và tạo Agent Token để tránh lịch gọi quá đều",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_MODE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链后端模式", "help": "auto=本地 PAY.153 checkout（旋转代理）；仅显式 remote 才使用 API/CDK",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_BILLING_COUNTRY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "本地 Checkout 账单国家", "help": "PAY.153 本地模式默认 PH",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_CURRENCY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "本地 Checkout 币种", "help": "PAY.153 本地模式默认 PHP",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_PLAN_NAME", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "本地 Checkout 计划", "help": "默认 chatgptplusplan",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_PROMO_CAMPAIGN_ID", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "本地 Checkout 优惠活动", "help": "默认 plus-1-month-free",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_APPLY_PROMO", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "本地 Checkout 应用优惠", "help": "验证首月免费金额为 0",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_CHECKOUT_ATTEMPTS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "本地 Checkout 重试次数", "help": "PAY.153 本地创建 Checkout 的最大尝试数，建议 1-3",
    },
    {
        "key": "EXTRACT_LINK_LOCAL_UPDATE_ATTEMPTS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "本地优惠更新重试次数", "help": "PAY.153 本地更新优惠的最大尝试数，建议 1-3",
    },
    {
        "key": "EXTRACT_LINK_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务地址", "help": "填写提链服务 API 地址",
    },
    {
        "key": "EXTRACT_LINK_CDK", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链 CDK", "help": "创建提链任务和监听任务事件使用；成功提链扣 1 次",
        "storage": "env", "secret": True,
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链并发数", "help": "批量提链后台线程数，建议 1-4",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_AUTOMATION_CALLBACK_SECRET", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 自动化回调 Secret", "help": "Turb 完成 provisioning 或 reauthorization 后回调 sub2api 时使用；必须与 sub2api 的 callback secret 一致，保存到 SQLite",
        "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key",
    },
    {
        "key": "SUB2API_GROUP_IDS", "file": "sub2api.py", "type": "list_str_multiline", "group": "Codex",
        "label": "sub2 默认分组 ID", "help": "每行一个 sub2api 分组 ID；默认 14；保存后用于 OAuth、Agent Token 和 Codex 补跑导出",
    },
    {
        "key": "SUB2API_PRIORITY", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 默认优先级", "help": "用于新建或更新的 sub2api 账号；默认 1，数值越小优先级越高",
    },
    {
        "key": "SUB2API_MODEL", "file": "sub2api.py", "type": "list_str_multiline", "group": "Codex",
        "label": "Codex 补跑 models", "help": "每行一个或用逗号分隔多个模型 ID；每个模型都会写入 sub2api credentials.model_mapping；留空则使用 sub2api 默认模型",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码通道", "help": "grizzly / viotp / hero / l / h；HeroSMS auto 按实时 cost 从低到高扫描，sticky country 仅在同价位优先",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；HeroSMS 可填 auto 按实时价格/库存选择符合条件的候选",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L/H 复用此字段；HeroSMS OpenAI/ChatGPT 使用 dr",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "HERO_SMS_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS API 地址", "help": "默认 https://hero-sms.com/stubs/handler_api.php",
    },
    {
        "key": "HERO_SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS API密钥", "help": "保存在 .env（HERO_SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "HERO_SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS 服务代码", "help": "OpenAI / ChatGPT 使用 dr", "storage": "env",
    },
    {
        "key": "HERO_SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS 国家", "help": "auto=按实时 cost 从低到高扫描；sticky country 只在同价位优先，较贵 sticky 等低价候选失败后再试；也可填固定 country ID", "storage": "env",
    },
    {
        "key": "HERO_SMS_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "HeroSMS 最高价", "help": "可选硬上限；先尝试低价 offer，逐级升到该价格，绝不超过它；留空不限", "storage": "env",
    },
    {
        "key": "HERO_SMS_NUMBER_REJECT_THRESHOLD", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "HeroSMS 已用号码换国家阈值", "help": "同一 country 累计收到已使用号码错误达到此次数后，auto 模式暂时改试其他 country", "storage": "env",
    },
    {
        "key": "VIOTP_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "ViOTP API 地址", "help": "ViOTP API 基础地址，默认 https://api.viotp.com",
    },
    {
        "key": "VIOTP_API_TOKEN", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "ViOTP Token", "help": "保存在 .env（VIOTP_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "VIOTP_SERVICE_ID", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "ViOTP 服务ID", "help": "ViOTP serviceId，可通过 /service/getv2 查询",
        "storage": "env",
    },
    {
        "key": "VIOTP_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "ViOTP 国家", "help": "可留空；例如 vn 或 la",
        "storage": "env",
    },
    {
        "key": "VIOTP_NETWORK", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "ViOTP 运营商", "help": "可留空；多个运营商用 | 分隔",
        "storage": "env",
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "NORDVPN_WG_ENABLED", "file": "nordvpn_wireguard.py", "type": "bool", "group": "NordVPN WireGuard",
        "label": "启用独立代理", "help": "总开关。关闭后即使仍保存 Access Token，也不会为 Roxy 创建 NordLynx SOCKS5",
    },
    {
        "key": "NORDVPN_ACCESS_TOKEN", "file": "nordvpn_account.py", "type": "str", "group": "NordVPN WireGuard",
        "label": "NordVPN Access Token", "help": "仅保存到 .env；用于从 NordVPN API 获取 NordLynx 私钥，不会写入 Roxy Profile",
        "storage": "env", "secret": True,
    },
    {
        "key": "NORDVPN_WG_COUNTRY_FILTER", "file": "nordvpn_wireguard.py", "type": "str", "group": "NordVPN WireGuard",
        "label": "出口国家", "help": "两位国家代码，如 JP/US/SG；留空使用 NordVPN 推荐服务器",
    },
    {
        "key": "NORDVPN_WG_WIREPROXY_EXE", "file": "nordvpn_wireguard.py", "type": "str", "group": "NordVPN WireGuard",
        "label": "wireproxy 路径", "help": "留默认值即可；PATH 中没有时会自动下载已校验版本，也可填写完整路径",
    },
    {
        "key": "NORDVPN_WG_AUTO_DOWNLOAD", "file": "nordvpn_wireguard.py", "type": "bool", "group": "NordVPN WireGuard",
        "label": "自动安装 wireproxy", "help": "PATH 中找不到时自动下载固定版本并校验 SHA-256 到 data/tools",
    },
    {
        "key": "NORDVPN_WG_PORT_START", "file": "nordvpn_wireguard.py", "type": "int", "group": "NordVPN WireGuard",
        "label": "SOCKS5 起始端口", "help": "每个并发注册任务分配一个本地端口",
    },
    {
        "key": "NORDVPN_WG_PORT_END", "file": "nordvpn_wireguard.py", "type": "int", "group": "NordVPN WireGuard",
        "label": "SOCKS5 结束端口", "help": "端口区间大小至少等于最大并发 workers",
    },
    {
        "key": "NORDVPN_WG_CONNECT_TIMEOUT", "file": "nordvpn_wireguard.py", "type": "float", "group": "NordVPN WireGuard",
        "label": "代理就绪超时(秒)", "help": "等待 wireproxy 开始监听 SOCKS5 的最长时间",
    },
    {
        "key": "NORDVPN_API_BASE", "file": "nordvpn_account.py", "type": "str", "group": "NordVPN WireGuard",
        "label": "NordVPN API", "help": "默认 https://api.nordvpn.com，通常无需修改",
    },
    {
        "key": "NORDVPN_API_TIMEOUT", "file": "nordvpn_account.py", "type": "float", "group": "NordVPN WireGuard",
        "label": "API 超时(秒)", "help": "获取 NordLynx 凭据和推荐服务器的请求超时",
    },
    {
        "key": "NORDVPN_SERVER_CACHE_TTL", "file": "nordvpn_account.py", "type": "int", "group": "NordVPN WireGuard",
        "label": "服务器缓存(秒)", "help": "缓存推荐服务器列表，选取时仍会避开最近使用的服务器",
    },
    {
        "key": "NORDVPN_ENABLED", "file": "nordvpn.py", "type": "bool", "group": "NordVPN",
        "label": "启用 NordVPN CLI", "help": "开启后可通过命令行控制本地 NordVPN 连接；关闭则所有操作静默跳过",
    },
    {
        "key": "NORDVPN_INSTALL_DIR", "file": "nordvpn.py", "type": "str", "group": "NordVPN",
        "label": "NordVPN 安装目录", "help": "NordVPN.exe 所在目录，默认 C:\\Program Files\\NordVPN",
    },
    {
        "key": "NORDVPN_CLI_TIMEOUT", "file": "nordvpn.py", "type": "int", "group": "NordVPN",
        "label": "CLI 超时(秒)", "help": "单次 connect/disconnect 命令的最长等待秒数",
    },
    {
        "key": "NORDVPN_POST_CONNECT_DELAY", "file": "nordvpn.py", "type": "float", "group": "NordVPN",
        "label": "连接后等待(秒)", "help": "connect 成功后等待 NordLynx tunnel 稳定的额外秒数",
    },
    {
        "key": "NORDVPN_COUNTRY_GROUPS", "file": "nordvpn.py", "type": "str", "group": "NordVPN",
        "label": "国家分组", "help": "逗号分隔的国家/专业服务器分组代码，如 Japan,United_States；留空连接最佳服务器",
    },
    {
        "key": "NORDVPN_AUTO_ROTATE_ENABLED", "file": "nordvpn.py", "type": "bool", "group": "NordVPN",
        "label": "自动轮换IP", "help": "开启后每注册成功 N 个账号自动切换 NordVPN 服务器",
    },
    {
        "key": "NORDVPN_AUTO_ROTATE_INTERVAL", "file": "nordvpn.py", "type": "int", "group": "NordVPN",
        "label": "轮换间隔(个)", "help": "每成功注册多少个账号后自动切换一次 IP",
    },
    {
        "key": "NORDVPN_AUTO_ROTATE_COUNTRY_GROUP", "file": "nordvpn.py", "type": "str", "group": "NordVPN",
        "label": "轮换目标地区", "help": "自动轮换时连接的目标国家/地区分组；留空使用上方的国家分组",
    },
]

RUNTIME_SETTINGS_STORAGE = "sqlite"
for _field in EDITABLE_FIELDS:
    _field["storage"] = RUNTIME_SETTINGS_STORAGE
    _field["help"] = str(_field.get("help") or "").replace(".env", "SQLite")

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001, S110
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:  # noqa: BLE001
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:  # noqa: BLE001
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:  # noqa: BLE001, S110
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:  # noqa: BLE001
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 SQLite settings，再读取 `.env` / 环境变量；没有配置时回退到
    `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file, read_runtime_settings
    load_env(override=True)
    env_file_values = read_env_file()
    runtime_values = read_runtime_settings()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in runtime_values:
            raw_env_value = runtime_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = RUNTIME_SETTINGS_STORAGE
        item["help"] = str(item.get("help") or "").replace(".env", "SQLite")
        item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 canonical SQLite，不修改 config/*.py 或只读 .env
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 runtime_config 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写 canonical SQLite settings。"""
    from config.env_loader import load_env, write_env_values

    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated}
