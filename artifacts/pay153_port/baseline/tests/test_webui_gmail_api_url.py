"""Gmail API URL alias inventory and reset WebUI tests."""
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class GmailApiUrlWebUiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.list_gmail_api_url_email_pool")
    def test_pool_endpoint_returns_alias_inventory_fields(self, list_pool):
        list_pool.return_value = [{
            "email": "source@gmail.com",
            "status": "available",
            "source": "gmail_api_url",
            "alias_total": 12,
            "alias_available": 10,
            "alias_used": 2,
            "alias_reserved": 0,
        }]

        response = self.client.get(
            "/api/outlook?source=gmail_api_url&paged=1&page=1&page_size=20"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["alias_available"], 10)
        self.assertEqual(response.get_json()["items"][0]["alias_total"], 12)

    @patch("webui.app.db.reset_gmail_api_url_aliases")
    def test_alias_reset_endpoint_returns_reset_counts(self, reset_aliases):
        reset_aliases.return_value = {
            "reset_aliases": 10,
            "alias_total": 12,
            "alias_available": 10,
            "alias_used": 2,
            "alias_reserved": 0,
            "source_status": "available",
        }

        response = self.client.post(
            "/api/outlook/gmail-aliases/reset",
            json={"email": "source@gmail.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reset_aliases"], 10)
        reset_aliases.assert_called_once_with("source@gmail.com")

    def test_alias_reset_endpoint_requires_authentication(self):
        client = self.app.test_client()
        response = client.post(
            "/api/outlook/gmail-aliases/reset",
            json={"email": "source@gmail.com"},
        )

        self.assertIn(response.status_code, {401, 403})

    def test_pool_markup_exposes_alias_column_and_reset_action(self):
        template = (
            Path(__file__).resolve().parent.parent
            / "webui"
            / "templates"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('class="col-alias"', template)
        self.assertIn("alias_available", template)
        self.assertIn("reset-aliases", template)
        self.assertIn("/api/outlook/gmail-aliases/reset", template)


if __name__ == "__main__":
    unittest.main()
