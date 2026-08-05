# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class ReservedTestAliasWebUiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def test_preview_requires_authentication(self):
        response = self.client.post(
            "/api/tools/reserved-test-aliases/preview",
            json={"base": "abcdef", "domains": ["mail.test"], "limit": 6},
        )

        self.assertEqual(response.status_code, 401)

    @patch("core.email_provider.acquire_email")
    @patch("webui.app.svc.submit_registration")
    def test_preview_generates_aliases_without_registration_or_provider_calls(
        self,
        submit_registration,
        acquire_email,
    ):
        response = self.client.post(
            "/api/tools/reserved-test-aliases/preview",
            headers={"X-Auth-Code": "test-auth"},
            json={
                "base": "abcdef",
                "domains": ["mail.test", "inbox.invalid"],
                "limit": 7,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["count"], 7)
        self.assertEqual(
            response.json["aliases"][:2],
            ["abcdef@mail.test", "abcdef@inbox.invalid"],
        )
        submit_registration.assert_not_called()
        acquire_email.assert_not_called()

    def test_preview_rejects_real_domains_and_invalid_counts(self):
        for payload in (
            {"base": "abcdef", "domains": ["gmail.com"], "limit": 6},
            {"base": "abcdef", "domains": ["mail.test"], "limit": 0},
            {"base": "abcdef", "domains": ["mail.test"], "limit": 201},
            {"base": "abcdef", "domains": "mail.test", "limit": 6},
            {
                "base": "abcdef",
                "domains": ["one.test", "two.invalid", "three.example"],
                "limit": 6,
            },
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/tools/reserved-test-aliases/preview",
                    headers={"X-Auth-Code": "test-auth"},
                    json=payload,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])

    def test_preview_rejects_non_object_json(self):
        response = self.client.post(
            "/api/tools/reserved-test-aliases/preview",
            headers={"X-Auth-Code": "test-auth"},
            json=["invalid"],
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
