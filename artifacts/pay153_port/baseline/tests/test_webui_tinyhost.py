# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class TinyHostWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_reject_tinyhost_without_api_base(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "tinyhost"
        ), patch.object(email_config, "TINYHOST_API_BASE", "", create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("TinyHost API 地址", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_tinyhost_submits_existing_registration_flow(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ), patch.object(email_config, "TINYHOST_API_BASE", "https://tinyhost.shop", create=True):
            response = self.client.post(
                "/api/jobs",
                json={"count": 1, "workers": 1, "email_source": "tinyhost"},
            )

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(count=1, workers=1, email_source="tinyhost")

    def test_registration_template_exposes_tinyhost_source(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="tinyhost"', response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
