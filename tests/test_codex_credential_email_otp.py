"""Offline tests for explicit credential-login email OTP providers."""
import logging
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, call, patch

from config import codex as codex_config
from core import browser_credential_login as local
from core import browser_use_codex_oauth as cloud
from core.codex_login_credentials import CodexLoginCredentials
from core.codex_oauth import run_codex_oauth


class CredentialEmailOtpTests(unittest.TestCase):
    def setUp(self):
        self.credentials = CodexLoginCredentials(
            email="user@example.com", password="password-secret", totp_secret="JBSWY3DPEHPK3PXP"
        )
        self.auth_url = "https://auth.openai.com/oauth/authorize"

    def run_login(self, kind, initial, states, provider):
        page = Mock()
        with ExitStack() as stack:
            if kind == "local":
                stack.enter_context(patch.object(local, "_open_and_submit_email", return_value=initial))
                stack.enter_context(patch.object(local, "_select_matching_account", return_value=False))
                wait = stack.enter_context(patch.object(local, "_wait_for_login_state", side_effect=states))
                password = stack.enter_context(patch.object(local, "_submit_password"))
                otp = stack.enter_context(patch.object(local, "_submit_email_otp"))
                totp = stack.enter_context(patch.object(local, "_submit_totp_code"))
                stack.enter_context(patch.object(local, "generate_totp_code", return_value="654321"))
                local.login_with_credentials(page, self.credentials, self.auth_url, otp_provider=provider)
                fill_email = None
            else:
                stack.enter_context(patch.object(cloud, "_wait_auth_page_ready"))
                stack.enter_context(patch.object(cloud, "_bu_delay"))
                stack.enter_context(patch.object(cloud, "_maybe_accept_cookies"))
                stack.enter_context(patch.object(cloud, "_classify_credential_login_state", return_value=initial))
                fill_email = stack.enter_context(patch.object(cloud, "_fill_email_for_codex"))
                wait = stack.enter_context(patch.object(cloud, "_wait_for_credential_login_state", side_effect=states))
                password = stack.enter_context(patch.object(cloud, "_fill_first_any_frame", return_value=True))
                stack.enter_context(patch.object(cloud, "_click_first_any_frame", return_value=True))
                otp = stack.enter_context(patch.object(cloud, "_submit_credential_email_otp"))
                totp = None
                stack.enter_context(patch.object(cloud, "generate_totp_code", return_value="654321"))
                cloud._login_with_credentials(page, self.credentials, self.auth_url, otp_provider=provider)
        return page, wait, password, otp, totp, fill_email

    def test_initial_after_email_and_after_password_otp(self):
        for kind in ("local", "cloud"):
            cases = (
                ("initial", "email_otp", ["accepted"]),
                ("after_email", "email_submitted" if kind == "local" else "unknown", ["email_otp", "accepted"]),
                ("after_password", "password", ["email_otp", "accepted"]),
            )
            for position, initial, states in cases:
                with self.subTest(adapter=kind, position=position):
                    provider = Mock(return_value="123456")
                    page, wait, password, otp, _, fill_email = self.run_login(kind, initial, states, provider)
                    provider.assert_called_once()
                    self.assertEqual(provider.call_args.args, (self.credentials.email,))
                    if position == "initial":
                        self.assertIsNone(provider.call_args.kwargs["after_ts"])
                    else:
                        self.assertIsInstance(provider.call_args.kwargs["after_ts"], float)
                    otp.assert_called_once_with(page, "123456")
                    provider.acknowledge.assert_called_once_with("123456")
                    self.assertEqual(wait.call_args.kwargs, {"ignored_states": {"email_otp"}})
                    if position == "initial":
                        password.assert_not_called()
                        if fill_email is not None:
                            fill_email.assert_not_called()

    def test_otp_can_advance_to_password_or_authenticator(self):
        for kind in ("local", "cloud"):
            for next_state in ("password", "totp"):
                with self.subTest(adapter=kind, next_state=next_state):
                    provider = Mock(return_value="123456")
                    self.run_login(kind, "email_otp", [next_state, "accepted"], provider)
                    provider.acknowledge.assert_called_once_with("123456")

    def test_ordinary_function_provider_needs_no_acknowledge(self):
        for kind in ("local", "cloud"):
            calls = []

            def provider(email, after_ts=None, *, recorded=calls):
                recorded.append((email, after_ts))
                return "123456"

            with self.subTest(adapter=kind):
                self.run_login(kind, "email_otp", ["accepted"], provider)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], self.credentials.email)

    def test_password_after_submitted_password_is_not_acknowledged(self):
        for kind in ("local", "cloud"):
            with self.subTest(adapter=kind):
                provider = Mock(return_value="123456")
                with self.assertRaises(RuntimeError):
                    self.run_login(kind, "password", ["email_otp", "password"], provider)
                provider.acknowledge.assert_not_called()

    def test_chooser_after_otp_requires_a_confirmed_following_state(self):
        for next_state in ("accepted", "unknown"):
            with self.subTest(next_state=next_state):
                driver = Mock()
                provider = Mock(return_value="123456")
                with ExitStack() as stack:
                    stack.enter_context(patch.object(local, "_open_and_submit_email", return_value="email_otp"))
                    stack.enter_context(patch.object(local, "_wait_for_login_state", side_effect=["account_chooser", next_state]))
                    stack.enter_context(patch.object(local, "_select_matching_account", return_value=True))
                    stack.enter_context(patch.object(local, "_submit_email_otp"))
                    if next_state == "unknown":
                        with self.assertRaises(RuntimeError):
                            local.login_with_credentials(driver, self.credentials, self.auth_url, otp_provider=provider)
                    else:
                        local.login_with_credentials(driver, self.credentials, self.auth_url, otp_provider=provider)
                if next_state == "accepted":
                    provider.acknowledge.assert_called_once_with("123456")
                else:
                    provider.acknowledge.assert_not_called()

    def test_rejected_code_is_retried_once_then_acknowledged(self):
        for kind in ("local", "cloud"):
            with self.subTest(adapter=kind):
                provider = Mock(side_effect=["111111", "222222"])
                page, wait, _, otp, _, _ = self.run_login(
                    kind, "email_otp", ["email_otp_invalid", "accepted"], provider
                )
                self.assertEqual(otp.call_args_list, [call(page, "111111"), call(page, "222222")])
                self.assertEqual(wait.call_args.kwargs, {"ignored_states": {"email_otp", "email_otp_invalid"}})
                provider.acknowledge.assert_called_once_with("222222")
                self.assertEqual(provider.call_args_list[0].kwargs, provider.call_args_list[1].kwargs)

    def test_rejection_timeout_unknown_or_unrelated_state_is_not_acknowledged(self):
        for kind in ("local", "cloud"):
            for result in ("email_otp_invalid", "email_otp", "unknown", "password_invalid", "account_chooser"):
                with self.subTest(adapter=kind, result=result):
                    provider = Mock(side_effect=["111111", "222222"])
                    states = [result, result] if result == "email_otp_invalid" else [result]
                    with self.assertRaises(RuntimeError):
                        self.run_login(kind, "email_otp", states, provider)
                    provider.acknowledge.assert_not_called()
                    self.assertLessEqual(provider.call_count, 2)

    def test_unchanged_otp_after_actual_wait_times_out(self):
        for module, waiter in ((local, local._wait_for_login_state), (cloud, cloud._wait_for_credential_login_state)):
            classify = "classify_login_state" if module is local else "_classify_credential_login_state"
            with self.subTest(adapter=module.__name__), patch.object(module, classify, return_value="email_otp"), patch.object(module.time, "sleep"):
                self.assertEqual(waiter(Mock(), timeout=0.002, ignored_states={"email_otp"}), "email_otp")

    def test_repeated_rejected_code_is_not_resubmitted(self):
        for kind in ("local", "cloud"):
            with self.subTest(adapter=kind):
                provider = Mock(return_value="111111")
                with self.assertRaisesRegex(RuntimeError, "previously rejected"):
                    self.run_login(kind, "email_otp", ["email_otp_invalid"], provider)
                self.assertEqual(provider.call_count, 2)
                provider.acknowledge.assert_not_called()

    def test_no_provider_remains_fail_closed(self):
        for kind in ("local", "cloud"):
            for initial, states in (("email_otp", []), ("password", ["email_otp"])):
                with self.subTest(adapter=kind, initial=initial), self.assertRaisesRegex(RuntimeError, "email OTP"):
                    self.run_login(kind, initial, states, None)

    def test_provider_and_acknowledge_errors_do_not_leak_in_traceback(self):
        for kind in ("local", "cloud"):
            for failing in ("request", "acknowledge"):
                with self.subTest(adapter=kind, failing=failing):
                    provider = Mock(return_value="123456")
                    error = RuntimeError("sensitive-source-url secret-code-123456")
                    if failing == "request":
                        provider.side_effect = error
                    else:
                        provider.acknowledge.side_effect = error
                    with self.assertRaises(RuntimeError) as raised:
                        self.run_login(kind, "email_otp", ["accepted"], provider)
                    self.assertNotIn("sensitive-source-url", str(raised.exception))
                    self.assertTrue(raised.exception.__suppress_context__)
                    if failing == "request":
                        provider.acknowledge.assert_not_called()

    def test_local_submit_supports_single_and_segmented_inputs(self):
        for segmented in (False, True):
            with self.subTest(segmented=segmented):
                driver = Mock()
                elements = [Mock() for _ in range(6)] if segmented else [Mock()]
                for element in elements:
                    element.get_attribute.return_value = "numeric"
                driver.find_elements.return_value = elements

                def write_verified(_driver, _code, writer, _kind):
                    writer()

                with patch.object(local, "_clear_otp_inputs"), patch.object(local, "_visible", return_value=True), patch.object(
                    local, "_type_otp_with_verification", side_effect=write_verified
                ), patch.object(local, "human_delay"), patch.object(local, "_click_any") as click:
                    local._submit_email_otp(driver, "123456")
                click.assert_called_once()
                if segmented:
                    for element, digit in zip(elements, "123456"):
                        element.send_keys.assert_called_once_with(digit)
                else:
                    elements[0].send_keys.assert_called_once_with("123456")

    def test_local_otp_submission_failure_does_not_log_values(self):
        driver = Mock()
        element = Mock()
        element.send_keys.side_effect = RuntimeError("secret-code-123456")
        driver.find_elements.return_value = [element]

        def write_verified(_driver, _code, writer, _kind):
            writer()

        with patch.object(local, "_clear_otp_inputs"), patch.object(local, "_visible", return_value=True), patch.object(
            local, "_type_otp_with_verification", side_effect=write_verified
        ), self.assertNoLogs("core.browser_page_actions", logging.DEBUG), self.assertRaises(RuntimeError) as raised:
            local._submit_email_otp(driver, "123456")
        self.assertNotIn("123456", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_cloud_submit_supports_single_and_segmented_inputs(self):
        for segmented in (False, True):
            with self.subTest(segmented=segmented):
                page = Mock()
                boxes = Mock()
                boxes.count.return_value = 6
                page.locator.return_value = boxes
                single = None if segmented else Mock()
                with patch.object(cloud, "_all_frames", return_value=[page]), patch.object(
                    cloud, "_clear_otp_inputs"
                ), patch.object(cloud, "_visible_locator_any_frame", return_value=single), patch.object(
                    cloud, "_bu_delay"
                ), patch.object(cloud, "_click_first_any_frame", return_value=True):
                    cloud._submit_credential_email_otp(page, "123456")
                if segmented:
                    self.assertEqual(boxes.nth.call_args_list, [call(index) for index in range(6)])
                    self.assertEqual(boxes.nth.return_value.fill.call_count, 6)
                else:
                    single.fill.assert_called_once_with("123456", timeout=5000)

    def test_cloud_sensitive_input_failures_do_not_log_values(self):
        locator = Mock()
        locator.fill.side_effect = RuntimeError("secret-code-123456")
        locator.evaluate.side_effect = RuntimeError("secret-code-123456")
        with patch.object(cloud, "_visible_locator_any_frame", return_value=locator), patch.object(
            cloud.time, "sleep"
        ), self.assertNoLogs(cloud.logger, logging.DEBUG):
            self.assertFalse(cloud._fill_first_any_frame(Mock(), ["input"], "123456", timeout_ms=1, sensitive=True))

    def test_cloud_email_submission_failure_does_not_expose_page_snapshot(self):
        page = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cloud, "_wait_auth_page_ready"))
            stack.enter_context(patch.object(cloud, "_bu_delay"))
            stack.enter_context(patch.object(cloud, "_maybe_accept_cookies"))
            stack.enter_context(patch.object(cloud, "_classify_credential_login_state", return_value="unknown"))
            stack.enter_context(patch.object(cloud, "_fill_email_for_codex", side_effect=RuntimeError("page secret-code-123456")))
            with self.assertRaises(RuntimeError) as raised:
                cloud._login_with_credentials(page, self.credentials, self.auth_url)
        self.assertNotIn("123456", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_snapshot_diagnostics_do_not_log_page_contents(self):
        page = Mock()
        snapshot = {"url": "https://auth.openai.com/secret-code-123456", "body": "secret-code-123456", "inputs": [{"value": "123456"}]}
        with patch.object(local, "_page_state", return_value=snapshot), self.assertLogs(local.logger, logging.WARNING) as logs:
            local._log_state_snapshot(page, "unknown")
        self.assertNotIn("123456", str(logs.output))
        self.assertNotIn("secret-code", str(logs.output))

    def test_classifiers_recognize_email_otp_rejection(self):
        state = {"url": "https://auth.openai.com/email-verification", "body": "Invalid code, try again", "inputs": []}
        driver = Mock(current_url=state["url"], execute_script=Mock(return_value=state))
        self.assertEqual(local.classify_login_state(driver), "email_otp_invalid")
        with patch.object(cloud, "_quick_auth_state", return_value=state), patch.object(cloud, "_body_snippet", return_value=state["body"]):
            self.assertEqual(cloud._classify_credential_login_state(Mock()), "email_otp_invalid")

    def test_initial_local_otp_does_not_type_email(self):
        driver = Mock()
        with patch.object(local, "human_delay"), patch.object(
            local, "classify_login_state", return_value="email_otp"
        ), patch.object(local, "_type_email_address") as fill_email:
            self.assertEqual(local._open_and_submit_email(driver, self.credentials.email, self.auth_url), "email_otp")
        fill_email.assert_not_called()


class CredentialEmailOtpRoutingTests(unittest.TestCase):
    def setUp(self):
        self.credentials = CodexLoginCredentials(
            email="user@example.com", password="password-secret", totp_secret="JBSWY3DPEHPK3PXP"
        )

    def test_dispatcher_forwards_explicit_provider_for_each_browser(self):
        for driver in ("roxy", "cloak", "browser_use", "skyvern"):
            with self.subTest(driver=driver), ExitStack() as stack:
                provider = Mock()
                runner_path = (
                    "core.roxy_codex_oauth.run_roxy_codex_oauth" if driver in {"roxy", "cloak"}
                    else f"core.{driver}_codex_oauth.run_{driver}_codex_oauth"
                )
                runner = stack.enter_context(patch(runner_path, return_value={"ok": True}))
                stack.enter_context(patch.object(codex_config, "CODEX_OAUTH_DRIVER", driver))
                stack.enter_context(patch("core.rotating_proxy_runtime.resolve_rotating_proxy", return_value=None))
                if driver == "cloak":
                    stack.enter_context(patch("core.cloakbrowser_driver.build_cloak_driver", return_value=(Mock(), Mock())))
                result = run_codex_oauth("user@example.com", otp_provider=provider, force=True, credentials=self.credentials)
                self.assertTrue(result["ok"])
                self.assertIs(runner.call_args.kwargs["otp_provider"], provider)

    def test_adapters_forward_provider_and_reuse_existing_page(self):
        from core import roxy_codex_oauth as roxy

        for kind in ("local", "cloud"):
            for supplied in (False, True):
                with self.subTest(adapter=kind, supplied=supplied), ExitStack() as stack:
                    provider = Mock() if supplied else None
                    page = Mock()
                    stack.enter_context(patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"))
                    stack.enter_context(patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "s", "auth_url": "https://example.invalid"}))
                    if kind == "local":
                        login = stack.enter_context(patch.object(local, "login_with_credentials"))
                        stack.enter_context(patch.object(roxy, "_do_phone_verification_if_present", side_effect=RuntimeError("stop")))
                        clear = stack.enter_context(patch.object(roxy, "clear_roxy_browser_auth_state"))
                        create = stack.enter_context(patch.object(roxy, "RoxyBrowserClient"))
                        roxy._run_roxy_codex_oauth_once(
                            "user@example.com", otp_provider=provider, force=True, existing_driver=page,
                            existing_opened=Mock(profile_id="p"), reuse_existing_profile=True,
                            clear_existing_state=False, credentials=self.credentials,
                        )
                    else:
                        login = stack.enter_context(patch.object(cloud, "_login_with_credentials"))
                        stack.enter_context(patch.object(cloud, "_install_account_dead_response_tracker"))
                        stack.enter_context(patch.object(cloud, "_do_phone_verification_if_present", side_effect=RuntimeError("stop")))
                        fallback = stack.enter_context(patch("core.email_provider.wait_for_otp"))
                        create = stack.enter_context(patch.object(cloud, "BrowserUseClient"))
                        clear = None
                        cloud._run_browser_use_codex_oauth_once(
                            "user@example.com", otp_provider=provider, force=True, existing_browser=Mock(),
                            existing_context=Mock(), existing_page=page, existing_session_info=Mock(),
                            clear_existing_state=False, credentials=self.credentials,
                        )
                        fallback.assert_not_called()
                    create.assert_not_called()
                    if clear is not None:
                        clear.assert_not_called()
                    self.assertIs(login.call_args.args[0], page)
                    if supplied:
                        self.assertIs(login.call_args.kwargs["otp_provider"], provider)
                    else:
                        self.assertNotIn("otp_provider", login.call_args.kwargs)

    def test_provider_errors_are_sanitized_before_adapter_logging(self):
        from core import roxy_codex_oauth as roxy

        for kind in ("local", "cloud"):
            with self.subTest(adapter=kind), ExitStack() as stack:
                page = Mock()
                provider = Mock(side_effect=RuntimeError("sensitive-source-url secret-code-123456"))
                stack.enter_context(patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"))
                stack.enter_context(patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "s", "auth_url": "https://example.invalid"}))
                if kind == "local":
                    stack.enter_context(patch.object(local, "_open_and_submit_email", return_value="email_otp"))
                    logs = stack.enter_context(self.assertLogs("core.roxy_codex_oauth", logging.DEBUG))
                    result = roxy._run_roxy_codex_oauth_once(
                        "user@example.com", otp_provider=provider, force=True, existing_driver=page,
                        existing_opened=Mock(profile_id="p"), reuse_existing_profile=True,
                        clear_existing_state=False, credentials=self.credentials,
                    )
                else:
                    stack.enter_context(patch.object(cloud, "_install_account_dead_response_tracker"))
                    stack.enter_context(patch.object(cloud, "_wait_auth_page_ready"))
                    stack.enter_context(patch.object(cloud, "_maybe_accept_cookies"))
                    stack.enter_context(patch.object(cloud, "_bu_delay"))
                    stack.enter_context(patch.object(cloud, "_classify_credential_login_state", return_value="email_otp"))
                    logs = stack.enter_context(self.assertLogs("core.browser_use_codex_oauth", logging.DEBUG))
                    result = cloud._run_browser_use_codex_oauth_once(
                        "user@example.com", otp_provider=provider, force=True, existing_browser=Mock(),
                        existing_context=Mock(), existing_page=page, existing_session_info=Mock(),
                        clear_existing_state=False, credentials=self.credentials,
                    )
                self.assertFalse(result["ok"])
                self.assertIn("OTP provider failed", result["message"])
                self.assertNotIn("sensitive-source-url", str(result))
                for record in logs.records:
                    formatted = logging.Formatter("%(message)s").format(record)
                    self.assertNotIn("sensitive-source-url", formatted)
                    self.assertNotIn("123456", formatted)
                provider.acknowledge.assert_not_called()

    def test_noncredential_cloud_still_defaults_to_mailbox_provider(self):
        page = Mock()
        with ExitStack() as stack:
            fallback = stack.enter_context(patch("core.email_provider.wait_for_otp"))
            fill = stack.enter_context(patch.object(cloud, "_fill_email_and_otp", side_effect=RuntimeError("stop")))
            stack.enter_context(patch.object(cloud, "_install_account_dead_response_tracker"))
            stack.enter_context(patch("core.codex_oauth._codex_auth_url_source", return_value="cpa"))
            stack.enter_context(patch("core.codex_oauth._request_cpa_authorize_url", return_value={"state": "s", "auth_url": "https://example.invalid"}))
            cloud._run_browser_use_codex_oauth_once(
                "user@example.com", force=True, existing_browser=Mock(), existing_context=Mock(),
                existing_page=page, existing_session_info=Mock(), credentials=None,
            )
        self.assertIs(fill.call_args.args[2], fallback)


if __name__ == "__main__":
    unittest.main()
