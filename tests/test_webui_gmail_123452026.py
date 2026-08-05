# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class Gmail123452026WebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_gmail_cdk_without_request_cards(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_123452026"
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "gmail_cdks": []})

        self.assertEqual(response.status_code, 400)
        self.assertIn("CDK", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_accepts_plain_http_without_opt_in(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_123452026"
        ), patch.object(
            email_config, "GMAIL_123452026_API_BASE", "http://gmail.123452026.xyz/api", create=True
        ), patch.object(email_config, "GMAIL_123452026_ALLOW_INSECURE_HTTP", False, create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "gmail_cdks": ["CDK-ONE"]})

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(count=1, workers=1, gmail_cdks=["CDK-ONE"])

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_valid_gmail_cdk_config_submits_without_outlook_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_123452026"
        ), patch.object(
            email_config, "GMAIL_123452026_API_BASE", "http://gmail.123452026.xyz/api", create=True
        ), patch.object(email_config, "GMAIL_123452026_ALLOW_INSECURE_HTTP", True, create=True), patch.object(
            email_config, "GMAIL_123452026_ACCOUNTS_PER_CDK", 6, create=True
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1, "workers": 1, "gmail_cdks": ["CDK-ONE", "CDK-TWO"]
            })

        self.assertEqual(response.status_code, 200)
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(
            count=1, workers=1, gmail_cdks=["CDK-ONE", "CDK-TWO"]
        )

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_explicit_gmail_provider_overrides_configured_source(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ), patch.object(
            email_config, "GMAIL_123452026_API_BASE", "http://gmail.123452026.xyz/api", create=True
        ), patch.object(email_config, "GMAIL_123452026_ALLOW_INSECURE_HTTP", True, create=True):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "gmail_123452026",
                "gmail_cdks": ["CDK-ONE"],
            })

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(
            count=1,
            workers=1,
            email_source="gmail_123452026",
            gmail_cdks=["CDK-ONE"],
        )

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_forward_normalized_gmail_routed_domains(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ), patch.object(
            email_config, "GMAIL_123452026_API_BASE", "https://mail.example.com/api", create=True
        ):
            response = self.client.post("/api/jobs", json={
                "count": 12,
                "workers": 3,
                "email_source": "gmail_123452026",
                "gmail_cdks": ["CDK-ONE", "CDK-TWO"],
                "gmail_routed_domains": [" Route-One.NET. ", "route-two.org"],
            })

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(
            count=12,
            workers=3,
            email_source="gmail_123452026",
            gmail_cdks=["CDK-ONE", "CDK-TWO"],
            gmail_routed_domains=["route-one.net", "route-two.org"],
        )

    @patch("webui.app.svc.submit_registration")
    def test_jobs_reject_reserved_gmail_routed_domain(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gmail_123452026"
        ), patch.object(
            email_config, "GMAIL_123452026_API_BASE", "https://mail.example.com/api", create=True
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "gmail_cdks": ["CDK-ONE"],
                "gmail_routed_domains": ["mail.test"],
            })

        self.assertEqual(response.status_code, 400)
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_unknown_explicit_provider_is_rejected(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "unknown-provider",
            })

        self.assertEqual(response.status_code, 400)
        submit_registration.assert_not_called()

    def test_registration_form_exposes_batch_cdk_input(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        source = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("gmailCdkBatch", source)
        self.assertIn("Mỗi dòng một CDK", source)
        self.assertIn("data-cdk-count", source)
        self.assertIn("data-email-provider", source)
        self.assertIn("data-provider-input", source)
        self.assertIn("email_source", source)
        self.assertEqual(source.count("<input data-gmail-routed-domain"), 2)
        self.assertIn("gmail_routed_domains", source)
        self.assertIn("routedDomains.length", source)


if __name__ == "__main__":
    unittest.main()
