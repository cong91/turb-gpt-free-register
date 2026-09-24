import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from core import (
    account_export,
    browser_failure_policy,
    db,
    registration_flow,
    registration_service,
    roxy_registration,
)


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
            registration_flow,
            "_read_chatgpt_session_once",
            side_effect=_always_banner,
        ), patch.object(
            registration_flow,
            "_check_manual_stop",
        ), patch.object(
            registration_flow,
            "time",
            clock,
        ), self.assertRaises(RuntimeError) as ctx:
            registration_flow._fetch_chatgpt_session(driver, timeout=40)

        self.assertIn("等待 /api/auth/session accessToken 超时", str(ctx.exception))
        self.assertGreater(
            reads["count"], registration_flow._SESSION_BANNER_REFRESH_AFTER + 1
        )
        driver.refresh.assert_called_once()


class RecoverChatgptSessionTests(unittest.TestCase):
    def test_recovers_via_repoll_refresh_then_reauth(self):
        driver = Mock()
        token = {"accessToken": "token-ok", "_http_status": 200}

        with patch.object(
            registration_flow,
            "_fetch_chatgpt_session",
            side_effect=[
                RuntimeError("poll fail 1"),
                RuntimeError("poll fail 2"),
                RuntimeError("poll fail after refresh"),
                token,
            ],
        ), patch.object(
            registration_flow, "_check_manual_stop"
        ), patch.object(
            registration_flow, "time", _FakeClock()
        ), patch(
            "core.account_export.reauth_login_after_session_timeout"
        ) as reauth:
            result = roxy_registration._recover_chatgpt_session(
                driver, "user@example.com", RuntimeError("original")
            )

        self.assertEqual(result, token)
        reauth.assert_called_once_with(driver, "user@example.com")
        driver.refresh.assert_called_once()

    def test_reauth_failure_raises_original_session_error(self):
        """re-auth 失败时必须抛出原始错误，保留 retry 分类 marker。"""
        driver = Mock()
        original = _original_session_timeout_error()

        with patch.object(
            registration_flow,
            "_fetch_chatgpt_session",
            side_effect=RuntimeError("poll fail"),
        ), patch.object(
            registration_flow, "_check_manual_stop"
        ), patch.object(
            registration_flow, "time", _FakeClock()
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
            registration_flow,
            "_fetch_chatgpt_session",
            side_effect=RuntimeError("still no token"),
        ), patch.object(
            registration_flow, "_check_manual_stop"
        ), patch.object(
            registration_flow, "time", _FakeClock()
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
            registration_flow, "_is_email_verification_page", return_value=True
        ):
            registration_flow._ensure_email_otp_page_ready(Mock())

    def test_raises_with_real_page_error_when_not_on_otp_page(self):
        clock = _FakeClock()
        driver = Mock()
        state = {
            "url": "https://auth.openai.com/create-account/password",
            "errors": ["Failed to create account. Please try again"],
            "text": "Create a password",
        }

        with patch.object(
            registration_flow, "_is_email_verification_page", return_value=False
        ), patch.object(
            registration_flow, "_has_access_token", return_value=False
        ), patch.object(
            registration_flow, "_email_otp_page_state", return_value=state
        ), patch.object(
            registration_flow, "_check_manual_stop"
        ), patch.object(
            registration_flow, "time", clock
        ), self.assertRaises(RuntimeError) as ctx:
            registration_flow._ensure_email_otp_page_ready(driver, timeout=3)

        message = str(ctx.exception)
        self.assertIn("当前页面不是邮箱验证码页", message)
        self.assertIn("Failed to create account", message)
        self.assertIn("create-account/password", message)

    def test_stays_silent_when_page_state_cannot_be_probed(self):
        """驱动异常/测试桩探测不了页面时保持沉默，不改变原有流程。

        Page-state reader dùng chung là total (không raise): kết quả không phải
        dict bị coerce thành state có error — _ensure_email_otp_page_ready vẫn
        phải im lặng với loại state này.
        """
        clock = _FakeClock()
        driver = Mock()
        driver.current_url = Mock()
        driver.execute_script.return_value = "navigation_in_progress"  # non-dict → coerced

        with patch.object(registration_flow, "time", clock), patch.object(
            registration_flow, "_check_manual_stop"
        ):
            registration_flow._ensure_email_otp_page_ready(driver, timeout=3)


class SignupCreateRejectionTests(unittest.TestCase):
    """密码页 'Failed to create account' 拒绝必须被共享 flow 原样抛出。"""

    def test_detects_failed_to_create_account_error(self):
        state = {
            "url": "https://auth.openai.com/create-account/password",
            "errors": ["Failed to create account. Please try again"],
        }
        self.assertEqual(
            registration_flow._password_submission_error(Mock(), state),
            "拒绝创建账号: Failed to create account. Please try again",
        )

    def test_detects_failed_to_create_account_in_page_text(self):
        state = {"url": "https://auth.openai.com/create-account/password", "text": "failed to create account"}
        self.assertEqual(
            registration_flow._password_submission_error(Mock(), state),
            "failed to create account",
        )

    def test_ignores_non_signup_pages_and_invalid_state(self):
        driver = Mock()
        self.assertIsNone(registration_flow._password_submission_error(driver, {"errors": ["Other"], "url": "https://chatgpt.com/"}))
        self.assertIsNone(registration_flow._password_submission_error(driver, None))


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
            registration_flow, "_is_email_verification_page", return_value=False
        ), patch.object(
            registration_flow, "_has_access_token", return_value=False
        ), patch.object(
            registration_flow, "_is_signup_password_page", return_value=True
        ), patch.object(
            registration_flow, "_is_login_password_page", return_value=False
        ), patch.object(
            registration_flow, "_password_page_state", return_value=rejected_state
        ), patch.object(
            registration_flow, "_registration_password", return_value="Secret12345678"
        ), patch.object(
            registration_flow, "_raise_if_account_unusable"
        ), patch.object(
            browser_failure_policy, "raise_if_account_unusable"
        ), patch.object(
            registration_flow, "_human_type_text"
        ), patch.object(
            registration_flow, "_human_click"
        ), patch.object(
            registration_flow, "human_delay"
        ), patch.object(
            registration_flow, "time", clock
        ), self.assertRaises(RuntimeError) as ctx:
            registration_flow._fill_password_page_if_present(driver, "user@example.com")

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
            self.assertFalse(hasattr(registration_flow, symbol), symbol)


class AboutYouCheckpointTests(unittest.TestCase):
    """about-you 提交后先落检查点：session 被拦时账号/alias 不再变孤儿。"""

    def setUp(self):
        # Cách ly DB thật: flow checkpoint/codex có thể ghi accounts — không
        # được rò row rác (user@example.com) vào turb.sqlite3 sống.
        import tempfile
        from pathlib import Path

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for name, value in (
            ("_ACCOUNTS_JSON", root / "accounts.json"),
            ("_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
            ("_JOBS_JSON", root / "jobs.json"),
            ("_LEGACY_JOBS_JSON", root / "legacy-jobs.json"),
            ("_LOG_DIR", root / "logs"),
            ("_ACCOUNTS_TXT", root / "accounts.txt"),
            ("_TOKENS_TXT", root / "tokens.txt"),
            ("_VIEWER_HTML", root / "viewer.html"),
        ):
            patcher = patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_with_session_failure(self, recover_side_effect):
        from contextlib import ExitStack

        driver = Mock()
        opened = SimpleNamespace(
            profile_id="test",
            raw={},
            debugger_address="127.0.0.1:9222",
            webdriver_url="",
        )
        client = Mock()
        client.open_profile.return_value = opened
        recover_patch = patch(
            "core.roxy_registration._recover_chatgpt_session",
            side_effect=(
                recover_side_effect
                if isinstance(recover_side_effect, BaseException)
                or callable(recover_side_effect)
                else None
            ),
            return_value=(
                recover_side_effect
                if not (isinstance(recover_side_effect, BaseException) or callable(recover_side_effect))
                else Mock()
            ),
        )
        stack = ExitStack()
        self.addCleanup(stack.close)
        checkpoint = None
        for target, kwargs in (
            ("core.roxy_registration._twofa_cfg.ENABLE_2FA", {"new": True}),
            ("core.roxy_registration.RoxyBrowserClient", {"return_value": client}),
            ("core.roxy_registration._build_driver", {"return_value": driver}),
            ("core.roxy_registration.SeleniumTrafficTracker", {"side_effect": RuntimeError("no tracker")}),
            ("core.registration_network_identity.probe_browser_geo", {"return_value": {}}),
            ("core.registration_network_identity.probe_browser_public_ip", {"return_value": "1.2.3.4"}),
            ("core.roxy_registration._center_browser_window", {}),
            ("core.browser_page_actions._safe_get", {}),
            ("core.browser_page_actions._page_warmup", {}),
            ("core.registration_flow._maybe_accept", {}),
            ("core.registration_flow._check_manual_stop", {}),
            ("core.registration_flow.human_delay", {}),
            ("core.registration_flow._submit_email_and_wait_next", {"return_value": "password"}),
            ("core.registration_flow._fill_password_page_if_present", {"return_value": "Secret123!"}),
            ("core.roxy_registration._complete_email_otp", {}),
            ("core.registration_flow._complete_profile_page", {"return_value": True}),
            ("core.registration_flow._read_chatgpt_session_once", {"side_effect": _original_session_timeout_error()}),
            ("core.roxy_registration.resolve_email_source", {"return_value": "gmail_api_url"}),
            ("core.registration_flow.checkpoint_account_data", {"return_value": 7}),
            ("core.roxy_registration.acquire_email_after_input", {"side_effect": lambda value: value}),
            ("core.account_export.setup_2fa_for_registration", {"return_value": "TOTPSECRET"}),
            ("core.roxy_registration.db.update_account_2fa", {}),
        ):
            stack.enter_context(patch(target, **kwargs))
        stack.enter_context(recover_patch)
        checkpoint = stack.enter_context(patch("core.registration_flow.checkpoint_account_data", return_value=7))
        result = roxy_registration.run_roxy_registration(
            email="user@example.com", name="Test", birthday="1990-01-01",
        )
        return result, checkpoint


    def test_session_failure_after_about_you_returns_recoverable_account(self):
        # session 恢复（重读/刷新/re-auth）全部失败：账号已在服务端创建过
        # about-you，必须返回 twofa=pending 让队列走 login-retry，而不是
        # raise 烧掉 alias（Gap B：WARNING_BANNER 孤儿账号问题）。
        result, checkpoint = self._run_with_session_failure(
            _original_session_timeout_error()
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], 7)
        self.assertEqual(result["twofa_status"], "pending")
        self.assertEqual(result["email"], "user@example.com")
        # about-you 后的早检查点必须已保存（无 accessToken，靠 password 恢复）。
        checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.kwargs["access_token"], "")
        extra = checkpoint.call_args.kwargs["extra"]
        self.assertEqual(extra["registration_password"], "Secret123!")

    def test_session_recovery_success_promotes_checkpoint_with_token(self):
        # S1 恢复成功：token 检查点要在早检查点之上更新（同一账号，带 token）。
        result, checkpoint = self._run_with_session_failure({"accessToken": "tok", "user": {}, "account": {}})

        self.assertEqual(result["twofa_status"], "active")
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual(checkpoint.call_args.kwargs["access_token"], "tok")
        # 早检查点不再保留 session_pending 标记。
        self.assertNotIn("session_pending", checkpoint.call_args.kwargs["extra"])


if __name__ == "__main__":
    unittest.main()
