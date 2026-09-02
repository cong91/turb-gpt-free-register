import unittest
from unittest.mock import patch

from config import email as email_config
from config import register as register_config
from core import registration_service
from core.account_export import save_account_data
from webui.app import create_app


class PaymeshIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_paymesh_without_cards(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "paymesh"
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "paymesh_cdks": [],
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("MAIL card", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_explicit_paymesh_provider_submits_deduplicated_cards(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "outlook"
        ), patch.object(email_config, "PAYMESH_API_BASE", "https://sms.paymesh.cn"), patch.object(
            email_config, "PAYMESH_ACCOUNTS_PER_CDK", 6
        ):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 2,
                "email_source": "sms.paymesh.cn",
                "paymesh_cdks": ["MAIL-ONE", "MAIL-ONE", "MAIL-TWO"],
            })

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(
            count=1,
            workers=2,
            email_source="paymesh",
            paymesh_cdks=["MAIL-ONE", "MAIL-TWO"],
        )

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="user@example.com")
    def test_registration_job_passes_paymesh_cards_to_provider(self, acquire_email, _birthday):
        registration_service._set_job_email_inputs(
            31,
            [],
            paymesh_cdks=["MAIL-ONE"],
            email_source="paymesh",
        )
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            email, name, birthday = registration_service._prepare_registration_args(job_id=31)

        self.assertEqual((email, name, birthday), ("user@example.com", "Alice", "1990-01-01"))
        acquire_email.assert_called_once_with(
            job_id=31,
            paymesh_cdks=["MAIL-ONE"],
            email_source="paymesh",
        )
        registration_service._clear_job_email_inputs(31)

    @patch("core.plan_check_service.enqueue_account_plan_check", return_value={"accepted": True})
    @patch("core.email_provider.mark_email_consumed", return_value=True)
    @patch("core.paymesh_mail_client.get_account_context")
    @patch("core.account_export._append_batch_archive", return_value="batch")
    @patch("core.db.insert_account", return_value=23)
    def test_account_export_saves_paymesh_source_card_before_consume(
        self, insert_account, _archive, get_context, mark_consumed, _enqueue
    ):
        get_context.return_value.cdk = "MAIL-EXACT"

        row_id = save_account_data(
            email="user@example.com",
            access_token="token",
            email_source="paymesh",
            registration_ip="8.8.8.8",
            extra={"network_identity": {"verified": True}},
        )

        self.assertEqual(row_id, 23)
        self.assertEqual(insert_account.call_args.kwargs["source_cdk"], "MAIL-EXACT")
        self.assertEqual(insert_account.call_args.kwargs["registration_ip"], "8.8.8.8")
        self.assertTrue(
            insert_account.call_args.kwargs["extra"]["network_identity"]["verified"]
        )
        mark_consumed.assert_called_once_with("user@example.com")

    def test_registration_form_exposes_paymesh_card_input(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        source = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('value="paymesh"', source)
        self.assertIn("paymeshCdkBatch", source)
        self.assertIn("paymesh_cdks", source)
        self.assertIn("Mỗi dòng một MAIL card", source)
        self.assertIn("data-paymesh-routed-domain", source)
        self.assertIn("test.com", source)

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_passes_paymesh_routed_domains_to_service(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "paymesh"
        ), patch.object(email_config, "PAYMESH_API_BASE", "https://sms.paymesh.cn"), patch.object(
            email_config, "PAYMESH_ACCOUNTS_PER_CDK", 6
        ), patch.object(email_config, "PAYMESH_ROUTED_DOMAINS", []):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "paymesh",
                "paymesh_cdks": ["MAIL-ONE"],
                "paymesh_routed_domains": ["test.com"],
            })

        self.assertEqual(response.status_code, 200)
        submit_registration.assert_called_once_with(
            count=1,
            workers=1,
            email_source="paymesh",
            paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["test.com"],
        )

    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_rejects_paymesh_with_too_many_routed_domains(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "paymesh"
        ), patch.object(email_config, "PAYMESH_API_BASE", "https://sms.paymesh.cn"), patch.object(
            email_config, "PAYMESH_ACCOUNTS_PER_CDK", 6
        ), patch.object(email_config, "PAYMESH_ROUTED_DOMAINS", []):
            response = self.client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "email_source": "paymesh",
                "paymesh_cdks": ["MAIL-ONE"],
                "paymesh_routed_domains": ["a.test", "b.test", "c.test"],
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("tối đa", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("core.profile_utils.generate_random_birthday", return_value="1990-01-01")
    @patch("core.email_provider.acquire_email", return_value="user@test.com")
    def test_registration_job_passes_paymesh_routed_domains_to_provider(self, acquire_email, _birthday):
        registration_service._set_job_email_inputs(
            41,
            [],
            paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["test.com"],
            email_source="paymesh",
        )
        with patch.object(register_config, "REGISTER_EMAIL", ""), patch.object(
            register_config, "REGISTER_NAME", "Alice"
        ), patch.object(email_config, "USE_EMAIL_SERVICE", True):
            email, name, birthday = registration_service._prepare_registration_args(job_id=41)

        self.assertEqual((email, name, birthday), ("user@test.com", "Alice", "1990-01-01"))
        acquire_email.assert_called_once_with(
            job_id=41,
            paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["test.com"],
            email_source="paymesh",
        )
        registration_service._clear_job_email_inputs(41)


if __name__ == "__main__":
    unittest.main()
