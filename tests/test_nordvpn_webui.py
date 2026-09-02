import unittest
from unittest.mock import MagicMock, patch

from webui.app import create_app
from webui.registration_jobs_api import create_registration_jobs


class NordVPNWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("config.reload_all")
    @patch("webui.config_editor.update_config")
    def test_nordvpn_save_reports_reload_without_claiming_connection(
        self, update_config, reload_all
    ):
        update_config.return_value = {
            "updated": ["NORDVPN_ENABLED"],
            "ignored": [],
        }

        response = self.client.post(
            "/api/config",
            json={"updates": {"NORDVPN_ENABLED": True}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["reloaded"])
        self.assertIn("保存并热加载", response.json["note"])
        self.assertNotIn("配置已生效", response.json["note"])
        update_config.assert_called_once_with({"NORDVPN_ENABLED": True})
        reload_all.assert_called_once_with()

    @patch("config.nordvpn.NORDVPN_ENABLED", True)
    @patch("core.nordvpn_cli.is_connected", return_value=False)
    @patch("core.nordvpn_cli.is_service_running", return_value=True)
    def test_nordvpn_status_reports_read_only_runtime_state(
        self, service_running, connected
    ):
        response = self.client.get("/api/nordvpn/status")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertTrue(response.json["configured"])
        self.assertTrue(response.json["service_running"])
        self.assertFalse(response.json["connected"])
        self.assertFalse(response.json["ready"])
        service_running.assert_called_once_with()
        connected.assert_called_once_with()

    def test_nordvpn_rotation_retry_requires_authentication(self):
        client = create_app(auth_code="test-auth").test_client()

        response = client.post("/api/nordvpn/rotation/retry")

        self.assertEqual(response.status_code, 401)

    @patch("config.nordvpn.NORDVPN_ENABLED", False)
    def test_nordvpn_rotation_retry_requires_pending_rotation(self):
        response = self.client.post("/api/nordvpn/rotation/retry")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["ok"])
        self.assertIn("没有待重试", response.json["error"])

    @patch("config.nordvpn.NORDVPN_ENABLED", True)
    @patch("config.nordvpn.NORDVPN_AUTO_ROTATE_ENABLED", True)
    @patch("config.nordvpn.NORDVPN_AUTO_ROTATE_INTERVAL", 3)
    def test_registration_response_reports_forced_single_worker(self):
        service = MagicMock()
        service.submit_registration.return_value = [{"id": 1}]
        service.effective_registration_workers.return_value = 1
        database = MagicMock()
        database.outlook_pool_summary.return_value = {"available": 1}

        payload, status = create_registration_jobs(
            {"count": 1, "workers": 8, "email_source": "outlook"},
            service=service,
            database=database,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["workers"], 1)
        service.submit_registration.assert_called_once_with(
            count=1,
            workers=8,
            email_source="outlook",
        )


if __name__ == "__main__":
    unittest.main()
