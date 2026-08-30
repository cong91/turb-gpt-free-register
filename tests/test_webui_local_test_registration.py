# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class LocalTestRegistrationWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    @patch("webui.app.svc.submit_local_test_registration", return_value=[{"id": 1}, {"id": 2}])
    def test_local_test_submits_generated_aliases_before_manual_email_checks(
        self,
        submit_local_test_registration,
        submit_registration,
    ):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            response = self.client.post(
                "/api/jobs",
                json={
                    "count": 2,
                    "workers": 3,
                    "email_source": "local_test",
                    "local_test_base": "sampleuser",
                    "local_test_domains": ["Mail.TEST.", "inbox.invalid"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(
            response.json["warning"],
            "Kiểm thử cục bộ dry-run: không gọi OpenAI, trình duyệt, OTP hoặc nhà cung cấp email.",
        )
        submit_local_test_registration.assert_called_once()
        aliases = submit_local_test_registration.call_args.kwargs["aliases"]
        self.assertEqual(aliases[:2], ["sampleuser@mail.test", "sampleuser@inbox.invalid"])
        self.assertEqual(submit_local_test_registration.call_args.kwargs["workers"], 3)
        submit_registration.assert_not_called()

    @patch("webui.app.db.list_jobs")
    def test_job_list_marks_local_test_without_manual_otp(self, list_jobs):
        list_jobs.return_value = [{
            "id": 4,
            "job_type": "local_test",
            "email_source": "reserved_test",
            "email": "sampleuser@mail.test",
            "status": "success",
        }]

        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            response = self.client.get("/api/jobs?paged=1&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        row = response.json["items"][0]
        self.assertEqual(row["job_type"], "local_test")
        self.assertEqual(row["email_source"], "reserved_test")
        self.assertNotIn("manual_otp_required", row)
        self.assertNotIn("retryable", row)

    @patch("webui.app.svc.submit_local_test_registration")
    @patch("webui.app.svc.submit_registration")
    def test_local_test_rejects_invalid_inputs_without_creating_jobs(
        self,
        submit_registration,
        submit_local_test_registration,
    ):
        payloads = (
            {"local_test_base": "sampleuser", "local_test_domains": ["gmail.com"]},
            {"local_test_base": "sampleuser", "local_test_domains": []},
            {
                "local_test_base": "sampleuser",
                "local_test_domains": ["one.test", "two.invalid", "three.example"],
            },
            {"local_test_base": "bad.base", "local_test_domains": ["mail.test"]},
        )
        for fields in payloads:
            with self.subTest(fields=fields):
                response = self.client.post(
                    "/api/jobs",
                    json={
                        "count": 2,
                        "workers": 1,
                        "email_source": "local_test",
                        **fields,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])

        submit_local_test_registration.assert_not_called()
        submit_registration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
