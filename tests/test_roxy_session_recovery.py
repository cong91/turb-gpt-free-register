import unittest
from unittest.mock import ANY, Mock, patch

from core import account_export, registration_service, roxy_registration


class _FakeClock:
    """可控时钟：sleep 推进时间，time.time 返回当前值，避免测试真等待。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _banner_response():
    return {"WARNING_BANNER": "unusual activity", "_http_status": 200}


def _original_session_timeout_error() -> RuntimeError:
    return RuntimeError(
        "等待 /api/auth/session accessToken 超时，最后响应: "
        "{'WARNING_BANNER': 'unusual activity', '_http_status': 200}"
    )


class FetchChatgptSessionFastFailTests(unittest.TestCase):
    def test_fast_fail_false_keeps_polling_after_refresh(self):
        """恢复轮询（fast_fail=False）刷新后仍持续 WARNING_BANNER 也不提前判死。"""
        clock = _FakeClock()
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        reads = {"count": 0}

        def _always_banner(_driver):
            reads["count"] += 1
            return _banner_response()

        with patch.object(
            roxy_registration,
            "_read_chatgpt_session_once",
            side_effect=_always_banner,
        ), patch.object(
            roxy_registration,
            "_check_manual_stop",
        ), patch.object(
            roxy_registration,
            "time",
            clock,
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._fetch_chatgpt_session(driver, timeout=40, fast_fail=False)

        self.assertIn("等待 /api/auth/session accessToken 超时", str(ctx.exception))
        self.assertGreater(
            reads["count"], roxy_registration._SESSION_BANNER_REFRESH_AFTER + 1
        )
        driver.refresh.assert_called_once()


class RecoverChatgptSessionTests(unittest.TestCase):
    def test_recovers_via_repoll_refresh_then_reauth(self):
        driver = Mock()
        token = {"accessToken": "token-ok", "_http_status": 200}

        with patch.object(
            roxy_registration,
            "_fetch_chatgpt_session",
            side_effect=[
                RuntimeError("poll fail 1"),
                RuntimeError("poll fail 2"),
                RuntimeError("poll fail after refresh"),
                token,
            ],
        ), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch(
            "core.account_export.reauth_login_after_session_timeout"
        ) as reauth:
            result = roxy_registration._recover_chatgpt_session(
                driver, "user@example.com", RuntimeError("original"), "Secret123456"
            )

        self.assertEqual(result, token)
        reauth.assert_called_once_with(driver, "user@example.com")
        driver.refresh.assert_called_once()

    def test_reauth_failure_raises_original_session_error(self):
        """re-auth 失败时必须抛出原始错误，保留 retry 分类 marker。"""
        driver = Mock()
        original = _original_session_timeout_error()

        with patch.object(
            roxy_registration,
            "_fetch_chatgpt_session",
            side_effect=RuntimeError("poll fail"),
        ), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch(
            "core.account_export.reauth_login_after_session_timeout",
            side_effect=RuntimeError("re-auth 未进入 email-verification 页面"),
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._recover_chatgpt_session(driver, "user@example.com", original)

        self.assertIn("WARNING_BANNER", str(ctx.exception))
        self.assertTrue(
            registration_service._is_final_session_access_token_timeout(str(ctx.exception))
        )

    def test_final_read_failure_after_reauth_still_raises_original(self):
        driver = Mock()
        original = _original_session_timeout_error()

        with patch.object(
            roxy_registration,
            "_fetch_chatgpt_session",
            side_effect=RuntimeError("still no token"),
        ), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch(
            "core.account_export.reauth_login_after_session_timeout"
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._recover_chatgpt_session(driver, "user@example.com", original)

        self.assertIn("WARNING_BANNER", str(ctx.exception))
        self.assertTrue(
            registration_service._is_final_session_access_token_timeout(str(ctx.exception))
        )


class ReauthLoginAfterSessionTimeoutTests(unittest.TestCase):
    def test_happy_path_navigates_continue_url_in_browser(self):
        session = Mock()

        with patch.object(
            account_export,
            "_trigger_reauth",
            return_value="https://auth.openai.com/authorize?x=1",
        ) as trigger, patch.object(
            account_export,
            "_follow_reauth",
            return_value="https://auth.openai.com/email-verification",
        ), patch.object(
            account_export,
            "_validate_reauth_otp",
            return_value="https://chatgpt.com/api/auth/callback?code=1",
        ) as validate, patch.object(
            account_export, "_resend_reauth_otp"
        ) as resend, patch(
            "core.email_provider.wait_for_otp", return_value="123456"
        ) as wait_otp, patch(
            "core.email_provider.snapshot_verification_code", return_value=None
        ), patch(
            "core.email_provider.acknowledge_verification_code"
        ) as acknowledge, patch.object(
            account_export, "human_delay"
        ):
            account_export.reauth_login_after_session_timeout(session, "user@example.com")

        trigger.assert_called_once_with(session, "user@example.com")
        resend.assert_not_called()
        wait_otp.assert_called_once_with(
            "user@example.com",
            after_ts=ANY,
            before_code=None,
            stage="registration_reauth_email_otp",
        )
        validate.assert_called_once_with(session, "123456")
        acknowledge.assert_called_once_with(
            "user@example.com", "123456", stage="registration_reauth_email_otp"
        )
        session.navigate.assert_called_once_with(
            "https://chatgpt.com/api/auth/callback?code=1"
        )

    def test_retryable_failure_rebuilds_auth_step_and_resends_otp(self):
        session = Mock()

        with patch.object(
            account_export,
            "_trigger_reauth",
            return_value="https://auth.openai.com/authorize?x=1",
        ) as trigger, patch.object(
            account_export,
            "_follow_reauth",
            side_effect=[
                RuntimeError("re-auth 未进入 email-verification 页面: x"),
                "https://auth.openai.com/email-verification",
            ],
        ), patch.object(
            account_export,
            "_validate_reauth_otp",
            return_value="https://chatgpt.com/api/auth/callback?code=1",
        ), patch.object(
            account_export, "_resend_reauth_otp"
        ) as resend, patch.object(
            account_export, "_reset_reauth_context"
        ) as reset, patch(
            "core.email_provider.wait_for_otp", return_value="654321"
        ), patch(
            "core.email_provider.snapshot_verification_code", return_value=None
        ), patch(
            "core.email_provider.acknowledge_verification_code"
        ), patch.object(
            account_export, "human_delay"
        ):
            account_export.reauth_login_after_session_timeout(session, "user@example.com")

        self.assertEqual(trigger.call_count, 2)
        resend.assert_called_once_with(session)
        reset.assert_called_once_with(session)
        session.navigate.assert_called_once()


class EnsureEmailOtpPageReadyTests(unittest.TestCase):
    def test_passes_immediately_on_verification_page(self):
        with patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ):
            roxy_registration._ensure_email_otp_page_ready(Mock())

    def test_raises_with_real_page_error_when_not_on_otp_page(self):
        clock = _FakeClock()
        driver = Mock()
        state = {
            "url": "https://auth.openai.com/create-account/password",
            "errors": ["Failed to create account. Please try again"],
            "text": "Create a password",
        }

        with patch.object(
            roxy_registration, "_is_email_verification_page", return_value=False
        ), patch.object(
            roxy_registration, "_has_access_token", return_value=False
        ), patch.object(
            roxy_registration, "_email_otp_page_state", return_value=state
        ), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", clock
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._ensure_email_otp_page_ready(driver, timeout=3)

        message = str(ctx.exception)
        self.assertIn("当前页面不是邮箱验证码页", message)
        self.assertIn("Failed to create account", message)
        self.assertIn("create-account/password", message)

    def test_stays_silent_when_page_state_cannot_be_probed(self):
        """驱动异常/测试桩探测不了页面时保持沉默，不改变原有流程。"""
        driver = Mock()
        driver.current_url = Mock()  # _is_email_verification_page 内部会抛 TypeError

        roxy_registration._ensure_email_otp_page_ready(driver)


class SignupCreateRejectionTests(unittest.TestCase):
    def test_detects_failed_to_create_account_error(self):
        state = {"errors": ["Failed to create account. Please try again"]}
        self.assertEqual(
            roxy_registration._signup_create_rejection(state),
            "Failed to create account. Please try again",
        )

    def test_ignores_other_errors_and_invalid_state(self):
        self.assertIsNone(roxy_registration._signup_create_rejection({"errors": ["Other"]}))
        self.assertIsNone(roxy_registration._signup_create_rejection(None))


class FillPasswordPageRejectionTests(unittest.TestCase):
    def test_fill_password_page_raises_on_create_account_rejection(self):
        """Job 2648：create-account 被拒后停在密码页，必须抛出真实原因而不是
        让 OTP 重发路径误报“找不到重新发送验证码按钮”。"""
        clock = _FakeClock()
        driver = Mock()
        driver.execute_script.side_effect = [
            {"ok": True, "input": Mock(), "button": Mock()},
            {
                "ok": True,
                "reason": "enabled_submit_target",
                "button": Mock(),
                "text": "Continue",
                "type": "submit",
                "dd": "Continue",
                "ariaDisabled": "false",
            },
        ]
        rejected_state = {
            "url": "https://auth.openai.com/create-account/password",
            "errors": ["Failed to create account. Please try again"],
            "inputs": [],
        }

        with patch.object(
            roxy_registration, "_is_email_verification_page", return_value=False
        ), patch.object(
            roxy_registration, "_has_access_token", return_value=False
        ), patch.object(
            roxy_registration, "_is_signup_password_page", return_value=True
        ), patch.object(
            roxy_registration, "_is_login_password_page", return_value=False
        ), patch.object(
            roxy_registration, "_password_page_state", return_value=rejected_state
        ), patch.object(
            roxy_registration, "_registration_password", return_value="Secret12345678"
        ), patch.object(
            roxy_registration, "_human_type_text"
        ), patch.object(
            roxy_registration, "_human_click"
        ), patch.object(
            roxy_registration, "human_delay"
        ), patch.object(
            roxy_registration, "time", clock
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._fill_password_page_if_present(driver, "user@example.com")

        message = str(ctx.exception)
        self.assertIn("拒绝创建账号", message)
        self.assertIn("Failed to create account", message)


class RemovedInJobRecoveryTiersTests(unittest.TestCase):
    """S2/S3/S4 已删除：任务内不再重启浏览器，换 IP 交给上层自动重试。"""

    def test_deleted_tier_symbols_no_longer_exist(self):
        for symbol in (
            "_relogin_after_browser_restart",
            "_relogin_with_fresh_ip_and_browser",
            "_recover_twofa_with_browser_restart",
            "_restart_roxy_browser",
            "_relogin_existing_account",
            "_submit_login_password_on_page",
        ):
            self.assertFalse(hasattr(roxy_registration, symbol), symbol)


if __name__ == "__main__":
    unittest.main()
