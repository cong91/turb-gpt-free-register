import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.exceptions import RequestEntityTooLarge

from core.roxy_profile_manager import RoxyProfileManagerStateError
from webui.app import create_app
from webui.roxy_profiles_api import _save_upload_limited


class RoxyProfilesApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def test_required_profile_manager_routes_are_registered(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        expected = {
            "/api/roxy/profiles",
            "/api/roxy/profiles/import",
            "/api/roxy/profiles/bulk",
            "/api/roxy/profiles/reconcile",
            "/api/roxy/profiles/<local_id>/export-full",
            "/api/roxy/profiles/<local_id>/open-local",
            "/api/roxy/profiles/<local_id>/close-local",
            "/api/roxy/profiles/<local_id>/local-status",
            "/api/roxy/profiles/<local_id>/archive/download",
        }
        self.assertTrue(expected.issubset(rules), expected - rules)

    def test_profile_manager_routes_require_authentication(self):
        response = self.client.post("/api/roxy/profiles/local/open-local", json={})
        self.assertEqual(response.status_code, 401)

    def test_profile_manager_mutations_require_same_origin(self):
        response = self.client.post(
            "/api/roxy/profiles/local/open-local",
            headers={"X-Auth-Code": "test-auth", "Origin": "https://untrusted.invalid"},
            json={},
        )
        self.assertEqual(response.status_code, 403)

    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_profile_listing_passes_search_filter_and_pagination(self, manager_type):
        manager = manager_type.return_value
        manager.list_profiles.return_value = []
        manager.count_profiles.return_value = 41
        manager.status.return_value = {"managed_count": 0}
        response = self.client.get(
            "/api/roxy/profiles?page=2&page_size=20&search=demo&state=TRASHED",
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 200)
        manager.list_profiles.assert_called_once_with(
            reconcile=False, search="demo", state="TRASHED", page=2, page_size=20
        )
        manager.count_profiles.assert_called_once_with(search="demo", state="TRASHED")
        manager.status.assert_called_once_with(include_remote=False)
        self.assertEqual(response.get_json()["total"], 41)
        self.assertTrue(response.get_json()["has_next"])

    def test_upload_stream_limit_does_not_trust_content_length(self):
        upload = Mock()
        upload.stream = io.BytesIO(b"12345")
        target = io.BytesIO()
        with self.assertRaises(RequestEntityTooLarge):
            _save_upload_limited(upload, target, max_bytes=4)
        self.assertLessEqual(len(target.getvalue()), 4)

    def test_invalid_pagination_is_rejected(self):
        response = self.client.get(
            "/api/roxy/profiles?page=invalid",
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 400)

    def test_import_rejects_arbitrary_json_fields(self):
        response = self.client.post(
            "/api/roxy/profiles",
            headers={"X-Auth-Code": "test-auth", "Origin": "http://localhost"},
            json={"name": "profile", "archive_path": "C:\\secrets"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_import_upload_uses_temporary_file_not_client_path(self, manager_type):
        manager = manager_type.return_value
        manager.import_full_profile.return_value = {"profile": {"state": "OFFLINE_STOPPED"}}
        response = self.client.post(
            "/api/roxy/profiles/import",
            headers={"X-Auth-Code": "test-auth", "Origin": "http://localhost"},
            data={"name": "Imported", "archive": (io.BytesIO(b"RPA2-test"), "profile.rpa2")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        archive_path = Path(manager.import_full_profile.call_args.args[0])
        self.assertNotEqual(archive_path.name, "profile.rpa2")
        self.assertFalse(archive_path.exists())

    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_import_route_rejects_stream_over_configured_limit(self, manager_type):
        with patch(
            "webui.roxy_profiles_api._manager_cfg.ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES",
            4,
        ):
            response = self.client.post(
                "/api/roxy/profiles/import",
                headers={"X-Auth-Code": "test-auth", "Origin": "http://localhost"},
                data={
                    "name": "Imported",
                    "archive": (io.BytesIO(b"12345"), "profile.rpa2"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 413)
        manager_type.return_value.import_full_profile.assert_not_called()

    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_state_conflict_maps_to_409(self, manager_type):
        manager_type.return_value.open_offline_profile.side_effect = RoxyProfileManagerStateError("disabled")
        response = self.client.post(
            "/api/roxy/profiles/local/open-local",
            headers={"X-Auth-Code": "test-auth"},
            json={},
        )
        self.assertEqual(response.status_code, 409)

    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_download_sets_attachment_headers(self, manager_type):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "archive.rpa2"
            path.write_bytes(b"encrypted")
            manager_type.return_value.archive_path.return_value = path
            response = self.client.get(
                "/api/roxy/profiles/local/archive/download",
                headers={"X-Auth-Code": "test-auth"},
                buffered=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn(".rpa2", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["Cache-Control"], "no-cache, max-age=0")
            response.close()

    @patch("main.run_registration")
    @patch("webui.roxy_profiles_api.RoxyProfileManager")
    def test_manager_routes_do_not_invoke_registration(self, manager_type, run_registration):
        manager = manager_type.return_value
        manager.list_profiles.return_value = []
        manager.count_profiles.return_value = 0
        manager.status.return_value = {"managed_count": 0}
        response = self.client.get(
            "/api/roxy/profiles",
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 200)
        run_registration.assert_not_called()

    def test_import_requires_file_and_name(self):
        response = self.client.post(
            "/api/roxy/profiles/import",
            headers={"X-Auth-Code": "test-auth", "Origin": "http://localhost"},
            data={},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
