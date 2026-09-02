"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链后端：auto 固定使用本地 PAY.153 checkout 提链器；只有显式填写
# remote 才使用旧的 API/CDK 服务。可选 auto / remote / local。
EXTRACT_LINK_MODE: str = "auto"

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

# 本地 checkout 提链默认沿用 PAY.153 的首月免费参数。
EXTRACT_LINK_LOCAL_BILLING_COUNTRY: str = "PH"
EXTRACT_LINK_LOCAL_CURRENCY: str = "PHP"
EXTRACT_LINK_LOCAL_PLAN_NAME: str = "chatgptplusplan"
EXTRACT_LINK_LOCAL_PROMO_CAMPAIGN_ID: str = "plus-1-month-free"
EXTRACT_LINK_LOCAL_APPLY_PROMO: bool = True
EXTRACT_LINK_LOCAL_CHECKOUT_ATTEMPTS: int = 3
EXTRACT_LINK_LOCAL_UPDATE_ATTEMPTS: int = 3

apply_env_overrides(globals(), {
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_MODE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
    'EXTRACT_LINK_LOCAL_BILLING_COUNTRY': 'str',
    'EXTRACT_LINK_LOCAL_CURRENCY': 'str',
    'EXTRACT_LINK_LOCAL_PLAN_NAME': 'str',
    'EXTRACT_LINK_LOCAL_PROMO_CAMPAIGN_ID': 'str',
    'EXTRACT_LINK_LOCAL_APPLY_PROMO': 'bool',
    'EXTRACT_LINK_LOCAL_CHECKOUT_ATTEMPTS': 'int',
    'EXTRACT_LINK_LOCAL_UPDATE_ATTEMPTS': 'int',
})
