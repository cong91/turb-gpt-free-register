# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册完成 → 通过邮箱 OTP re-auth → enroll TOTP → activate
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

已保存账号的“补做 2FA”任务会先按登录流程完成一次邮箱 OTP，随后复用当前登录态
enroll/activate，不再额外触发第二次邮箱 OTP。

关掉 2FA 不会影响账号可用性，仅意味着账号没有动态口令保护。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

# 2FA 网络代理模式：
#   saved = 优先使用账号保存的有效代理（无有效代理时回退代理池）
#   pool  = 忽略账号保存的代理，每次任务都从 PROXY_POOL 随机抽取
TWOFA_PROXY_MODE = "saved"

# 发起 reauth（CSRF + signin）时的临时网络错误重试。403 会先清理当前会话的
# 本地熔断，再按指数退避重试；业务类 4xx 不重试。
TWOFA_REAUTH_MAX_ATTEMPTS = 3
TWOFA_REAUTH_RETRY_DELAY = 3.0

# 2FA 后台队列。workers 是实际同时执行的账号数，修改后需重启进程以重建线程池。
TWOFA_WORKERS = 4
TWOFA_QUEUE_LIMIT = 200

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_2FA': 'bool',
    'TWOFA_PROXY_MODE': 'str',
    'TWOFA_REAUTH_MAX_ATTEMPTS': 'int',
    'TWOFA_REAUTH_RETRY_DELAY': 'float',
    'TWOFA_WORKERS': 'int',
    'TWOFA_QUEUE_LIMIT': 'int',
})
