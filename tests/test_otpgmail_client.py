import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import otpgmail_client as client
from core.gmail_aliases import generate_gmail_dual_domain_variants


class OtpGmailClientTests(unittest.TestCase):
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

    def tearDown(self):
        client.reset_runtime_state(clear_persisted=True)
        self.set_document.stop()
        self.get_document.stop()

    @staticmethod
    def _response(payload, status_code=200, headers=None):
        return SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
            json=lambda: payload,
        )

    @staticmethod
    def _config():
        return (
            "https://otpgmail.example.test",
            "test-key",
            "dr",
            7,
            1,
            2,
        )

    @staticmethod
    def _aliases(email: str) -> tuple[str, ...]:
        return tuple(generate_gmail_dual_domain_variants(email, 12))

    def _persisted_order(
        self,
        order_id: str,
        query_email: str,
        *,
        state: str = "available",
        aliases: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        aliases = aliases or self._aliases(query_email)
        return [
            {
                "email": alias,
                "order_id": order_id,
                "service_code": "dr",
                "query_email": query_email,
                "aliases": list(aliases),
                "state": state,
                "order_status": "waiting_code",
                "seen_codes": [],
            }
            for alias in aliases
        ]

    @staticmethod
    def _order_payload(
        order_id: str,
        email: str,
        *,
        status: str = "waiting_code",
        otp: list[dict[str, object]] | None = None,
        as_list: bool = False,
    ) -> dict[str, object]:
        data = {
            "orderId": order_id,
            "email": email,
            "status": status,
            "otp": list(otp or []),
        }
        return {"success": True, "data": [data] if as_list else data}

    def test_default_service_code_matches_otpgmail_openai_service(self):
        self.assertEqual(client.DEFAULT_SERVICE_CODE, "dr")

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_persisted_v1_state_restores_all_twelve_aliases(self, request, config):
        config.return_value = self._config()
        aliases = self._aliases("root@gmail.com")
        self.state["otpmail.orders.v1"] = self._persisted_order(
            "ord-persisted",
            "root@gmail.com",
            aliases=aliases,
        )

        client.reset_runtime_state()

        rows = client.list_accounts()

        self.assertEqual(len(rows), 12)
        self.assertEqual({row["order_id"] for row in rows}, {"ord-persisted"})
        self.assertEqual({row["status"] for row in rows}, {"available"})
        contexts = [client.get_account_context(alias) for alias in aliases]
        self.assertEqual({account.order_id for account in contexts if account}, {"ord-persisted"})
        self.assertEqual({account.query_email for account in contexts if account}, {"root@gmail.com"})
        self.assertEqual({account.aliases for account in contexts if account}, {aliases})
        request.assert_not_called()

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_pick_creates_one_order_with_twelve_aliases(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload(
                "ord-1", "user@gmail.com", as_list=True,
            )),
            self._response(self._order_payload("ord-1", "user@gmail.com")),
        ]

        accounts = [client.pick_account() for _ in range(12)]

        self.assertEqual(len({account.email for account in accounts}), 12)
        self.assertEqual({account.order_id for account in accounts}, {"ord-1"})
        self.assertEqual({account.query_email for account in accounts}, {"user@gmail.com"})
        self.assertEqual({account.aliases for account in accounts}, {self._aliases("user@gmail.com")})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[:2], (
            "POST", "https://otpgmail.example.test/v1/orders",
        ))
        self.assertEqual(request.call_args_list[0].kwargs["json"], {"service": "dr", "quantity": 1})
        self.assertEqual(request.call_args_list[1].args[:2], (
            "GET", "https://otpgmail.example.test/v1/orders/ord-1",
        ))

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_thirteenth_pick_starts_a_new_order_after_first_twelve_aliases(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-first", "first@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-first", "first@gmail.com")),
            self._response(self._order_payload("ord-second", "second@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-second", "second@gmail.com")),
        ]

        accounts = [client.pick_account() for _ in range(13)]

        self.assertEqual({account.order_id for account in accounts[:12]}, {"ord-first"})
        self.assertEqual(accounts[12].order_id, "ord-second")
        self.assertEqual(len({account.email for account in accounts}), 13)
        self.assertEqual(request.call_count, 4)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_fetch_for_every_alias_uses_the_same_parent_order_id(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-shared", "root@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-shared", "root@gmail.com")),
            *[
                self._response(self._order_payload("ord-shared", "root@gmail.com"))
                for _ in range(12)
            ],
        ]

        accounts = [client.pick_account() for _ in range(12)]
        for account in accounts:
            with self.assertRaises(client.OtpGmailError):
                client.fetch_latest_otp(account.email, max_wait=0)

        fetched_urls = [call.args[1] for call in request.call_args_list[2:]]
        self.assertEqual(fetched_urls, [
            "https://otpgmail.example.test/v1/orders/ord-shared"
        ] * 12)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_fetch_rejects_a_job_bound_order_id_that_does_not_match_alias(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-bound", "root@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-bound", "root@gmail.com")),
        ]

        account = client.pick_account()

        with self.assertRaises(client.OtpGmailError):
            client.fetch_latest_otp(account.email, max_wait=0, order_id="ord-other")
        self.assertEqual(request.call_count, 2)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_cancelled_existing_order_is_replaced_in_same_pick(self, request, config):
        config.return_value = self._config()
        self.state["otpmail.orders.v1"] = self._persisted_order(
            "ord-stale", "stale@gmail.com"
        )
        request.side_effect = [
            self._response(self._order_payload("ord-stale", "stale@gmail.com", status="cancelled")),
            self._response(self._order_payload("ord-fresh", "fresh@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-fresh", "fresh@gmail.com")),
        ]

        account = client.pick_account()

        self.assertEqual(account.order_id, "ord-fresh")
        self.assertEqual(account.email, self._aliases("fresh@gmail.com")[0])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(client.pool_summary()["failed"], 12)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_cancelled_new_order_is_replaced_in_same_pick(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-cancelled", "cancelled@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-cancelled", "cancelled@gmail.com", status="cancelled")),
            self._response(self._order_payload("ord-fresh", "fresh@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-fresh", "fresh@gmail.com")),
        ]

        account = client.pick_account()

        self.assertEqual(account.order_id, "ord-fresh")
        self.assertEqual(client.pool_summary()["failed"], 12)
        self.assertEqual(request.call_count, 4)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_duplicate_provider_email_is_cancelled_and_retried(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-first", "same@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-first", "same@gmail.com")),
            self._response(self._order_payload("ord-duplicate", "same@gmail.com", as_list=True)),
            self._response({"success": True, "data": {"orderId": "ord-duplicate", "status": "cancelled"}}),
            self._response(self._order_payload("ord-second", "new@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-second", "new@gmail.com")),
        ]

        first = [client.pick_account() for _ in range(12)]
        second = client.pick_account()

        self.assertEqual({account.order_id for account in first}, {"ord-first"})
        self.assertEqual(second.order_id, "ord-second")
        self.assertEqual(second.email, self._aliases("new@gmail.com")[0])
        self.assertEqual(request.call_args_list[3].args[:2], (
            "POST", "https://otpgmail.example.test/v1/orders/ord-duplicate/cancel",
        ))
        self.assertEqual(request.call_count, 6)

    @patch("core.db.get_account_by_email")
    def test_recovered_context_keeps_provider_email_and_all_aliases(self, get_account):
        aliases = self._aliases("root@gmail.com")
        get_account.return_value = {
            "extra_json": (
                '{"email_service":{"source":"otpmail",'
                '"order_id":"ord-root","service_code":"dr",'
                '"query_email":"root@gmail.com",'
                f'"aliases":{str(list(aliases)).replace(chr(39), chr(34))}'
                "}}"
            ),
        }

        account = client.get_account_context(aliases[3], order_id="ord-root")

        self.assertIsNotNone(account)
        self.assertEqual(account.order_id, "ord-root")
        self.assertEqual(account.query_email, "root@gmail.com")
        self.assertEqual(account.aliases, aliases)

    @patch("core.otpgmail_client._config")
    @patch("core.otpgmail_client.requests.request")
    def test_order_is_cancelled_only_after_all_aliases_fail(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response(self._order_payload("ord-cleanup", "user@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-cleanup", "user@gmail.com")),
            self._response({"success": True, "data": {"orderId": "ord-cleanup", "status": "cancelled"}}),
        ]

        client.pick_account()
        aliases = [row["email"] for row in client.list_accounts()]
        for alias in aliases:
            self.assertTrue(client.release_account(alias, status="failed"))

        self.assertEqual(request.call_count, 3)
        self.assertEqual(client.pool_summary()["failed"], 12)
        self.assertEqual({row["order_status"] for row in client.list_accounts()}, {"cancelled"})

    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_only_gmail_domains_are_accepted(self, request, config):
        request.return_value = self._response(self._order_payload(
            "ord-1", "user@outlook.com", as_list=True,
        ))

        with self.assertRaisesRegex(client.OtpGmailError, "有效 Gmail"):
            client.pick_account()
        self.assertEqual(request.call_count, 1)

    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_fetch_uses_newest_unseen_code_and_skips_before_code(self, request, config):
        request.side_effect = [
            self._response(self._order_payload(
                "ord-1", "user@googlemail.com", as_list=True,
            )),
            self._response(self._order_payload(
                "ord-1", "user@googlemail.com",
                otp=[{"code": "111111", "receivedAt": "2026-09-13T10:00:00Z"}],
            )),
            self._response(self._order_payload(
                "ord-1", "user@googlemail.com", status="completed",
                otp=[
                    {"code": "111111", "receivedAt": "2026-09-13T10:00:00Z"},
                    {"code": "222222", "receivedAt": "2026-09-13T10:01:00Z"},
                    {"code": "333333", "receivedAt": "2026-09-13T10:02:00Z"},
                ],
            )),
        ]

        account = client.pick_account()
        self.assertEqual(
            client.fetch_latest_otp(account.email, max_wait=0, before_code="333333"),
            "222222",
        )
        self.assertEqual(
            request.call_args_list[-1].args[1],
            "https://otpgmail.example.test/v1/orders/ord-1",
        )

    @patch("core.otpgmail_client.time.sleep", return_value=None)
    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_order_retry_reuses_idempotency_key(self, request, config, sleep):
        request.side_effect = [
            self._response({"success": False, "error": {"code": "MAINTENANCE", "message": "busy"}}, 503),
            self._response(self._order_payload("ord-2", "user@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-2", "user@gmail.com")),
        ]

        account = client.pick_account()

        self.assertEqual(account.order_id, "ord-2")
        first_key = request.call_args_list[0].kwargs["headers"]["Idempotency-Key"]
        second_key = request.call_args_list[1].kwargs["headers"]["Idempotency-Key"]
        self.assertEqual(first_key, second_key)
        sleep.assert_called_once()

    @patch("core.otpgmail_client.time.sleep", return_value=None)
    @patch("core.otpgmail_client.time.monotonic", side_effect=[0, 0, 0, 1, 1, 3])
    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_otp_poll_honors_retry_after(self, request, config, monotonic, sleep):
        request.side_effect = [
            self._response(self._order_payload("ord-retry", "user@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-retry", "user@gmail.com")),
            self._response(
                {"success": False, "error": {"code": "RATE_LIMIT", "message": "slow down"}},
                429,
                headers={"Retry-After": "2"},
            ),
            self._response(self._order_payload(
                "ord-retry", "user@gmail.com", status="completed",
                otp=[{"code": "654321", "receivedAt": "2026-09-13T10:00:00Z"}],
            )),
        ]

        account = client.pick_account()
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=2), "654321")
        sleep.assert_called_once_with(2.0)

    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_reserved_aliases_recover_as_available_after_restart(self, request, config):
        request.side_effect = [
            self._response(self._order_payload("ord-4", "user@gmail.com", as_list=True)),
            self._response(self._order_payload("ord-4", "user@gmail.com")),
        ]

        client.pick_account()
        client.reset_runtime_state()

        rows = client.list_accounts()
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["status"] for row in rows}, {"available"})
        self.assertEqual({row["order_id"] for row in rows}, {"ord-4"})

    @patch("core.otpgmail_client._config", return_value=(
        "https://otpgmail.example.test", "test-key", "openai", 7, 1, 2,
    ))
    @patch("core.otpgmail_client.requests.request")
    def test_services_and_balance_are_exposed(self, request, config):
        request.side_effect = [
            self._response({"success": True, "data": [{"code": "openai", "price": 0.1, "stock": 3}]}),
            self._response({"success": True, "data": {"balance": 1.25}}),
        ]

        self.assertEqual(client.list_services()[0]["code"], "openai")
        self.assertEqual(client.get_quota()["balance"], 1.25)


if __name__ == "__main__":
    unittest.main()
