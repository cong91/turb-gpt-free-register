# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import roxybrowser as roxy_cfg
from core.roxy_profile_manager_client import RoxyProfileManagerClient


class RoxyProfileManagerClientTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = RoxyProfileManagerClient(self.http)
        self.client.client.request = self.http.request
        self.workspace = patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "workspace")
        self.project = patch.object(roxy_cfg, "ROXY_PROJECT_ID", "project")
        self.workspace.start()
        self.project.start()
        self.addCleanup(self.workspace.stop)
        self.addCleanup(self.project.stop)

    def test_list_profiles_pages_until_short_page(self):
        first = [{"dirId": f"dir-{index}", "name": str(index)} for index in range(100)]
        second = [{"dirId": "dir-100", "name": "100"}]
        self.http.request.side_effect = [
            {"data": {"rows": first, "total": 101}},
            {"data": {"rows": second, "total": 101}},
        ]
        profiles = self.client.list_profiles()
        self.assertEqual(len(profiles), 101)
        self.assertEqual(self.http.request.call_count, 2)
        self.assertEqual(self.http.request.call_args_list[1].kwargs["params"]["page"], 2)

    def test_create_writes_and_verifies_window_remark_marker(self):
        self.http.request.side_effect = [
            {"data": {"dirId": "dir-1"}},
            {
                "data": [{
                    "dirId": "dir-1",
                    "windowRemark": "[managed:owner]",
                }],
            },
        ]
        created = self.client.create_profile({"name": "Managed"}, owner_marker="owner")
        self.assertEqual(created["dirId"], "dir-1")
        create_call = self.http.request.call_args_list[0]
        self.assertEqual(
            create_call.kwargs["json_body"]["windowRemark"],
            "[managed:owner]",
        )

    def test_soft_delete_requires_explicit_soft_delete_payload(self):
        self.http.request.side_effect = [
            {"data": [{"dirId": "dir-1", "windowRemark": "[managed:owner]"}]},
            {"data": {}},
            {"data": {"rows": []}},
        ]
        self.client.soft_delete_profile("dir-1", owner_marker="owner")
        delete_call = self.http.request.call_args_list[1]
        self.assertEqual(delete_call.args[:2], ("POST", "/browser/delete"))
        self.assertEqual(delete_call.kwargs["json_body"]["isSoftDelete"], True)
        self.assertEqual(delete_call.kwargs["json_body"]["dirIds"], ["dir-1"])


if __name__ == "__main__":
    unittest.main()
