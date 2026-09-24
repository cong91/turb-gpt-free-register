import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import bamboommo_client as client


class BambooMmoClientTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
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
    def _response(payload, status_code=200):
        return SimpleNamespace(status_code=status_code, headers={}, json=lambda: payload)

    @staticmethod
    def _config():
        return ("https://api.bamboommo.example", "test-key", 2, "GM", "OP", 7, 1, 2)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_rent_expands_gmail_order_to_twelve_aliases(self, request, config):
        config.return_value = self._config()
        request.return_value = self._response(
            {
                "statusCode": 200,
                "message": "Success",
                "responseData": "user@gmail.com",
                "responseHeader": {
                    "rentalId": "rent-1",
                    "status": "WAITING_OTP",
                    "canRequestNextOtp": False,
                },
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }
        )

        accounts = [client.pick_account() for _ in range(12)]

        self.assertEqual(len({account.email for account in accounts}), 12)
        self.assertEqual(sum(account.email.endswith("@gmail.com") for account in accounts), 6)
        self.assertEqual(sum(account.email.endswith("@googlemail.com") for account in accounts), 6)
        self.assertEqual({account.rental_id for account in accounts}, {"rent-1"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["json"], {
            "apiKey": "test-key",
            "server": 2,
            "codeTypeMail": "GM",
            "codeService": "OP",
        })

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_cancelled_existing_rental_is_replaced_in_same_pick(self, request, config):
        config.return_value = self._config()
        self.state[client._STATE_KEY] = [{
            "email": "stale@gmail.com",
            "rental_id": "rent-stale",
            "server": 2,
            "code_type_mail": "GM",
            "code_service": "OP",
            "aliases": ["stale@gmail.com"],
            "state": "available",
            "rental_status": "WAITING_OTP",
        }]
        request.side_effect = [
            self._response({
                "statusCode": 400,
                "message": "Rental cancelled",
                "responseHeader": {"rentalId": "rent-stale", "status": "CANCELLED"},
                "isSuccessStatusCode": False,
                "resourceKey": "RENTAL_CANCELLED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "fresh@gmail.com",
                "responseHeader": {"rentalId": "rent-fresh", "status": "WAITING_OTP"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        account = client.pick_account()

        self.assertEqual(account.rental_id, "rent-fresh")
        self.assertEqual(account.email, "fresh@gmail.com")
        self.assertEqual(client.pool_summary()["failed"], 1)
        self.assertEqual(request.call_count, 2)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_cancelled_new_rental_is_replaced_in_same_pick(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "cancelled@gmail.com",
                "responseHeader": {"rentalId": "rent-cancelled", "status": "CANCELLED"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "fresh@gmail.com",
                "responseHeader": {"rentalId": "rent-fresh", "status": "WAITING_OTP"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        account = client.pick_account()

        self.assertEqual(account.rental_id, "rent-fresh")
        self.assertEqual(account.email, "fresh@gmail.com")
        self.assertEqual(request.call_count, 2)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_three_cancelled_new_rentals_return_bounded_error(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": f"cancelled-{index}@gmail.com",
                "responseHeader": {"rentalId": f"rent-cancelled-{index}", "status": "CANCELLED"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            })
            for index in range(3)
        ]

        with self.assertRaisesRegex(client.BambooMmoError, "đã thử 3 rental mới"):
            client.pick_account()

        self.assertEqual(request.call_count, 3)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_cancelled_rental_is_quarantined_for_following_pick(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "old@gmail.com",
                "responseHeader": {"rentalId": "rent-old", "status": "WAITING_OTP"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 400,
                "message": "Rental cancelled",
                "responseHeader": {"rentalId": "rent-old", "status": "CANCELLED"},
                "isSuccessStatusCode": False,
                "resourceKey": "RENTAL_CANCELLED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "fresh@gmail.com",
                "responseHeader": {"rentalId": "rent-fresh", "status": "WAITING_OTP"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        old = client.pick_account()
        with self.assertRaises(client.BambooMmoError):
            client.fetch_latest_otp(old.email, max_wait=0)
        fresh = client.pick_account()

        self.assertEqual(fresh.rental_id, "rent-fresh")
        self.assertEqual(client.pool_summary()["failed"], 12)
        self.assertEqual(request.call_count, 3)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_cancelled_rent_again_is_quarantined_for_following_pick(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "old@gmail.com",
                "responseHeader": {
                    "rentalId": "rent-old",
                    "status": "WAITING_OTP",
                    "canRequestNextOtp": False,
                },
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "123456",
                "responseHeader": {
                    "rentalId": "rent-old",
                    "status": "COMPLETED",
                    "canRequestNextOtp": True,
                },
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 400,
                "message": "Rental cancelled",
                "isSuccessStatusCode": False,
                "resourceKey": "RENTAL_CANCELLED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "fresh@gmail.com",
                "responseHeader": {"rentalId": "rent-fresh", "status": "WAITING_OTP"},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        old = client.pick_account()
        self.assertEqual(client.fetch_latest_otp(old.email, max_wait=0), "123456")
        with self.assertRaises(client.BambooMmoError):
            client.fetch_latest_otp(old.email, max_wait=0, before_code="123456")
        fresh = client.pick_account()

        self.assertEqual(fresh.rental_id, "rent-fresh")
        self.assertEqual(client.pool_summary()["failed"], 12)
        self.assertEqual(request.call_count, 4)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_fetch_otp_then_requests_next_code_on_follow_up(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-2", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "123456",
                "responseHeader": {"rentalId": "rent-2", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-2", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "NEXT_OTP_REQUESTED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "654321",
                "responseHeader": {"rentalId": "rent-2", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        account = client.pick_account()
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0), "123456")
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0, before_code="123456"), "654321")
        self.assertEqual(request.call_args_list[2].args[:2], ("POST", "https://api.bamboommo.example/api/mail/get-mail-rent-again-apikey"))
        self.assertEqual(request.call_args_list[2].kwargs["json"], {
            "apiKey": "test-key",
            "server": 2,
            "rentalId": "rent-2",
        })

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_aliases_share_rental_next_otp_state(self, request, config):
        config.return_value = self._config()
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-shared", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "123456",
                "responseHeader": {"rentalId": "rent-shared", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-shared", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "NEXT_OTP_REQUESTED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "654321",
                "responseHeader": {"rentalId": "rent-shared", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        first = client.pick_account()
        second = client.pick_account()
        self.assertEqual(client.fetch_latest_otp(first.email, max_wait=0), "123456")
        self.assertEqual(client.fetch_latest_otp(second.email, max_wait=0), "654321")
        self.assertEqual(request.call_args_list[2].kwargs["json"]["rentalId"], "rent-shared")

    @patch("core.bamboommo_client._config", return_value=(
        "https://api.bamboommo.example", "test-key", 1, "GM", "OP", 7, 1, 2,
    ))
    @patch("core.bamboommo_client.requests.request")
    def test_server_one_rent_again_uses_mail_and_service_fields(self, request, config):
        request.side_effect = [
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-3", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "111111",
                "responseHeader": {"rentalId": "rent-3", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "user@gmail.com",
                "responseHeader": {"rentalId": "rent-3", "status": "WAITING_OTP", "canRequestNextOtp": False},
                "isSuccessStatusCode": True,
                "resourceKey": "NEXT_OTP_REQUESTED",
            }),
            self._response({
                "statusCode": 200,
                "responseData": "222222",
                "responseHeader": {"rentalId": "rent-3", "status": "COMPLETED", "canRequestNextOtp": True},
                "isSuccessStatusCode": True,
                "resourceKey": "SUCCESS",
            }),
        ]

        account = client.pick_account()
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0), "111111")
        self.assertEqual(client.fetch_latest_otp(account.email, max_wait=0), "222222")
        self.assertEqual(request.call_args_list[2].kwargs["json"], {
            "apiKey": "test-key",
            "server": 1,
            "mail": "user@gmail.com",
            "codeTypeMail": "GM",
            "codeService": "OP",
        })

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_release_account_preserves_explicit_available_status(self, request, config):
        config.return_value = self._config()
        request.return_value = self._response({
            "statusCode": 200,
            "responseData": "user@gmail.com",
            "responseHeader": {"rentalId": "rent-4", "status": "WAITING_OTP"},
            "isSuccessStatusCode": True,
            "resourceKey": "SUCCESS",
        })

        account = client.pick_account()
        self.assertTrue(client.release_account(account.email, status="available"))
        self.assertEqual(client.pool_summary()["available"], 12)

    @patch("core.bamboommo_client._config")
    @patch("core.bamboommo_client.requests.request")
    def test_list_accounts_reports_shared_alias_capacity(self, request, config):
        config.return_value = self._config()
        request.return_value = self._response({
            "statusCode": 200,
            "responseData": "user@gmail.com",
            "responseHeader": {"rentalId": "rent-5", "status": "WAITING_OTP"},
            "isSuccessStatusCode": True,
            "resourceKey": "SUCCESS",
        })

        account = client.pick_account()
        row = next(item for item in client.list_accounts() if item["email"] == account.email)
        self.assertEqual(row["alias_total"], 12)
        self.assertEqual(row["alias_reserved"], 1)
        self.assertEqual(row["alias_available"], 11)


if __name__ == "__main__":
    unittest.main()
