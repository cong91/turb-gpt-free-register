"""Regression: registration must always force password creation, never OTP-only."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import (
    browser_registration,
    browser_use_registration,
    cloakbrowser_registration,
    roxy_registration,
)
from core.account_export import BrowserPageTransport
from core.openai_auth import AccountUnusableError


class ForcePasswordFlowTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise password-page navigation, not post-registration automation.
        self._config_patches = ExitStack()
        self._config_patches.enter_context(
            patch("config.register.AUTO_PLAN_CHECK_AFTER_REGISTER", False)
        )
        self._config_patches.enter_context(
            patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", False)
        )
        self._config_patches.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
        self._config_patches.enter_context(
            patch("core.browser_registration.post_register_dwell")
        )
        self.addCleanup(self._config_patches.close)

    def test_password_submit_error_never_falls_through_to_otp(self):
        """A password-page error must stop before the caller consumes an OTP."""
        driver = Mock(current_url="https://auth.openai.com/create-account/password")
        clock = iter((0.0, 0.0, 0.0, 21.0))

        with patch("core.browser_registration._raise_if_account_unusable"), \
            patch("core.browser_registration._is_email_verification_page", return_value=False), \
            patch("core.browser_registration._password_page_state", return_value={  # noqa: SIM117
                "url": "https://auth.openai.com/create-account/password",
            }), \
            patch("core.browser_registration._is_signup_password_page", return_value=True), \
            patch("core.browser_registration._registration_password", return_value="Secret123!"), \
            patch("core.browser_registration._human_type_text"), \
            patch("core.browser_registration.human_delay"), \
            patch("core.browser_registration._human_click"), \
            patch("core.browser_registration._wait_after_password_submit"), \
            patch("core.browser_registration._has_access_token", return_value=False), \
            patch("core.browser_registration._page_snapshot", return_value={
                "url": "https://auth.openai.com/create-account/password",
                "errors": ["Không tạo được tài khoản. Vui lòng thử lại."],
                "text": "Tạo mật khẩu Không tạo được tài khoản. Vui lòng thử lại.",
            }), \
            patch("core.browser_registration.time.time", side_effect=clock), \
            patch("core.browser_registration.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "密码页提交失败"):
                browser_registration._fill_password_page_if_present(
                    driver, "user@example.com", timeout=25,
                )

    def test_cloak_auto_codex_passes_registration_password_and_totp_to_oauth(self):
        """Cloak registration must use password + authenticator TOTP when available."""
        driver = type("BrowserSeleniumDriver", (Mock,), {})()
        opened = SimpleNamespace(profile_id="cloak-test", raw={})
        auto_codex_kwargs = {}
        otp_events = []

        def run_auto_codex(**kwargs):
            auto_codex_kwargs.update(kwargs)
            return {"plan": {"ok": True}, "codex": kwargs["run_codex"]()}

        with ExitStack() as stack:
            stack.enter_context(patch("core.cloakbrowser_registration.build_cloak_driver", return_value=(driver, opened)))
            stack.enter_context(patch("core.cloakbrowser_registration._twofa_cfg.ENABLE_2FA", True))
            stack.enter_context(patch("config.register.AUTO_CODEX_FOR_FREE_AFTER_REGISTER", True))
            stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", True))
            stack.enter_context(patch("core.cloakbrowser_registration._safe_get"))
            stack.enter_context(patch("core.cloakbrowser_registration._maybe_accept"))
            stack.enter_context(patch("core.cloakbrowser_registration._check_manual_stop"))
            stack.enter_context(patch("core.cloakbrowser_registration._submit_email_and_wait_next", return_value="password"))
            stack.enter_context(patch("core.cloakbrowser_registration._fill_password_page_if_present", return_value="Secret123!"))
            stack.enter_context(patch("core.cloakbrowser_registration._complete_profile_page", return_value=True))
            fetch_session = stack.enter_context(
                patch(
                    "core.cloakbrowser_registration._fetch_chatgpt_session",
                    return_value={"accessToken": "tok", "user": {}, "account": {}},
                )
            )
            stack.enter_context(
                patch(
                    "core.cloakbrowser_registration.wait_for_otp",
                    side_effect=lambda *args, **kwargs: otp_events.append("otp") or "123456",
                )
            )
            stack.enter_context(
                patch(
                    "core.cloakbrowser_registration._wait_for_otp_inputs",
                    side_effect=lambda *args, **kwargs: otp_events.append("ready") or {"inputs": [{}]},
                )
            )
            stack.enter_context(patch("core.cloakbrowser_registration._clear_otp_inputs"))
            stack.enter_context(patch("core.cloakbrowser_registration._type_otp"))
            stack.enter_context(patch("core.cloakbrowser_registration._click_continue"))
            stack.enter_context(patch("core.cloakbrowser_registration._wait_after_email_otp_submit", return_value="accepted"))
            stack.enter_context(patch("core.cloakbrowser_registration.checkpoint_account_data", return_value=7))
            setup_2fa = stack.enter_context(
                patch("core.account_export.setup_2fa_for_registration", return_value="JBSWY3DPEHPK3PXP")
            )
            stack.enter_context(patch("core.cloakbrowser_registration.db.update_account_2fa"))
            stack.enter_context(patch("core.registration_auto_codex.run_registration_auto_codex", side_effect=run_auto_codex))
            run_codex = stack.enter_context(
                patch("core.roxy_codex_oauth.run_roxy_codex_oauth", return_value={"ok": True, "status": "success"})
            )
            stack.enter_context(patch("core.cloakbrowser_registration.save_account_data", return_value=8))
            stack.enter_context(patch("core.cloakbrowser_registration.resolve_email_source", return_value="outlook"))
            stack.enter_context(patch("core.cloakbrowser_registration.post_register_dwell"))
            stack.enter_context(patch("core.cloakbrowser_registration.human_delay"))
            stack.enter_context(patch("core.cloakbrowser_registration._cfg.CLOAK_KEEP_BROWSER_OPEN", True))
            result = cloakbrowser_registration.run_cloak_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertTrue(result["success"])
        credentials = run_codex.call_args.kwargs["credentials"]
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "Secret123!")
        self.assertEqual(credentials.totp_secret, "JBSWY3DPEHPK3PXP")
        self.assertIs(auto_codex_kwargs["browser_transport"].driver, driver)
        self.assertEqual(fetch_session.call_args.kwargs["auto_jump_wait"], 45)
        setup_2fa.assert_called_once()
        self.assertIsInstance(setup_2fa.call_args.args[0], BrowserPageTransport)
        self.assertIs(setup_2fa.call_args.args[0].driver, driver)
        self.assertEqual(otp_events[:2], ["ready", "otp"])

    def test_browser_use_password_page_never_switches_to_passwordless_otp(self):
        """Browser Use must fill registration password instead of selecting OTP-only."""
        page = Mock()
        with patch("core.browser_use_registration._quick_auth_state", return_value={"state": "password", "url": "https://auth.openai.com/create-account/password"}), \
            patch("core.browser_use_registration._browser_use_heartbeat", return_value=page), \
            patch("core.browser_use_registration._click_passwordless_signup_if_present", return_value=True) as passwordless, \
            patch("core.browser_use_registration._registration_password", return_value="Secret123!"), \
            patch("core.browser_use_registration._fill_first", return_value=True) as fill_first, \
            patch("core.browser_use_registration._click_first", return_value=True), \
            patch("core.browser_use_registration._bu_delay"), \
            patch("core.browser_use_registration._human_pause"), \
            patch("core.browser_use_registration._fast_mode", return_value=False):
            result = browser_use_registration._fill_password_if_present(
                page, "user@example.com", timeout=5,
            )

        self.assertEqual(result, "Secret123!")
        passwordless.assert_not_called()
        fill_first.assert_called_once()

    def test_browser_use_email_verification_state_switches_to_password_page(self):
        """Direct OTP transitions must be redirected to password creation first."""
        page = Mock()
        with patch(
            "core.browser_use_registration._quick_auth_state",
            side_effect=[
                {"state": "email_verification", "url": "https://auth.openai.com/email-verification"},
                {"state": "password", "url": "https://auth.openai.com/create-account/password"},
                {"state": "password", "url": "https://auth.openai.com/create-account/password"},
            ],
        ), \
            patch("core.browser_use_registration._browser_use_heartbeat", return_value=page), \
            patch("core.browser_use_registration._click_continue_with_password_link", return_value=True) as click_password, \
            patch("core.browser_use_registration._registration_password", return_value="Secret123!"), \
            patch("core.browser_use_registration._fill_first", return_value=True), \
            patch("core.browser_use_registration._click_first", return_value=True), \
            patch("core.browser_use_registration._bu_delay"), \
            patch("core.browser_use_registration._human_pause"), \
            patch("core.browser_use_registration._fast_mode", return_value=False):
            result = browser_use_registration._fill_password_if_present(
                page, "user@example.com", timeout=5,
            )

        self.assertEqual(result, "Secret123!")
        click_password.assert_called_once_with(page)

    def test_cloak_password_step_is_forced_when_email_transition_reports_otp(self):
        """Cloak must switch from OTP-only state to password creation first."""
        driver = Mock()
        opened = Mock(profile_id="cloak-test", raw={})
        call_order = []

        def click_password_link(current_driver):
            call_order.append(("click_password_link", current_driver))

        def fill_password(current_driver, email, timeout=25):
            call_order.append(("fill_password", current_driver, email, timeout))
            return "Secret123!"

        with patch("core.cloakbrowser_registration.build_cloak_driver", return_value=(driver, opened)), \
            patch("core.cloakbrowser_registration._twofa_cfg.ENABLE_2FA", False), \
            patch("core.cloakbrowser_registration._submit_email_and_wait_next", return_value="otp"), \
            patch("core.cloakbrowser_registration._click_continue_with_password_link", side_effect=click_password_link), \
            patch("core.cloakbrowser_registration._fill_password_page_if_present", side_effect=fill_password), \
            patch("core.cloakbrowser_registration._maybe_accept"), \
            patch("core.cloakbrowser_registration._check_manual_stop"), \
            patch("core.cloakbrowser_registration._complete_profile_page", return_value=True), \
            patch("core.cloakbrowser_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}}), \
            patch("core.cloakbrowser_registration._wait_after_email_otp_submit", return_value="accepted"), \
            patch("core.cloakbrowser_registration.wait_for_otp", return_value="123456"), \
            patch("core.cloakbrowser_registration._clear_otp_inputs"), \
            patch("core.cloakbrowser_registration._type_otp"), \
            patch("core.cloakbrowser_registration._click_continue"), \
            patch("core.cloakbrowser_registration.checkpoint_account_data", return_value=7), \
            patch("core.cloakbrowser_registration.save_account_data", return_value=7), \
            patch("core.cloakbrowser_registration.resolve_email_source", return_value="paymesh"), \
            patch("core.cloakbrowser_registration.post_register_dwell"), \
            patch("core.cloakbrowser_registration.human_delay"), \
            patch("core.cloakbrowser_registration._cfg.CLOAK_KEEP_BROWSER_OPEN", True):
            result = cloakbrowser_registration.run_cloak_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertTrue(result["success"])
        self.assertEqual(call_order[0], ("click_password_link", driver))
        self.assertEqual(call_order[1], ("fill_password", driver, "user@example.com", 25))

    def test_cloak_password_step_is_forced_when_transition_reports_logged_in_on_otp_page(self):
        """A transient session token must not bypass signup password creation."""
        driver = Mock()
        opened = Mock(profile_id="cloak-test", raw={})
        call_order = []

        def click_password_link(current_driver):
            call_order.append(("click_password_link", current_driver))

        def fill_password(current_driver, email, timeout=25):
            call_order.append(("fill_password", current_driver, email, timeout))
            return "Secret123!"

        with patch("core.cloakbrowser_registration.build_cloak_driver", return_value=(driver, opened)), \
            patch("core.cloakbrowser_registration._twofa_cfg.ENABLE_2FA", False), \
            patch("core.cloakbrowser_registration._submit_email_and_wait_next", return_value="logged_in"), \
            patch("core.cloakbrowser_registration._is_email_verification_page", return_value=True), \
            patch("core.cloakbrowser_registration._click_continue_with_password_link", side_effect=click_password_link), \
            patch("core.cloakbrowser_registration._fill_password_page_if_present", side_effect=fill_password), \
            patch("core.cloakbrowser_registration._maybe_accept"), \
            patch("core.cloakbrowser_registration._check_manual_stop"), \
            patch("core.cloakbrowser_registration._complete_profile_page", return_value=True), \
            patch("core.cloakbrowser_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}}), \
            patch("core.cloakbrowser_registration._wait_after_email_otp_submit", return_value="accepted"), \
            patch("core.cloakbrowser_registration.wait_for_otp", return_value="123456"), \
            patch("core.cloakbrowser_registration._clear_otp_inputs"), \
            patch("core.cloakbrowser_registration._type_otp"), \
            patch("core.cloakbrowser_registration._click_continue"), \
            patch("core.cloakbrowser_registration.checkpoint_account_data", return_value=7), \
            patch("core.cloakbrowser_registration.save_account_data", return_value=7), \
            patch("core.cloakbrowser_registration.resolve_email_source", return_value="paymesh"), \
            patch("core.cloakbrowser_registration.post_register_dwell"), \
            patch("core.cloakbrowser_registration.human_delay"), \
            patch("core.cloakbrowser_registration._cfg.CLOAK_KEEP_BROWSER_OPEN", True):
            result = cloakbrowser_registration.run_cloak_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertTrue(result["success"])
        self.assertEqual(call_order[0], ("click_password_link", driver))
        self.assertEqual(call_order[1], ("fill_password", driver, "user@example.com", 25))

    def test_cloak_deactivated_account_disables_email_pool_entry(self):
        driver = Mock()
        opened = Mock(profile_id="cloak-test", raw={})
        error = AccountUnusableError("账号已废（account_deactivated）", error_code="account_deactivated")

        with patch("core.cloakbrowser_registration.build_cloak_driver", return_value=(driver, opened)), \
            patch("core.cloakbrowser_registration._safe_get"), \
            patch("core.cloakbrowser_registration._submit_email_and_wait_next", side_effect=error), \
            patch("core.cloakbrowser_registration._maybe_accept"), \
            patch("core.cloakbrowser_registration._check_manual_stop"), \
            patch("core.cloakbrowser_registration.human_delay"), \
            patch("core.cloakbrowser_registration._cfg.CLOAK_KEEP_BROWSER_OPEN", True), \
            patch("core.email_provider.release_email") as release_email:
            result = cloakbrowser_registration.run_cloak_registration(
                email="user@example.com", name="Test", birthday="1990-01-01",
            )

        self.assertFalse(result["success"])
        release_email.assert_called_once()
        self.assertEqual(release_email.call_args.kwargs["status"], "disabled")

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
    @patch("core.roxy_registration.checkpoint_account_data", return_value=7)
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}})
    @patch("core.roxy_registration._complete_profile_page", return_value=True)
    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration.wait_for_otp", return_value="123456")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    def test_password_step_always_called_even_when_next_state_is_otp(
        self, _clear, _type, _click, _wait_otp, _wait_after, _profile, _fetch, _checkpoint, _resolve,
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
    @patch("core.roxy_registration.checkpoint_account_data", return_value=7)
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}})
    @patch("core.roxy_registration._complete_profile_page", return_value=True)
    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration.wait_for_otp", return_value="123456")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    def test_force_password_clicks_continue_with_password_before_otp(
        self, _clear, _type, _click, _wait_otp, _wait_after, _profile, _fetch, _checkpoint, _resolve,
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
    @patch("core.roxy_registration._twofa_cfg.ENABLE_2FA", True)
    @patch("core.roxy_registration._fill_password_page_if_present", return_value="Secret123!")
    @patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp")
    @patch("core.roxy_registration._build_driver")
    @patch("core.roxy_registration.RoxyBrowserClient")
    @patch("core.roxy_registration.human_delay")
    @patch("core.roxy_registration._maybe_accept")
    @patch("core.roxy_registration._center_browser_window")
    @patch("core.roxy_registration._check_manual_stop")
    @patch("core.roxy_registration.db.update_account_2fa")
    @patch("core.roxy_registration.checkpoint_account_data", return_value=41)
    @patch("core.roxy_registration.resolve_email_source", return_value="paymesh")
    @patch("core.account_export.setup_2fa_for_registration", side_effect=RuntimeError("script timeout"))
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "tok", "user": {}, "account": {}})
    @patch("core.roxy_registration._complete_profile_page", return_value=True)
    @patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted")
    @patch("core.roxy_registration.wait_for_otp", return_value="123456")
    @patch("core.roxy_registration._click_continue")
    @patch("core.roxy_registration._type_otp")
    @patch("core.roxy_registration._clear_otp_inputs")
    def test_twofa_failure_returns_persisted_partial_account(
        self, _clear, _type, _click, _wait_otp, _wait_after, _profile, _fetch,
        _setup_twofa, _resolve, checkpoint, update_twofa, _check_stop, _center,
        _maybe, _human, _client_cls, _build, _submit, _fill_pwd,
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

        result = roxy_registration.run_roxy_registration(
            email="user@example.com", name="Test", birthday="1990-01-01",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["account_id"], 41)
        self.assertEqual(result["twofa_status"], "failed")
        self.assertIn("script timeout", result["twofa_error"])
        checkpoint.assert_called_once()
        _setup_twofa.assert_called_once_with(driver, "user@example.com")
        update_twofa.assert_called_with(41, status="failed", error=result["twofa_error"])


if __name__ == "__main__":
    unittest.main()
