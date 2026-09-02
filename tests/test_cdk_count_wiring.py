import unittest

from webui.app import create_app


class CdkCountWiringTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @staticmethod
    def _template_has(html: str, marker: str) -> bool:
        return marker in html

    def test_modern_template_exposes_paymesh_inline_config_fields(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        # Màn đăng ký phải có inline config anchor để JS render các field api-base / accounts-per-CDK
        # cho provider Paymesh ngay tại toolbar, không cần sang Settings tab.
        self.assertIn('data-provider-config="paymesh"', html)
        self.assertIn("PAYMESH_API_BASE", html)
        self.assertIn("PAYMESH_ACCOUNTS_PER_CDK", html)

    def test_modern_template_auto_count_hint_when_cdks_pasted(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        # JS phải có helper tự set regCount = len(cdks) * accounts-per-CDK khi user paste/thay đổi CDK
        # để 5 card * 6 = 30 task, không còn kẹt count=1.
        self.assertIn("syncCdkBatchCount", html)

    def test_paymesh_auto_count_includes_original_and_each_routed_domain(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("isPaymesh ? 1 + routedDomains.length", html)

    def test_paymesh_auto_count_deduplicates_normalized_routed_domains(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("new Set(Array.from(panel.querySelectorAll(domainSelector))", html)
        self.assertIn("toLowerCase().replace(/\\.+$/, '')", html)

    def test_paymesh_accounts_per_card_changes_recalculate_auto_count(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            "'[data-gmail-routed-domain], [data-paymesh-routed-domain], #paymeshAccountsPerCdkInline'",
            html,
        )

    def test_paymesh_routed_domain_changes_recalculate_auto_count(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            "'[data-gmail-routed-domain], [data-paymesh-routed-domain], #paymeshAccountsPerCdkInline'",
            html,
        )


if __name__ == "__main__":
    unittest.main()