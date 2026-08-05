# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class AccountExportFormatTemplateTests(unittest.TestCase):
    def test_account_template_exposes_format_select_and_payload(self):
        root = Path(__file__).resolve().parent.parent / "webui" / "templates"
        source = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="accountExportFormatV2"', source)
        self.assertIn('value="modern"', source)
        self.assertIn('value="legacy"', source)
        self.assertIn("format: formatName", source)
        self.assertIn("field === 'copy_line' ? getAccountExportFormat() : 'modern'", source)


if __name__ == "__main__":
    unittest.main()
