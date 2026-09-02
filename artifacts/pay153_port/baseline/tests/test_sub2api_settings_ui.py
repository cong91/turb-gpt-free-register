import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = PROJECT_ROOT / "webui" / "templates" / "index.html"


class Sub2APISettingsUiTests(unittest.TestCase):
    def test_callback_secret_is_placed_in_the_common_sub2api_codex_section(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        start = source.index("function codexConfigSectionForKey")
        end = source.index("function renderRoxyWorkspaceToolsV2", start)
        renderer = source[start:end]

        self.assertIn("SUB2API_AUTOMATION_CALLBACK_SECRET", renderer)


if __name__ == "__main__":
    unittest.main()
