import unittest
from unittest.mock import patch

from config import email as email_config
from core.qan8_gmail_api_client import (
    Qan8DeliveryError,
    Qan8GmailApiClient,
    Qan8GmailApiError,
    Qan8OrderUnknownError,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = "response"

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Qan8GmailApiClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Qan8GmailApiClient(
            api_base="https://shop.example",
            api_key="secret-api-key",
            sku_id=156,
            request_timeout=3,
            proxy_url="",
        )

    @patch("core.qan8_gmail_api_client.requests.get")
    def test_list_products_uses_documented_path_without_key(self, mock_get):
        mock_get.return_value = _Response({"success": True, "data": [{"sku_id": 156}]})

        result = self.client.list_products()

        self.assertEqual(result, [{"sku_id": 156}])
        mock_get.assert_called_once_with(
            "https://shop.example/api/v1/open/products",
            timeout=3,
        )

    @patch("core.qan8_gmail_api_client.requests.get")
    def test_get_balance_uses_authenticated_documented_path(self, mock_get):
        mock_get.return_value = _Response({"success": True, "data": {"balance": 9.5}})

        result = self.client.get_balance()

        self.assertEqual(result, {"balance": 9.5})
        mock_get.assert_called_once_with(
            "https://shop.example/api/v1/open/balance",
            params={"api_key": "secret-api-key"},
            timeout=3,
        )

    @patch("core.qan8_gmail_api_client.requests.get")
    def test_configured_proxy_is_passed_to_get_requests(self, mock_get):
        mock_get.return_value = _Response({"success": True, "data": []})
        client = Qan8GmailApiClient(
            api_base="https://shop.example",
            api_key="secret-api-key",
            proxy_url="http://proxy.example:8080",
        )

        self.assertEqual(client.list_products(), [])
        mock_get.assert_called_once_with(
            "https://shop.example/api/v1/open/products",
            timeout=15,
            proxies={
                "http": "http://proxy.example:8080",
                "https": "http://proxy.example:8080",
            },
        )

    @patch("core.qan8_gmail_api_client.requests.post")
    def test_configured_proxy_is_passed_to_post_requests(self, mock_post):
        mock_post.return_value = _Response({
            "success": True,
            "data": {"order_no": "out-proxy", "status": "processing"},
        })
        client = Qan8GmailApiClient(
            api_base="https://shop.example",
            api_key="secret-api-key",
            sku_id=156,
            proxy_url="socks5h://proxy.example:1080",
        )

        client.create_order("out-proxy")

        self.assertEqual(
            mock_post.call_args.kwargs["proxies"],
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )

    @patch.object(email_config, "QAN8_API_PROXY", "http://proxy.example:8080", create=True)
    def test_client_reads_proxy_from_dynamic_email_config(self):
        client = Qan8GmailApiClient(api_base="https://shop.example")

        self.assertEqual(client.proxy_url, "http://proxy.example:8080")

    def test_client_rejects_unsupported_proxy_scheme(self):
        with self.assertRaisesRegex(Qan8GmailApiError, "proxy"):
            Qan8GmailApiClient(api_base="https://shop.example", proxy_url="ftp://proxy.example:21")

    @patch("core.qan8_gmail_api_client.requests.post")
    def test_create_order_forces_quantity_one_and_returns_snapshot(self, mock_post):
        mock_post.return_value = _Response({
            "success": True,
            "data": {"order_no": "out-1", "status": "processing"},
        })

        result = self.client.create_order("out-1")

        self.assertEqual(result.order_no, "out-1")
        self.assertEqual(result.status, "processing")
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["quantity"], 1)
        self.assertEqual(body["sku_id"], 156)
        self.assertEqual(body["api_key"], "secret-api-key")

    @patch("core.qan8_gmail_api_client.requests.post")
    def test_create_order_preserves_known_provider_rejection(self, mock_post):
        mock_post.return_value = _Response({
            "success": False,
            "message": "insufficient balance",
        })

        with self.assertRaisesRegex(Qan8GmailApiError, "insufficient balance"):
            self.client.create_order("out-rejected")

    @patch("core.qan8_gmail_api_client.requests.post")
    def test_http_rejection_is_not_classified_as_unknown_order(self, mock_post):
        mock_post.return_value = _Response(
            {"success": False, "message": "order endpoint not found"},
            status_code=404,
        )

        with self.assertRaisesRegex(Qan8GmailApiError, "HTTP 404"):
            self.client.create_order("out-http-rejected")

        mock_post.assert_called_once()

    def test_create_order_rejects_non_quantity_one_without_http(self):
        with self.assertRaises(ValueError):
            self.client.create_order("out-1", quantity=2)

    @patch("core.qan8_gmail_api_client.requests.get")
    def test_get_order_uses_order_lookup_and_normalizes_data(self, mock_get):
        mock_get.return_value = _Response({
            "ok": True,
            "data": {
                "order_no": "out-1",
                "status": "completed",
                "delivery": "user@gmail.com----https://mail.example/code",
                "message": "done",
            },
        })

        result = self.client.get_order("out-1")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.delivery, "user@gmail.com----https://mail.example/code")
        mock_get.assert_called_once_with(
            "https://shop.example/api/v1/open/orders/out-1",
            params={"api_key": "secret-api-key"},
            timeout=3,
        )

    @patch("core.qan8_gmail_api_client.requests.post", side_effect=RuntimeError("connection lost"))
    def test_create_timeout_has_unknown_outcome_without_secret(self, _mock_post):
        with self.assertRaises(Qan8OrderUnknownError) as raised:
            self.client.create_order("out-2")

        self.assertIn("out-2", str(raised.exception))
        self.assertNotIn("secret-api-key", str(raised.exception))

    def test_parse_delivery_accepts_deduplicated_gmail_records(self):
        delivery = (
            "user@gmail.com----https://mail.example/a\n"
            "user@gmail.com----https://mail.example/a\n"
            "other@googlemail.com----http://mail.example/b"
        )

        result = self.client.parse_delivery(delivery)

        self.assertEqual([item.email for item in result], ["user@gmail.com", "other@googlemail.com"])

    def test_parse_delivery_rejects_unsupported_or_credential_lines(self):
        values = [
            "",
            "user@example.com----https://mail.example/a",
            "user@gmail.com----not-a-url",
            "user@gmail.com----https://mail.example/a----password",
            {"email": "user@gmail.com", "code_url": "https://mail.example/a"},
        ]

        for value in values:
            with self.subTest(value=value), self.assertRaises(Qan8DeliveryError):
                self.client.parse_delivery(value)

    @patch("core.qan8_gmail_api_client.logger")
    @patch("core.qan8_gmail_api_client.requests.post", side_effect=RuntimeError("failed"))
    def test_client_never_logs_api_key(self, _mock_post, mock_logger):
        with self.assertRaises(Qan8OrderUnknownError):
            self.client.create_order("out-3")

        logged = " ".join(str(call) for call in mock_logger.method_calls)
        self.assertNotIn("secret-api-key", logged)


if __name__ == "__main__":
    unittest.main()
