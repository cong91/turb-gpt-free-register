"""Tests cho account-unusable probe chain + release-status policy superset (P3)."""
import unittest
from unittest.mock import Mock, patch

from core import browser_failure_policy as bfp
from core import browser_twofa_login, registration_flow
from core.browser_failure_policy import (
    account_unusable_page_code,
    is_account_unusable_failure,
    raise_if_account_unusable,
    release_status_for_failure,
    wait_after_password_submit,
)
from core.openai_auth import AccountUnusableError


class _DeadAccountDriver:
    """Trang auth hiển thị thông báo tài khoản đã bị vô hiệu hóa."""

    current_url = "https://auth.openai.com/log-in/password"

    def __init__(self, body="Your account has been deactivated. error_code: account_deactivated"):
        self.body = body

    def execute_script(self, script, *args):
        if "document.body" in script:
            return self.body
        return {}


class _LiveAccountDriver:
    current_url = "https://auth.openai.com/create-account/password"

    def execute_script(self, script, *args):
        if "document.body" in script:
            return "Create a password"
        return {}


class ProbeChainTests(unittest.TestCase):
    def test_probe_reads_page_body_and_maps_to_error_code(self):
        self.assertEqual(account_unusable_page_code(_DeadAccountDriver()), "account_deactivated")
        self.assertEqual(account_unusable_page_code(_LiveAccountDriver()), "")

    def test_probe_swallows_selenium_webdriver_exception(self):
        from selenium.common.exceptions import WebDriverException

        class _BrokenDriver:
            current_url = "https://auth.openai.com/"

            def execute_script(self, script, *args):
                raise WebDriverException("session deleted")

        self.assertEqual(account_unusable_page_code(_BrokenDriver()), "")

    def test_raise_if_account_unusable_raises_with_error_code(self):
        with self.assertRaises(AccountUnusableError) as ctx:
            raise_if_account_unusable(_DeadAccountDriver())
        self.assertEqual(ctx.exception.error_code, "account_deactivated")
        self.assertIn("account_deactivated", str(ctx.exception))

    def test_raise_if_account_unusable_passes_on_live_account(self):
        self.assertIsNone(raise_if_account_unusable(_LiveAccountDriver()))

    def test_wait_after_password_submit_stops_on_unusable_page(self):
        driver = _DeadAccountDriver()
        with patch.object(bfp.time, "sleep") as sleep, self.assertRaises(AccountUnusableError):
            wait_after_password_submit(driver, initial_url="https://auth.openai.com/create-account/password", timeout=5)
        sleep.assert_not_called()

    def test_wait_after_password_submit_returns_on_navigation(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/email-verification"
        driver.execute_script.return_value = "Enter code"
        with patch.object(bfp, "raise_if_account_unusable"), patch.object(bfp.time, "sleep"):
            wait_after_password_submit(
                driver,
                initial_url="https://auth.openai.com/create-account/password",
                timeout=5,
            )


class ReleaseStatusPolicyTests(unittest.TestCase):
    def test_unusable_exception_disables_email(self):
        exc = AccountUnusableError("OpenAI đã khóa tài khoản (account_deactivated)", error_code="account_deactivated")
        self.assertEqual(release_status_for_failure(exc), "disabled")

    def test_dead_code_text_disables_email(self):
        self.assertEqual(release_status_for_failure(RuntimeError("account_deactivated")), "disabled")
        self.assertEqual(release_status_for_failure(RuntimeError("x account_deleted y")), "disabled")
        self.assertEqual(release_status_for_failure(RuntimeError("x account_banned y")), "disabled")

    def test_unsupported_email_disables_email(self):
        self.assertEqual(
            release_status_for_failure(RuntimeError("about-you 提交失败: this email is not supported")),
            "disabled",
        )

    def test_login_password_page_fails_email(self):
        self.assertEqual(
            release_status_for_failure(
                RuntimeError("邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url=x")
            ),
            "failed",
        )

    def test_create_acknowledged_fails_email(self):
        self.assertEqual(
            release_status_for_failure(RuntimeError("proxy timeout"), create_acknowledged=True),
            "failed",
        )

    def test_transient_error_returns_email_to_pool(self):
        self.assertEqual(release_status_for_failure(RuntimeError("navigation timeout")), "available")


class SharedRegistrationFlowProbeTests(unittest.TestCase):
    """Both registration and existing-account browser flows use the P3 probe."""

    def test_registration_flow_raises_unusable_account_before_password_actions(self):
        driver = _DeadAccountDriver()
        with patch.object(registration_flow, "_is_email_verification_page", return_value=False), \
                patch.object(registration_flow, "_is_login_password_page", return_value=False), \
                patch.object(registration_flow, "_is_signup_password_page", return_value=True), \
                patch.object(registration_flow, "_password_page_state", return_value={
                    "url": "https://auth.openai.com/create-account/password",
                }), \
                patch.object(registration_flow, "_registration_password", return_value="Secret12345678"), \
                patch.object(registration_flow, "_human_type_text") as type_text, \
                self.assertRaises(AccountUnusableError):
            registration_flow._fill_password_page_if_present(driver, "user@example.com", timeout=5)
        type_text.assert_not_called()

    def test_existing_account_browser_login_raises_unusable_account(self):
        driver = _DeadAccountDriver()
        with patch.object(browser_twofa_login, "_find_login_password_controls") as controls, \
                self.assertRaises(AccountUnusableError):
            browser_twofa_login._login_password(driver, "secret", timeout=1)
        controls.assert_not_called()

    def test_roxy_release_policy_disables_new_account_unusable_exception(self):
        exc = AccountUnusableError("OpenAI 已锁定 account_deactivated", error_code="account_deactivated")
        with patch("core.email_provider.release_email") as release:
            registration_flow.release_registration_email_on_failure(exc, "user@example.com")
        release.assert_called_once_with(
            "user@example.com",
            status="disabled",
            note="OpenAI đã khóa tài khoản",
        )



class PolicyEquivalenceTests(unittest.TestCase):
    """Policy superset phải giống hệt nhánh disabled của registration_flow cũ."""

    def test_disabled_branch_matches_old_browser_lane_rules(self):
        cases = [
            (AccountUnusableError("x", error_code="account_deactivated"), False, "disabled"),
            (RuntimeError("account_banned"), False, "disabled"),
            (RuntimeError("about-you 提交失败：email is not supported"), False, "disabled"),
            # Superset: error TEXT nhúng "AccountUnusableError" (wrapped/re-raise)
            # cũng disable — khớp với _should_disable_failed_registration_email.
            (RuntimeError("RuntimeError: AccountUnusableError: OpenAI đã khóa tài khoản"), False, "disabled"),
            (RuntimeError("邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url=y"), False, "failed"),
            (RuntimeError("z auth.openai.com/log-in/password"), False, "failed"),
            (RuntimeError("z /log-in/password"), False, "failed"),
            (RuntimeError("network reset"), False, "available"),
        ]
        for exc, ack, expected in cases:
            with self.subTest(exc=str(exc)[:60]):
                self.assertEqual(release_status_for_failure(exc, create_acknowledged=ack), expected)

    def test_is_account_unusable_failure_matches_disable_predicates(self):
        self.assertTrue(is_account_unusable_failure(AccountUnusableError("x")))
        self.assertTrue(is_account_unusable_failure(RuntimeError("account_deactivated")))
        self.assertFalse(is_account_unusable_failure(RuntimeError("proxy timeout")))
        self.assertFalse(is_account_unusable_failure(None))


if __name__ == "__main__":
    unittest.main()
