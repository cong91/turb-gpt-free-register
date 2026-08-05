# -*- coding: utf-8 -*-
"""Regression: registration must always force password creation, never OTP-only."""
import unittest
from unittest.mock import Mock, patch, call

from core import roxy_registration


class ForcePasswordFlowTests(unittest.TestCase):
    @patch("core.roxy_registration._twofa_cfg.ENABLE_2FA", False)
    @patch("core.roxy_registration._fill_password_page_if_present", return_value="Secret123!")
    @patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp")
    @patch("core.roxy_registration._build_driver")
    @patch("core.roxy_registration.RoxyBrowserClient")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration._center_browser_window")
    @patch("core.roxy_registration._check_manual_stop")
    @patch("core.roxy_registration.save_account_data")
    @patch("core.roxy_registration.resolve_email_source", return_value="paymesh")
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}})
    @patch("core.roxy_registration._complete_profile_page", return_value=True)
    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration.wait_for_otp", return_value="123456")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    def test_password_step_always_called_even_when_next_state_is_otp(
        self, _clear, _type, _click, _wait_otp, _wait_after, _profile, _fetch, _resolve,
        _save, _check_stop, _center, _maybe, _human, _client_cls, _build, _submit, fill_pwd,
    ):
        """Khi _submit_email_and_wait_next trả 'otp', run_roxy_registration vẫn phải gọi
        _fill_password_page_if_present để force password, không bỏ qua."""
        driver = Mock()
        _build.return_value = driver
        opened = Mock()
        opened.profile_id = "test"
        opened.debugger_address = "127.0.0.1:9999"
        opened.raw = {}
        client = Mock()
        client.open_profile.return_value = opened
        _client_cls.return_value = client

        from pathlib import Path
        roxy_registration.run_roxy_registration(
            email="user@example.com", name="Test", birthday="1990-01-01",
        )

        # Must always call _fill_password_page_if_present, even when next_state == "otp"
        fill_pwd.assert_called_once()
        self.assertEqual(fill_pwd.call_args.args[1], "user@example.com")

    @patch("core.roxy_registration._click_continue_with_password_link", return_value=True)
    @patch("core.roxy_registration._twofa_cfg.ENABLE_2FA", False)
    @patch("core.roxy_registration._fill_password_page_if_present", return_value="Secret123!")
    @patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp")
    @patch("core.roxy_registration._build_driver")
    @patch("core.roxy_registration.RoxyBrowserClient")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration._center_browser_window")
    @patch("core.roxy_registration._check_manual_stop")
    @patch("core.roxy_registration.save_account_data")
    @patch("core.roxy_registration.resolve_email_source", return_value="paymesh")
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}})
    @patch("core.roxy_registration._complete_profile_page", return_value=True)
    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration.wait_for_otp", return_value="123456")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    def test_force_password_clicks_continue_with_password_before_otp(
        self, _clear, _type, _click, _wait_otp, _wait_after, _profile, _fetch, _resolve,
        _save, _check_stop, _center, _maybe, _human, _client_cls, _build, _submit,
        fill_pwd, click_pwd_link,
    ):
        """Khi vào otp state, phải click 'Continue with password' trước khi fill password."""
        driver = Mock()
        _build.return_value = driver
        opened = Mock()
        opened.profile_id = "test"
        opened.debugger_address = "127.0.0.1:9999"
        opened.raw = {}
        client = Mock()
        client.open_profile.return_value = opened
        _client_cls.return_value = client

        roxy_registration.run_roxy_registration(
            email="user@example.com", name="Test", birthday="1990-01-01",
        )

        # Must call click-continue-with-password before fill_password
        click_pwd_link.assert_called_once_with(driver)

    @patch("core.roxy_registration._cfg.ROXY_KEEP_BROWSER_OPEN", False)
    @patch("core.roxy_registration._fill_password_page_if_present", return_value="Secret123!")
    @patch("core.roxy_registration._submit_email_and_wait_next", return_value="password")
    @patch("core.roxy_registration._build_driver")
    @patch("core.roxy_registration.RoxyBrowserClient")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration._center_browser_window")
    @patch("core.roxy_registration._check_manual_stop")
    @patch("core.roxy_registration.wait_for_otp", side_effect=RuntimeError("otp timeout"))
    def test_password_submission_failure_does_not_reuse_email(
        self, _wait_otp, _check_stop, _center, _maybe, _human, _client_cls, _build, _submit, _fill_pwd,
    ):
        driver = Mock()
        _build.return_value = driver
        opened = Mock()
        opened.profile_id = "test"
        opened.debugger_address = "127.0.0.1:9999"
        opened.raw = {}
        client = Mock()
        client.open_profile.return_value = opened
        _client_cls.return_value = client

        with patch("core.email_provider.release_email") as release_email:
            result = roxy_registration.run_roxy_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertFalse(result["success"])
        release_email.assert_called_once()
        self.assertEqual(release_email.call_args.kwargs["status"], "failed")

    @patch("core.roxy_registration._cfg.ROXY_KEEP_BROWSER_OPEN", False)
    @patch(
        "core.roxy_registration._submit_email_and_wait_next",
        side_effect=RuntimeError("邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url=https://auth.openai.com/log-in/password"),
    )
    @patch("core.roxy_registration._build_driver")
    @patch("core.roxy_registration.RoxyBrowserClient")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration._center_browser_window")
    @patch("core.roxy_registration._check_manual_stop")
    def test_login_password_failure_does_not_reuse_email(
        self, _check_stop, _center, _maybe, _human, _client_cls, _build, _submit,
    ):
        driver = Mock()
        _build.return_value = driver
        opened = Mock()
        opened.profile_id = "test"
        opened.debugger_address = "127.0.0.1:9999"
        opened.raw = {}
        client = Mock()
        client.open_profile.return_value = opened
        _client_cls.return_value = client

        with patch("core.email_provider.release_email") as release_email:
            result = roxy_registration.run_roxy_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertFalse(result["success"])
        release_email.assert_called_once()
        self.assertEqual(release_email.call_args.kwargs["status"], "failed")


if __name__ == "__main__":
    unittest.main()
