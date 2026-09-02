import unittest
from pathlib import Path


class RoxyProfilesUiContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.template = (root / "webui" / "templates" / "_roxy_profiles_tool.html").read_text(encoding="utf-8")
        self.script = (root / "webui" / "static" / "roxy_profiles.js").read_text(encoding="utf-8")

    def test_template_exposes_explicit_modes_and_import(self):
        for marker in ("roxyProfileSearch", "roxyProfileStateFilter", "roxyProfileImportModal", "btnImportRoxyProfile", "roxyProfileEditModal", "roxyProfilesSelectAll"):
            self.assertIn(marker, self.template)

    def test_script_keeps_remote_and_local_actions_separate(self):
        for marker in ("remote-open", "remote-close", "local-open", "local-close", "export-full", "Chỉ trạng thái trình duyệt", "runBulk", "editProfile", "metadata_export", "remote_open"):
            self.assertIn(marker, self.script)

    def test_script_renders_pagination_and_state_filter(self):
        for marker in (
            "page_size", "state.profileState", "btnRoxyProfilesPrev",
            "btnRoxyProfilesNext", "has_next", "source_core_version",
            "fingerprint_status", "CDP đang hoạt động", "eligibility",
            "offline_recovery_staging",
        ):
            self.assertIn(marker, self.script)
        self.assertIn("Thời gian chạy", self.template)


if __name__ == "__main__":
    unittest.main()
