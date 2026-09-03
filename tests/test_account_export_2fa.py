import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlparse

from core.account_export import (
    BrowserPageTransport,
    _follow_reauth,
    _is_reauth_retryable_error,
    _ScriptResponse,
    _trigger_reauth,
    setup_2fa,
    setup_2fa_for_registration,
)
from core.gmail_api_url_client import GmailApiUrlError


class AccountExportTwofaTransportTests(unittest.TestCase):
    def test_rate_limit_reauth_error_is_retryable(self):
        self.assertTrue(_is_reauth_retryable_error(RuntimeError("HTTP 429: rate_limit_exceeded")))

    @patch("core.account_export.human_delay")
    @patch("core.account_export._activate_totp")
    @patch(
        "core.account_export._enroll_totp",
        side_effect=[RuntimeError('HTTP 500: {"detail":"Request timeout"}'), ("SECRET", "session-id")],
    )
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    @patch("core.account_export._open_twofa_action")
    def test_setup_2fa_retries_transient_enroll_timeout(
        self,
        open_action,
        fetch_session,
        enroll_totp,
        activate_totp,
        _human_delay,
    ):
        secret = setup_2fa(Mock(), "user@example.com")

        self.assertEqual(secret, "SECRET")
        self.assertEqual(enroll_totp.call_count, 2)
        activate_totp.assert_called_once_with(
            enroll_totp.call_args.args[0],
            "home-token",
            "SECRET",
            "session-id",
        )
        open_action.assert_called_once()
        fetch_session.assert_called_once()

    @patch("core.account_export.human_delay")
    @patch("core.account_export._activate_totp")
    @patch("core.account_export._enroll_totp", side_effect=RuntimeError("HTTP 401: unauthorized"))
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    @patch("core.account_export._open_twofa_action")
    def test_setup_2fa_does_not_retry_non_transient_enroll_error(
        self,
        _open_action,
        _fetch_session,
        enroll_totp,
        activate_totp,
        _human_delay,
    ):
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            setup_2fa(Mock(), "user@example.com")

        enroll_totp.assert_called_once()
        activate_totp.assert_not_called()

    @patch("core.account_export.human_delay")
    @patch(
        "core.account_export._activate_totp",
        side_effect=[RuntimeError('HTTP 500: {"detail":"Request timeout"}'), True],
    )
    @patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id"))
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    @patch("core.account_export._open_twofa_action")
    def test_setup_2fa_retries_transient_activate_timeout(
        self,
        _open_action,
        _fetch_session,
        _enroll_totp,
        activate_totp,
        _human_delay,
    ):
        secret = setup_2fa(Mock(), "user@example.com")

        self.assertEqual(secret, "SECRET")
        self.assertEqual(activate_totp.call_count, 2)

    def test_registration_twofa_always_reauthenticates_before_enrollment(self):
        transport = Mock()

        with patch("core.account_export.setup_2fa", return_value="SECRET") as setup:
            secret = setup_2fa_for_registration(transport, "user@example.com")

        self.assertEqual(secret, "SECRET")
        setup.assert_called_once_with(transport, "user@example.com", reauth=True)

    def test_registration_twofa_does_not_fallback_to_unreauthenticated_session(self):
        transport = Mock()
        error = RuntimeError("HTTP 401: recent_auth_required")

        with patch("core.account_export.setup_2fa", side_effect=error) as setup:  # noqa: SIM117
            with self.assertRaises(RuntimeError) as raised:
                setup_2fa_for_registration(transport, "user@example.com")

        self.assertIs(raised.exception, error)
        setup.assert_called_once_with(transport, "user@example.com", reauth=True)

    def test_reauth_uses_password_reauth_route_to_trigger_new_otp(self):
        transport = Mock()
        transport.device_id = "device-id"
        transport.get_nextauth_headers.return_value = {}
        csrf_response = Mock()
        csrf_response.json.return_value = {"csrfToken": "csrf-token"}
        auth_response = Mock()
        auth_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state"}
        transport.get.return_value = csrf_response
        transport.post.return_value = auth_response

        auth_url = _trigger_reauth(transport, "user@example.com")

        self.assertIn("auth.openai.com", auth_url)
        signin_url = transport.post.call_args.args[0]
        query = parse_qs(urlparse(signin_url).query)
        self.assertEqual(query.get("connection"), ["password"])
        self.assertEqual(query.get("reauth"), ["password"])
        self.assertEqual(query.get("login_hint"), ["user@example.com"])
        self.assertNotIn("prompt", query)
        self.assertNotIn("screen_hint", query)

    def test_reauth_reports_non_json_csrf_response_context(self):
        transport = Mock()
        transport.get_nextauth_headers.return_value = {}
        csrf_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/csrf")
        csrf_response.json.side_effect = ValueError("empty response")
        transport.get.return_value = csrf_response

        with self.assertRaisesRegex(RuntimeError, "re-auth CSRF response is not JSON") as raised:
            _trigger_reauth(transport, "user@example.com")

        self.assertIn("status=200", str(raised.exception))
        self.assertIn("api/auth/csrf", str(raised.exception))
        self.assertNotIn("empty response", str(raised.exception))

    def test_reauth_rejects_password_page_before_waiting_for_otp(self):
        transport = Mock()
        transport.get_auth_navigate_headers.return_value = {}
        response = Mock(url="https://auth.openai.com/log-in/password")
        transport.get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "email-verification"):
            _follow_reauth(transport, "https://auth.openai.com/api/accounts/authorize?state=state")

    def test_reauth_does_not_resend_email_otp_after_authorize_redirect(self):
        transport = Mock()
        transport.get_auth_navigate_headers.return_value = {}
        response = Mock(url="https://auth.openai.com/email-verification")
        transport.get.return_value = response

        with patch("core.openai_auth.send_email_otp") as send_email_otp:
            final_url = _follow_reauth(
                transport,
                "https://auth.openai.com/api/accounts/authorize?state=state",
            )

        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        send_email_otp.assert_not_called()

    def test_wrong_reauth_otp_logs_in_again_and_fetches_fresh_code(self):
        transport = Mock()
        with (
            patch("core.account_export.time.time", side_effect=[100.0, 200.0]),
            patch("core.account_export.human_delay"),
            patch("core.account_export.time.sleep"),
            patch("core.account_export._trigger_reauth", side_effect=["first-auth-url", "second-auth-url"]) as trigger_reauth,
            patch("core.account_export._follow_reauth") as follow_reauth,
            patch(
                "core.account_export._validate_reauth_otp",
                side_effect=[
                    RuntimeError('HTTP 401: {"code":"wrong_email_otp_code"}'),
                    "continue-url",
                ],
            ) as validate_otp,
            patch("core.account_export._exchange_new_token", return_value="reauth-token") as exchange_token,
            patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id")) as enroll_totp,
            patch("core.account_export._activate_totp") as activate_totp,
            patch("core.email_provider.wait_for_otp", side_effect=["111111", "222222"]) as wait_for_otp,
            patch("core.openai_auth.send_email_otp") as resend_otp,
        ):
            secret = setup_2fa(transport, "user@example.com", reauth=True)

        self.assertEqual(secret, "SECRET")
        self.assertEqual(
            trigger_reauth.call_args_list,
            [
                call(transport, "user@example.com"),
                call(transport, "user@example.com"),
            ],
        )
        self.assertEqual(
            follow_reauth.call_args_list,
            [call(transport, "first-auth-url"), call(transport, "second-auth-url")],
        )
        self.assertEqual(
            wait_for_otp.call_args_list,
            [
                call(
                    "user@example.com",
                    after_ts=100.0,
                    before_code=None,
                    stage="twofa_reauth_email_otp",
                ),
                call(
                    "user@example.com",
                    after_ts=200.0,
                    before_code="111111",
                    stage="twofa_reauth_email_otp",
                ),
            ],
        )
        self.assertEqual(
            validate_otp.call_args_list,
            [call(transport, "111111"), call(transport, "222222")],
        )
        resend_otp.assert_called_once_with(
            transport,
            referer="https://auth.openai.com/email-verification",
        )
        transport.navigate.assert_called_once_with("https://chatgpt.com/")
        exchange_token.assert_called_once_with(transport, "continue-url")
        enroll_totp.assert_called_once_with(transport, "reauth-token")
        activate_totp.assert_called_once_with(transport, "reauth-token", "SECRET", "session-id")

    def test_invalid_auth_step_reauth_logs_in_again_and_fetches_fresh_code(self):
        transport = Mock()
        with (
            patch("core.account_export.time.time", side_effect=[100.0, 200.0]),
            patch("core.account_export.human_delay"),
            patch("core.account_export.time.sleep"),
            patch("core.account_export._trigger_reauth", side_effect=["first-auth-url", "second-auth-url"]) as trigger_reauth,
            patch("core.account_export._follow_reauth"),
            patch(
                "core.account_export._validate_reauth_otp",
                side_effect=[
                    RuntimeError('HTTP 400: {"code":"invalid_auth_step"}'),
                    "continue-url",
                ],
            ) as validate_otp,
            patch("core.account_export._exchange_new_token", return_value="reauth-token") as exchange_token,
            patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id")) as enroll_totp,
            patch("core.account_export._activate_totp") as activate_totp,
            patch("core.email_provider.wait_for_otp", side_effect=["111111", "222222"]) as wait_for_otp,
            patch("core.openai_auth.send_email_otp"),
        ):
            secret = setup_2fa(transport, "user@example.com", reauth=True)

        self.assertEqual(secret, "SECRET")
        self.assertEqual(trigger_reauth.call_count, 2)
        self.assertEqual(
            wait_for_otp.call_args_list,
            [
                call(
                    "user@example.com",
                    after_ts=100.0,
                    before_code=None,
                    stage="twofa_reauth_email_otp",
                ),
                call(
                    "user@example.com",
                    after_ts=200.0,
                    before_code="111111",
                    stage="twofa_reauth_email_otp",
                ),
            ],
        )
        self.assertEqual(validate_otp.call_args_list, [call(transport, "111111"), call(transport, "222222")])
        exchange_token.assert_called_once_with(transport, "continue-url")
        enroll_totp.assert_called_once_with(transport, "reauth-token")
        activate_totp.assert_called_once_with(transport, "reauth-token", "SECRET", "session-id")

    def test_reauth_retries_when_provider_does_not_publish_a_new_otp(self):
        transport = Mock()
        with (
            patch("core.account_export.time.time", side_effect=[100.0, 200.0]),
            patch("core.account_export.human_delay"),
            patch("core.account_export.time.sleep"),
            patch("core.account_export._trigger_reauth", side_effect=["first-auth-url", "second-auth-url"]) as trigger_reauth,
            patch("core.account_export._follow_reauth") as follow_reauth,
            patch("core.account_export._validate_reauth_otp", return_value="continue-url") as validate_otp,
            patch("core.account_export._exchange_new_token", return_value="reauth-token"),
            patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id")),
            patch("core.account_export._activate_totp"),
            patch(
                "core.email_provider.wait_for_otp",
                side_effect=[
                    GmailApiUrlError("Timeout after 60s waiting for new OTP"),
                    "222222",
                ],
            ) as wait_for_otp,
            patch("core.openai_auth.send_email_otp"),
        ):
            secret = setup_2fa(transport, "user@example.com", reauth=True)

        self.assertEqual(secret, "SECRET")
        self.assertEqual(trigger_reauth.call_count, 2)
        self.assertEqual(
            follow_reauth.call_args_list,
            [call(transport, "first-auth-url"), call(transport, "second-auth-url")],
        )
        self.assertEqual(
            wait_for_otp.call_args_list,
            [
                call(
                    "user@example.com",
                    after_ts=100.0,
                    before_code=None,
                    stage="twofa_reauth_email_otp",
                ),
                call(
                    "user@example.com",
                    after_ts=200.0,
                    before_code=None,
                    stage="twofa_reauth_email_otp",
                ),
            ],
        )
        validate_otp.assert_called_once_with(transport, "222222")

    def test_reauth_rate_limit_uses_long_exponential_backoff(self):
        transport = Mock()
        rate_limit_error = RuntimeError(
            "re-auth 未进入 email-verification 页面: "
            "https://auth.openai.com/error?errorCode=rate_limit_exceeded"
        )
        with (
            patch("core.account_export.time.time", side_effect=[100.0, 200.0, 300.0]),
            patch("core.account_export.human_delay") as human_delay,
            patch("core.account_export._trigger_reauth", side_effect=["first-url", "second-url", "third-url"]),
            patch("core.account_export._follow_reauth", side_effect=[rate_limit_error, rate_limit_error, "email-url"]),
            patch("core.account_export._validate_reauth_otp", return_value="continue-url"),
            patch("core.account_export._exchange_new_token", return_value="reauth-token"),
            patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id")),
            patch("core.account_export._activate_totp"),
            patch("core.email_provider.wait_for_otp", return_value="333333"),
            patch("core.email_provider.snapshot_verification_code", return_value=None),
            patch("core.openai_auth.send_email_otp"),
        ):
            secret = setup_2fa(transport, "user@example.com", reauth=True)

        self.assertEqual(secret, "SECRET")
        retry_backoffs = [
            (call.kwargs["minimum"], call.kwargs["maximum"])
            for call in human_delay.call_args_list
            if call.kwargs.get("minimum") is not None
        ]
        self.assertEqual(retry_backoffs, [(15.0, 18.75), (30.0, 37.5)])

    @patch("core.account_export.human_delay")
    @patch("core.account_export._activate_totp")
    @patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id"))
    @patch("core.account_export._exchange_new_token", return_value="reauth-token")
    @patch("core.account_export._validate_reauth_otp", return_value="continue-url")
    @patch("core.account_export._follow_reauth")
    @patch("core.account_export._trigger_reauth", return_value="auth-url")
    @patch("core.email_provider.wait_for_otp", return_value="123456")
    def test_saved_account_twofa_retry_uses_email_reauth(
        self,
        wait_for_otp,
        trigger_reauth,
        follow_reauth,
        validate_otp,
        exchange_token,
        enroll_totp,
        activate_totp,
        _human_delay,
    ):
        transport = Mock()

        secret = setup_2fa(transport, "user@example.com", reauth=True)

        self.assertEqual(secret, "SECRET")
        trigger_reauth.assert_called_once_with(transport, "user@example.com")
        follow_reauth.assert_called_once_with(transport, "auth-url")
        wait_for_otp.assert_called_once()
        validate_otp.assert_called_once_with(transport, "123456")
        exchange_token.assert_called_once_with(transport, "continue-url")
        enroll_totp.assert_called_once_with(transport, "reauth-token")
        activate_totp.assert_called_once_with(transport, "reauth-token", "SECRET", "session-id")

    @patch("core.account_export.human_delay")
    @patch("core.account_export._activate_totp")
    @patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id"))
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    @patch("core.email_provider.wait_for_otp")
    def test_setup_2fa_uses_home_action_without_email_reauth(
        self,
        wait_for_otp,
        fetch_session,
        enroll_totp,
        activate_totp,
        _human_delay,
    ):
        transport = Mock()
        events = []
        transport.navigate.side_effect = lambda *_args, **_kwargs: events.append("action")
        fetch_session.side_effect = lambda *_args: events.append("home_session") or {"accessToken": "home-token"}
        enroll_totp.side_effect = lambda *_args: events.append("enroll") or ("SECRET", "session-id")
        activate_totp.side_effect = lambda *_args: events.append("activate")

        secret = setup_2fa(transport, "user@example.com")

        self.assertEqual(secret, "SECRET")
        self.assertEqual(events, ["action", "home_session", "enroll", "activate"])
        transport.navigate.assert_called_once_with(
            "https://chatgpt.com/?action=enable&factor=totp",
            referer="https://chatgpt.com/",
        )
        fetch_session.assert_called_once_with(transport)
        enroll_totp.assert_called_once_with(transport, "home-token")
        activate_totp.assert_called_once_with(
            transport,
            "home-token",
            "SECRET",
            "session-id",
        )
        wait_for_otp.assert_not_called()

    @patch("core.account_export.human_delay")
    @patch("core.account_export._activate_totp")
    @patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id"))
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    def test_setup_2fa_opens_home_action_for_protocol_session(
        self,
        fetch_session,
        enroll_totp,
        activate_totp,
        _human_delay,
    ):
        response = Mock()

        class ProtocolSession:
            device_id = "device-id"

            def get_chatgpt_navigate_headers(self, referer="https://chatgpt.com/"):
                return {"referer": referer}

            def get(self, url, **kwargs):
                self.request = (url, kwargs)
                return response

        session = ProtocolSession()

        secret = setup_2fa(session, "user@example.com")

        self.assertEqual(secret, "SECRET")
        self.assertEqual(
            session.request,
            (
                "https://chatgpt.com/?action=enable&factor=totp",
                {
                    "headers": {"referer": "https://chatgpt.com/"},
                    "allow_redirects": True,
                },
            ),
        )
        response.raise_for_status.assert_called_once_with()
        fetch_session.assert_called_once_with(session)
        enroll_totp.assert_called_once_with(session, "home-token")
        activate_totp.assert_called_once_with(
            session,
            "home-token",
            "SECRET",
            "session-id",
        )

    @patch("core.account_export._open_twofa_action")
    @patch("core.account_export.fetch_session", return_value={"accessToken": "home-token"})
    @patch("core.account_export._enroll_totp", return_value=("SECRET", "session-id"))
    @patch("core.account_export._activate_totp")
    @patch("core.account_export.human_delay")
    def test_setup_2fa_without_reauth_uses_home_action(
        self,
        _human_delay,
        _activate,
        _enroll,
        fetch_session,
        open_action,
    ):
        transport = Mock()

        setup_2fa(transport, "user@example.com")

        open_action.assert_called_once_with(transport)
        fetch_session.assert_called_once_with(transport)

    def test_browser_request_sets_and_restores_script_timeout(self):
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        driver.script_timeout = 47
        driver.execute_async_script.return_value = {
            "status": 200,
            "url": "https://chatgpt.com/api/test",
            "text": "{\"ok\":true}",
            "json": {"ok": True},
        }

        response = BrowserPageTransport(driver).get("https://chatgpt.com/api/test")

        self.assertIsInstance(response, _ScriptResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(driver.set_script_timeout.call_args_list[0].args, (47,))
        self.assertEqual(driver.set_script_timeout.call_args_list[-1].args, (47,))

    def test_browser_request_propagates_script_timeout(self):
        driver = Mock()
        driver.current_url = "https://chatgpt.com/"
        driver.script_timeout = 47
        driver.execute_async_script.side_effect = RuntimeError("script timeout")

        with self.assertRaisesRegex(RuntimeError, "script timeout"):
            BrowserPageTransport(driver).get("https://chatgpt.com/api/test")

        self.assertEqual(driver.set_script_timeout.call_args_list[-1].args, (47,))


if __name__ == "__main__":
    unittest.main()
