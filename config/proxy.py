"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
    - host:port:user:pass  简写格式，按 HTTP 代理处理
"""
import random
from urllib.parse import quote, urlparse

import requests

from config.env_loader import apply_env_overrides

# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3

# Proxy xoay proxy.vn: lease chỉ tồn tại khi worker đang chạy. Khi hoàn tất,
# key được trả ngay; proxy còn TTL được cache để workflow kế tiếp tái dùng.
# Key mới chỉ được mua khi số worker đồng thời vượt số key nhàn rỗi.
ROTATING_PROXY_ENABLED = False
ROTATING_PROXY_API_BASE = "https://proxy.vn/proxyxoay"
ROTATING_PROXY_PROXY_API_BASE = "https://proxyxoay.shop/api"
ROTATING_PROXY_API_KEY = ""
ROTATING_PROXY_PROTOCOL = "http"
ROTATING_PROXY_NHAMANG = "random"
ROTATING_PROXY_TINHTHANH = "0"
ROTATING_PROXY_WHITELIST = ""
ROTATING_PROXY_REQUEST_TIMEOUT = 15.0


def normalize_proxy_url(value: str) -> str:
    """Normalize a proxy URL or ``host:port:username:password`` entry."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text

    host, separator, remainder = text.partition(":")
    if not separator:
        raise ValueError("代理格式应为 scheme://host:port 或 host:port:username:password")
    parts = remainder.split(":", 2)
    if len(parts) != 3:
        raise ValueError("代理格式应为 host:port:username:password")
    port, username, password = parts
    if not host.strip() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("代理 host/port 无效")
    return (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host.strip()}:{port}"
    )


def _proxy_candidates() -> list[str]:
    """Return valid proxy URLs from the configured pool."""
    candidates = []
    for raw in PROXY_POOL:
        try:
            normalized = normalize_proxy_url(raw)
            parsed = urlparse(normalized)
            if parsed.scheme and parsed.hostname and parsed.port:
                candidates.append(normalized)
        except (TypeError, ValueError):
            continue
    return candidates


def _proxy_reaches(proxy_url: str, probe_url: str, timeout: float) -> bool:
    """Check that a proxy can establish an HTTP(S) request tunnel."""
    try:
        response = requests.get(
            probe_url,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=max(1.0, float(timeout)),
            allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return 100 <= int(response.status_code) < 600
    except requests.RequestException:
        return False


def pick_proxy(
    *,
    probe_url: str | None = None,
    probe_timeout: float = 5.0,
    max_probe: int | None = None,
) -> str:
    """Pick a random valid proxy, optionally requiring a reachable target."""
    candidates = _proxy_candidates()
    if not candidates:
        return ""
    if not probe_url:
        return random.choice(candidates)
    ordered = random.sample(candidates, len(candidates))
    if max_probe is not None:
        ordered = ordered[: max(1, int(max_probe))]
    for candidate in ordered:
        if _proxy_reaches(candidate, probe_url, probe_timeout):
            return candidate
    return ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
    'ROTATING_PROXY_ENABLED': 'bool',
    'ROTATING_PROXY_API_BASE': 'str',
    'ROTATING_PROXY_PROXY_API_BASE': 'str',
    'ROTATING_PROXY_API_KEY': 'str',
    'ROTATING_PROXY_PROTOCOL': 'str',
    'ROTATING_PROXY_NHAMANG': 'str',
    'ROTATING_PROXY_TINHTHANH': 'str',
    'ROTATING_PROXY_WHITELIST': 'str',
    'ROTATING_PROXY_REQUEST_TIMEOUT': 'float',
})
PROXY = pick_proxy()
