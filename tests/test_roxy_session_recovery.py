import unittest
from unittest.mock import ANY, Mock, patch

from core import account_export, registration_service, roxy_registration
from core.rotating_proxy_runtime import TWOFA_RETRY_PROXY_SCOPE


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


class RestartRoxyBrowserTests(unittest.TestCase):
    def test_closes_then_reopens_same_profile_and_rebuilds_driver(self):
        client = Mock()
        reopened = Mock()
        client.reopen_profile.return_value = reopened
        opened = Mock()
        opened.profile_id = "pid-1"
        old_driver, new_driver = Mock(), Mock()

        with patch.object(
            roxy_registration, "_build_driver", return_value=new_driver
        ) as build, patch.object(
            roxy_registration, "_center_browser_window"
        ) as center, patch.object(
            roxy_registration, "time", _FakeClock()
        ):
            out_opened, out_driver = roxy_registration._restart_roxy_browser(
                client, opened, old_driver, proxy=None
            )

        old_driver.quit.assert_called_once_with()
        client.close_profile.assert_called_once_with("pid-1")
        client.reopen_profile.assert_called_once_with("pid-1", proxy=None)
        build.assert_called_once_with(reopened)
        center.assert_called_once_with(new_driver)
        new_driver.set_page_load_timeout.assert_called_once()
        self.assertIs(out_opened, reopened)
        self.assertIs(out_driver, new_driver)

    def test_reopen_failure_propagates(self):
        client = Mock()
        client.reopen_profile.side_effect = RuntimeError("roxy down")
        opened = Mock()
        opened.profile_id = "pid-1"

        with patch.object(
            roxy_registration, "_build_driver"
        ), patch.object(
            roxy_registration, "_center_browser_window"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), self.assertRaises(RuntimeError):
            roxy_registration._restart_roxy_browser(client, opened, Mock())


class ReloginAfterBrowserRestartTests(unittest.TestCase):
    def test_restarts_browser_then_relogins_and_fetches_session(self):
        old_driver, new_driver = Mock(), Mock()
        reopened = Mock()
        token = {"accessToken": "tok", "_http_status": 200}

        with patch.object(
            roxy_registration,
            "_restart_roxy_browser",
            return_value=(reopened, new_driver),
        ) as restart, patch.object(
            roxy_registration, "_relogin_existing_account"
        ) as relogin, patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=token
        ) as fetch:
            out_opened, out_driver, session = roxy_registration._relogin_after_browser_restart(
                Mock(), Mock(), old_driver, None, "user@example.com", "Secret123456"
            )

        restart.assert_called_once()
        relogin.assert_called_once_with(new_driver, "user@example.com", "Secret123456")
        fetch.assert_called_once_with(new_driver, timeout=90, auto_jump_wait=8, fast_fail=False)
        self.assertIs(out_opened, reopened)
        self.assertIs(out_driver, new_driver)
        self.assertEqual(session, token)

    def test_relogin_failure_after_restart_quits_new_driver(self):
        old_driver, new_driver = Mock(), Mock()

        with patch.object(
            roxy_registration, "_restart_roxy_browser", return_value=(Mock(), new_driver)
        ), patch.object(
            roxy_registration,
            "_relogin_existing_account",
            side_effect=RuntimeError("relogin boom"),
        ), patch.object(
            roxy_registration, "_fetch_chatgpt_session"
        ) as fetch, self.assertRaises(RuntimeError) as ctx:
            roxy_registration._relogin_after_browser_restart(
                Mock(), Mock(), old_driver, None, "user@example.com", "Secret123456"
            )

        self.assertIn("relogin boom", str(ctx.exception))
        new_driver.quit.assert_called_once_with()
        fetch.assert_not_called()


class ReloginExistingAccountTests(unittest.TestCase):
    def test_password_page_then_otp_page_both_handled(self):
        """Nhập email xong rơi vào trang mật khẩu → điền mật khẩu → lại gặp
        trang OTP → chờ mail và填写。Hai nhánh nối tiếp phải xử lý được hết。"""
        driver = Mock()

        with patch.object(
            roxy_registration, "_reset_login_page_for_retry"
        ), patch.object(
            roxy_registration, "_has_access_token", side_effect=[False, False, False]
        ), patch.object(
            roxy_registration, "_type_email_address"
        ) as type_email, patch.object(
            roxy_registration, "_submit_email_step"
        ) as submit_email, patch.object(
            roxy_registration, "_is_email_verification_page", side_effect=[False, True]
        ), patch.object(
            roxy_registration, "_is_login_password_page", side_effect=[True]
        ), patch.object(
            roxy_registration, "_submit_login_password_on_page", return_value=True
        ) as submit_pwd, patch.object(
            roxy_registration, "_complete_email_otp"
        ) as complete_otp, patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch.object(
            roxy_registration, "human_delay"
        ):
            roxy_registration._relogin_existing_account(
                driver, "user@example.com", "Secret123456"
            )

        type_email.assert_called_once()
        submit_email.assert_called_once()
        submit_pwd.assert_called_once_with(driver, "Secret123456")
        complete_otp.assert_called_once()

    def test_otp_page_directly_without_password(self):
        """Nhập email xong直接 vào验证码页：只要走 OTP 分支即可。"""
        driver = Mock()

        with patch.object(
            roxy_registration, "_reset_login_page_for_retry"
        ), patch.object(
            roxy_registration, "_has_access_token", side_effect=[False, False]
        ), patch.object(
            roxy_registration, "_type_email_address"
        ), patch.object(
            roxy_registration, "_submit_email_step"
        ), patch.object(
            roxy_registration, "_is_email_verification_page", side_effect=[True]
        ), patch.object(
            roxy_registration, "_is_login_password_page"
        ) as is_pwd, patch.object(
            roxy_registration, "_submit_login_password_on_page"
        ) as submit_pwd, patch.object(
            roxy_registration, "_complete_email_otp"
        ) as complete_otp, patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch.object(
            roxy_registration, "human_delay"
        ):
            roxy_registration._relogin_existing_account(
                driver, "user@example.com", "Secret123456"
            )

        is_pwd.assert_not_called()
        submit_pwd.assert_not_called()
        complete_otp.assert_called_once()

    def test_password_page_without_known_password_raises(self):
        driver = Mock()

        with patch.object(
            roxy_registration, "_reset_login_page_for_retry"
        ), patch.object(
            roxy_registration, "_has_access_token", side_effect=[False, False]
        ), patch.object(
            roxy_registration, "_type_email_address"
        ), patch.object(
            roxy_registration, "_submit_email_step"
        ), patch.object(
            roxy_registration, "_is_email_verification_page", side_effect=[False]
        ), patch.object(
            roxy_registration, "_is_login_password_page", side_effect=[True]
        ), patch.object(
            roxy_registration, "_submit_login_password_on_page"
        ) as submit_pwd, patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch.object(
            roxy_registration, "human_delay"
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._relogin_existing_account(driver, "user@example.com", None)

        self.assertIn("登录密码页需要密码", str(ctx.exception))
        submit_pwd.assert_not_called()

    def test_neither_page_nor_login_state_times_out_with_clear_error(self):
        driver = Mock()

        with patch.object(
            roxy_registration, "_reset_login_page_for_retry"
        ), patch.object(
            roxy_registration, "_has_access_token", return_value=False
        ), patch.object(
            roxy_registration, "_type_email_address"
        ), patch.object(
            roxy_registration, "_submit_email_step"
        ), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=False
        ), patch.object(
            roxy_registration, "_is_login_password_page", return_value=False
        ), patch.object(
            roxy_registration, "_submit_login_password_on_page"
        ) as submit_pwd, patch.object(
            roxy_registration, "_complete_email_otp"
        ) as complete_otp, patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(
            roxy_registration, "time", _FakeClock()
        ), patch.object(
            roxy_registration, "human_delay"
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._relogin_existing_account(
                driver, "user@example.com", "Secret123456"
            )

        self.assertIn("未完成登录", str(ctx.exception))
        submit_pwd.assert_not_called()
        complete_otp.assert_not_called()


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


class RecoverTwofaWithBrowserRestartTests(unittest.TestCase):
    def test_delegates_to_run_twofa_retry_with_rotation(self):
        """补做必须交给 run_twofa_retry：不传代理 → TWOFA_RETRY scope 轮换新 IP。"""
        with patch(
            "core.browser_twofa_retry.run_twofa_retry",
            return_value={"ok": True, "status": "success", "totp_secret": "SECRET"},
        ) as retry:
            status, secret, error = roxy_registration._recover_twofa_with_browser_restart(
                account_id=7,
                email="user@example.com",
                openai_password="Secret123!",
                access_token="tok",
                proxy_used="http://old:1",
            )

        self.assertEqual(status, "active")
        self.assertEqual(secret, "SECRET")
        self.assertIsNone(error)
        retry.assert_called_once()
        account_arg = retry.call_args.args[0]
        self.assertEqual(account_arg["id"], 7)
        self.assertEqual(account_arg["email"], "user@example.com")
        self.assertEqual(account_arg["registration_password"], "Secret123!")
        self.assertEqual(retry.call_args.kwargs.get("max_attempts"), 2)
        self.assertEqual(retry.call_args.kwargs.get("browser_restart_attempts"), 2)
        self.assertNotIn("proxy", retry.call_args.kwargs)

    def test_missing_password_skips_browser_retry(self):
        with patch("core.browser_twofa_retry.run_twofa_retry") as retry:
            status, secret, error = roxy_registration._recover_twofa_with_browser_restart(
                account_id=7,
                email="user@example.com",
                openai_password=None,
                access_token="tok",
                proxy_used=None,
            )

        self.assertEqual(status, "failed")
        self.assertIsNone(secret)
        self.assertIn("缺少注册密码", error)
        retry.assert_not_called()

    def test_retry_failure_returns_message(self):
        with patch(
            "core.browser_twofa_retry.run_twofa_retry",
            return_value={"ok": False, "message": "登录失败"},
        ):
            status, secret, error = roxy_registration._recover_twofa_with_browser_restart(
                account_id=7,
                email="user@example.com",
                openai_password="Secret123!",
                access_token="tok",
                proxy_used=None,
            )

        self.assertEqual(status, "failed")
        self.assertIsNone(secret)
        self.assertIn("登录失败", error)


class ReloginWithFreshIpAndBrowserTests(unittest.TestCase):
    """Ladder tầng 5：同出口 IP 全部失败后，必须换 IP + 全新环境重新登录。"""

    def test_rotates_proxy_and_relogs_in_a_fresh_environment(self):
        client = Mock()
        old_opened, old_driver = Mock(), Mock()
        new_opened, new_driver = Mock(), Mock()
        client.open_profile.return_value = new_opened
        token = {"accessToken": "tok", "_http_status": 200}

        with patch(
            "core.rotating_proxy_runtime.resolve_rotating_proxy",
            return_value="http://new-ip:1",
        ) as resolve, patch(
            "core.rotating_proxy_runtime.release_rotating_proxy"
        ) as release, patch.object(
            roxy_registration, "_build_driver", return_value=new_driver
        ), patch.object(
            roxy_registration, "_center_browser_window"
        ), patch.object(
            roxy_registration, "_relogin_existing_account"
        ) as relogin, patch.object(
            roxy_registration, "_fetch_chatgpt_session", return_value=token
        ) as fetch, patch.object(
            roxy_registration, "_check_manual_stop"
        ):
            out_opened, out_driver, session = (
                roxy_registration._relogin_with_fresh_ip_and_browser(
                    client, old_opened, old_driver, "user@example.com", "Secret123!"
                )
            )

        resolve.assert_called_once_with(None, scope=TWOFA_RETRY_PROXY_SCOPE)
        release.assert_called_once_with(
            scope=TWOFA_RETRY_PROXY_SCOPE, proxy_url="http://new-ip:1"
        )
        old_driver.quit.assert_called_once_with()
        client.cleanup_profile.assert_any_call(old_opened)
        client.open_profile.assert_called_once()
        self.assertEqual(client.open_profile.call_args.kwargs.get("proxy"), "http://new-ip:1")
        relogin.assert_called_once_with(new_driver, "user@example.com", "Secret123!")
        fetch.assert_called_once_with(new_driver, timeout=90, auto_jump_wait=8, fast_fail=False)
        self.assertIs(out_opened, new_opened)
        self.assertIs(out_driver, new_driver)
        self.assertEqual(session, token)

    def test_failure_cleans_up_new_environment_and_releases_lease(self):
        client = Mock()
        old_opened, old_driver = Mock(), Mock()
        new_opened, new_driver = Mock(), Mock()
        client.open_profile.return_value = new_opened

        with patch(
            "core.rotating_proxy_runtime.resolve_rotating_proxy",
            return_value="http://new-ip:1",
        ), patch(
            "core.rotating_proxy_runtime.release_rotating_proxy"
        ) as release, patch.object(
            roxy_registration, "_build_driver", return_value=new_driver
        ), patch.object(
            roxy_registration, "_center_browser_window"
        ), patch.object(
            roxy_registration,
            "_relogin_existing_account",
            side_effect=RuntimeError("relogin boom"),
        ), patch.object(
            roxy_registration, "_fetch_chatgpt_session"
        ) as fetch, patch.object(
            roxy_registration, "_check_manual_stop"
        ), self.assertRaises(RuntimeError) as ctx:
            roxy_registration._relogin_with_fresh_ip_and_browser(
                client, old_opened, old_driver, "user@example.com", "Secret123456"
            )

        self.assertIn("relogin boom", str(ctx.exception))
        new_driver.quit.assert_called_once_with()
        client.cleanup_profile.assert_any_call(new_opened)
        release.assert_called_once_with(
            scope=TWOFA_RETRY_PROXY_SCOPE, proxy_url="http://new-ip:1"
        )
        fetch.assert_not_called()

    def test_missing_password_short_circuits(self):
        client = Mock()

        with patch(
            "core.rotating_proxy_runtime.resolve_rotating_proxy"
        ) as resolve, self.assertRaises(RuntimeError) as ctx:
            roxy_registration._relogin_with_fresh_ip_and_browser(
                client, Mock(), Mock(), "user@example.com", None
            )

        self.assertIn("缺少注册密码", str(ctx.exception))
        resolve.assert_not_called()
        client.open_profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
