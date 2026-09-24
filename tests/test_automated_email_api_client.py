import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import automated_email_api_client as client


class AutomatedEmailApiClientTests(unittest.TestCase):
    def setUp(self):
        self.state: dict[str, object] = {}
        self.get_document = patch(
            "core.app_state_db.get_named_document",
            side_effect=lambda key, default=None: self.state.get(key, default),
        )
        self.set_document = patch(
            "core.app_state_db.set_named_document",
            side_effect=lambda key, value: self.state.__setitem__(key, value),
        )
        self.get_document.start()
        self.set_document.start()
        client.reset_runtime_state(clear_persisted=True)
        self.config = SimpleNamespace(
            EMAIL_API_BASE_URL="https://mail.example.test",
            EMAIL_API_KEY="test-key",
            EMAIL_API_REQUEST_TIMEOUT=7,
            EMAIL_API_POLL_INTERVAL=1,
            OTP_MAX_WAIT=2,
            OTP_SETTLE_SECONDS=0,
        )

    def tearDown(self):
        client.reset_runtime_state(clear_persisted=True)
        self.set_document.stop()
        self.get_document.stop()

    @staticmethod
    def _response(payload, status_code=200):
        return SimpleNamespace(
            status_code=status_code,
            text="",
            json=lambda: payload,
        )

    @patch("core.automated_email_api_client._config")
    @patch("core.automated_email_api_client.requests.request")
    @patch("core.automated_email_api_client._prepare_account_for_use")
    def test_pick_creates_gmail_only_mailbox_and_twelve_dual_domain_aliases(
        self, prepare, request, config
    ):
        config.return_value = (
            self.config.EMAIL_API_BASE_URL,
            self.config.EMAIL_API_KEY,
            self.config.EMAIL_API_REQUEST_TIMEOUT,
            self.config.EMAIL_API_POLL_INTERVAL,
        )
        request.return_value = self._response(
            {"code": 0, "message": "success", "data": {"type": "gmail", "email": "abcdef@gmail.com"}}
        )

        first = client.pick_account()
        remaining = [client.pick_account() for _ in range(11)]
        aliases = [first.email] + [account.email for account in remaining]

        self.assertEqual(len(set(aliases)), 12)
        self.assertEqual(sum(email.endswith("@gmail.com") for email in aliases), 6)
        self.assertEqual(sum(email.endswith("@googlemail.com") for email in aliases), 6)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["params"]["type"], "gmail")
        self.assertEqual(request.call_args.kwargs["params"]["apikey"], "test-key")
        self.assertEqual(prepare.call_count, 1)

    @patch("core.automated_email_api_client._config")
    @patch("core.automated_email_api_client.requests.request")
    @patch("core.automated_email_api_client._prepare_account_for_use")
    def test_googlemail_response_still_balances_six_aliases_per_domain(
        self, prepare, request, config
    ):
        config.return_value = (
            self.config.EMAIL_API_BASE_URL,
            self.config.EMAIL_API_KEY,
            self.config.EMAIL_API_REQUEST_TIMEOUT,
            self.config.EMAIL_API_POLL_INTERVAL,
        )
        request.return_value = self._response(
            {"code": 0, "message": "success", "data": {"type": "gmail", "email": "abcdef@googlemail.com"}}
        )

        accounts = [client.pick_account()] + [client.pick_account() for _ in range(11)]
        aliases = [account.email for account in accounts]

        self.assertEqual(sum(email.endswith("@gmail.com") for email in aliases), 6)
        self.assertEqual(sum(email.endswith("@googlemail.com") for email in aliases), 6)
        self.assertEqual({account.query_email for account in accounts}, {"abcdef@googlemail.com"})

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_registration_lock_is_shared_and_reentrant_per_source_mailbox(
        self, request, prepare
    ):
        request.return_value = {
            "code": 0,
            "data": {"type": "gmail", "email": "abcdef@gmail.com"},
        }

        first = client.pick_account()
        second = client.pick_account()
        first_lock = client.registration_mailbox_lock(first.email)
        second_lock = client.registration_mailbox_lock(second.email)

        self.assertIs(first_lock, second_lock)
        first_lock.acquire()
        try:
            self.assertTrue(first_lock.acquire(blocking=False))
            first_lock.release()
        finally:
            first_lock.release()

    @patch("core.automated_email_api_client._request")
    def test_create_email_rejects_non_gmail_type(self, request):
        with self.assertRaisesRegex(client.AutomatedEmailApiError, "仅支持 type=gmail"):
            client.create_email("outlook")
        request.assert_not_called()

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_get_latest_mail_queries_original_mailbox_for_alias(self, request, prepare):
        request.side_effect = [
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            {"code": 0, "message": "success", "data": {
                "from": "sender@example.com",
                "to": "abcdef@gmail.com",
                "subject": "Verify",
                "text": "your code is 123456",
            }},
        ]

        client.pick_account()
        alias = client.pick_account().email
        message = client.get_latest_mail(alias)

        self.assertEqual(message.code, "123456")
        self.assertEqual(message.recipient, "abcdef@gmail.com")
        self.assertEqual(request.call_args.kwargs["params"]["email"], "abcdef@gmail.com")

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_empty_success_mail_is_not_an_error(self, request, prepare):
        request.side_effect = [
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            {"code": 0, "message": "success"},
        ]

        account = client.pick_account()
        self.assertIsNone(client.get_latest_mail(account.email))

    @patch("core.automated_email_api_client.time.sleep", return_value=None)
    @patch("core.automated_email_api_client.time.monotonic", side_effect=[0, 0, 0, 1, 1, 1, 3, 3])
    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_fetch_latest_otp_polls_retryable_empty_response(
        self, request, prepare, monotonic, sleep
    ):
        request.side_effect = [
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            {"code": 0, "message": "success"},
            {"code": 0, "data": {"code": "654321", "text": "your code is 654321"}},
        ]

        account = client.pick_account()
        with patch.object(client, "_config", return_value=(
            self.config.EMAIL_API_BASE_URL,
            self.config.EMAIL_API_KEY,
            self.config.EMAIL_API_REQUEST_TIMEOUT,
            self.config.EMAIL_API_POLL_INTERVAL,
        )), patch("config.email.OTP_MAX_WAIT", 2), patch("config.email.OTP_SETTLE_SECONDS", 0):
            self.assertEqual(client.fetch_latest_otp(account.email, max_wait=2), "654321")
        sleep.assert_called()

    @patch("core.automated_email_api_client.requests.request")
    @patch("core.automated_email_api_client._config")
    def test_terminal_api_error_is_not_retried(self, config, request):
        config.return_value = (
            self.config.EMAIL_API_BASE_URL,
            self.config.EMAIL_API_KEY,
            self.config.EMAIL_API_REQUEST_TIMEOUT,
            self.config.EMAIL_API_POLL_INTERVAL,
        )
        request.return_value = self._response(
            {"code": 402, "message": "insufficient quota"}, 402
        )

        with self.assertRaisesRegex(client.AutomatedEmailApiError, "insufficient quota") as raised:
            client.pick_account()
        self.assertEqual(raised.exception.status_code, 402)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(request.call_count, 1)

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client.time.monotonic", return_value=0)
    @patch("core.automated_email_api_client._request")
    def test_terminal_mailbox_error_quarantines_all_aliases(self, request, monotonic, prepare):
        request.side_effect = [
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            client.AutomatedEmailApiError("mailbox disabled", status_code=410),
        ]

        first = client.pick_account()
        aliases = [first.email]
        aliases.extend(client.pick_account().email for _ in range(11))

        with self.assertRaises(client.AutomatedEmailApiError):
            client.fetch_latest_otp(first.email, max_wait=0, settle_seconds=0)

        states = {row["status"] for row in client.list_accounts()}
        self.assertEqual(states, {"failed"})
        self.assertEqual(len(client.list_accounts(status="failed")), len(aliases))

    @patch("core.automated_email_api_client._request")
    def test_preflight_records_existing_code_as_stale_baseline(self, request):
        request.side_effect = [
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            {"code": 0, "data": {"code": "123456", "text": "code 123456"}},
        ]

        account = client.pick_account()

        self.assertEqual(account.seen_codes, {"123456"})
        self.assertEqual(request.call_args.kwargs["params"]["email"], "abcdef@gmail.com")

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_deleted_alias_is_not_restored_from_persisted_mailbox(self, request, prepare):
        request.return_value = {
            "code": 0,
            "data": {"type": "gmail", "email": "abcdef@gmail.com"},
        }

        first = client.pick_account()
        second = client.pick_account()
        self.assertTrue(client.delete_account(first.email))
        self.assertNotIn(first.email, {row["email"] for row in client.list_accounts()})

        client.reset_runtime_state()
        restored = {row["email"] for row in client.list_accounts()}
        self.assertNotIn(first.email, restored)
        self.assertIn(second.email, restored)

    @patch("core.automated_email_api_client._prepare_account_for_use")
    @patch("core.automated_email_api_client._request")
    def test_pool_summary_counts_aliases_and_available_mailboxes(self, request, prepare):
        request.return_value = {
            "code": 0,
            "data": {"type": "gmail", "email": "abcdef@gmail.com"},
        }

        first = client.pick_account()
        second = client.pick_account()
        client.mark_account_consumed(first.email)

        summary = client.pool_summary()

        self.assertEqual(summary["total"], 12)
        self.assertEqual(summary["used"], 1)
        self.assertEqual(summary["available"], 10)
        self.assertEqual(summary["alias_source_available"], 1)
        self.assertEqual(second.state, "reserved")

    @patch("core.automated_email_api_client.time.sleep", return_value=None)
    @patch("core.automated_email_api_client._request")
    def test_mailbox_creation_retries_temporary_stock_error(self, request, sleep):
        request.side_effect = [
            client.AutomatedEmailApiError("out of stock", status_code=404, retryable=True),
            {"code": 0, "data": {"type": "gmail", "email": "abcdef@gmail.com"}},
            {"code": 0, "message": "success"},
        ]

        account = client.pick_account()

        self.assertEqual(account.email, "abcdef@gmail.com")
        self.assertEqual(request.call_count, 3)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
