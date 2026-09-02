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
        self.assertIn('id="btnRecoverLatestFreePlusV2"', source)
        self.assertIn('>Lấy bản xuất gần nhất</button>', source)
        self.assertIn('title="Tạo lại bản xuất Free Plus gần nhất"', source)
        self.assertIn("/api/accounts/free-plus/recover-latest", source)

    def test_import_modal_exposes_codex_credential_accounts(self):
        root = Path(__file__).resolve().parent.parent / "webui" / "templates"
        source = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-value="credentials"', source)
        self.assertIn("USER | PASS | 2FA", source)
        self.assertIn("credentials: 'Codex 登录凭据'", source)
        self.assertIn("if (source !== 'credentials') setPoolSourceV2(source);", source)
        self.assertIn("更新 ${r.updated || 0}", source)


if __name__ == "__main__":
    unittest.main()
