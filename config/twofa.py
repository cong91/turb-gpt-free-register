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

# 仅用于已有账号登录和 2FA re-auth 的邮箱 OTP 等待；普通注册仍使用独立的 OTP_MAX_WAIT。
TWOFA_OTP_MAX_WAIT = 90

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_2FA': 'bool', 'TWOFA_OTP_MAX_WAIT': 'int'})
