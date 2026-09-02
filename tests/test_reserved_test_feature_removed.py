import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import registration_service
from webui.app import create_app
from webui.registration_jobs_api import create_registration_jobs

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


class ReservedTestFeatureRemovedTests(unittest.TestCase):
    def test_webui_has_no_reserved_test_tool_or_local_test_mode(self):
        html = TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn('data-tab="tools"', html)
        self.assertNotIn("_reserved_test_aliases_tool.html", html)
        self.assertNotIn("reserved_test_aliases.css", html)
        self.assertNotIn("reserved_test_aliases.js", html)
        self.assertNotIn('value="local_test"', html)
        self.assertNotIn('data-provider-input="local_test"', html)
        self.assertNotIn("Tạo bí danh email kiểm thử", html)
        self.assertNotIn("Tạo测试Email别名", html)

    def test_reserved_test_alias_endpoint_is_removed(self):
        client = create_app(auth_code="test-auth").test_client()

        response = client.post(
            "/api/tools/reserved-test-aliases/preview",
            headers={"X-Auth-Code": "test-auth"},
            json={"base": "abcdef", "domains": ["mail.test"], "limit": 6},
        )

        self.assertEqual(response.status_code, 404)

    def test_local_test_registration_source_is_rejected(self):
        service = MagicMock()
        database = MagicMock()

        payload, status = create_registration_jobs(
            {
                "count": 1,
                "workers": 1,
                "email_source": "local_test",
                "local_test_base": "sampleuser",
                "local_test_domains": ["mail.test"],
            },
            service=service,
            database=database,
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        service.submit_local_test_registration.assert_not_called()

    @patch("main.run_registration")
    @patch("core.registration_service._notify_sub2api_automation_job")
    @patch("core.registration_service._deactivate_job")
    @patch("core.registration_service._activate_job", return_value=True)
    @patch("core.registration_service.db.update_job")
    @patch("core.registration_service.db.get_job")
    @patch("core.registration_service._prepare_registration_args")
    @patch("core.registration_service._release_unconsumed_job_email")
    def test_legacy_local_test_job_is_cancelled_without_registration(
        self,
        release_unconsumed_job_email,
        prepare_registration_args,
        get_job,
        update_job,
        activate_job,
        deactivate_job,
        notify_job,
        run_registration,
    ):
        get_job.return_value = {
            "id": 42,
            "status": "pending",
            "job_type": "local_test",
            "email": "sampleuser@mail.test",
        }
        prepare_registration_args.return_value = (
            "sampleuser@mail.test",
            "Test User",
            "1990-01-01",
        )

        registration_service._run_one_job(42, "unused.log")

        self.assertTrue(
            any(call.kwargs.get("status") == "cancelled" for call in update_job.call_args_list)
        )
        run_registration.assert_not_called()
        release_unconsumed_job_email.assert_not_called()
        deactivate_job.assert_called_once_with(42)
        notify_job.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
