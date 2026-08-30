# -*- coding: utf-8 -*-
import unittest

from webui.app import create_app


class WebUiLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_login_page_loads_vietnamese_localization(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<html lang="vi">', html)
        self.assertIn("/static/vi.js", html)

    def test_page_loads_vietnamese_localization(self):
        response = self.client.get("/", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<html lang="vi">', html)
        self.assertIn("/static/vi.js", html)

    def test_frontend_does_not_expose_known_untranslated_labels(self):
        response = self.client.get("/", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for leaked in (
            "Reactive 2FA",
            "Local test (dry-run)",
            "Local test base",
            "Test domain 1",
            "Test domain 2 (optional)",
            "ACCOUNT OPERATIONS",
            "EMAIL LOGIN",
            "AUTHENTICATOR KEY",
            "RUN RESULTS",
            "Profile manager actions",
            "Managed Roxy profiles",
            "Roxy Profiles",
            "Locale / country",
            ">Legacy<",
            "BULK COMMANDS",
            "Legacy 会包含",
            "theo filter",
        ):
            self.assertNotIn(leaked, html)
        self.assertIn("Khôi phục 2FA", html)
        self.assertIn("Kiểm thử cục bộ", html)
        self.assertIn("THAO TÁC TÀI KHOẢN", html)
        self.assertIn("Profile Roxy", html)
        self.assertIn("Khu vực / quốc gia", html)

    def test_page_ignores_removed_legacy_ui_selector(self):
        response = self.client.get(
            "/?ui=legacy",
            headers=self.headers,
            environ_overrides={"HTTP_COOKIE": "ui_mode=legacy"},
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="app-sidebar"', html)
        self.assertNotIn("切换老 UI", html)
        self.assertNotIn("ui_mode=", response.headers.get("Set-Cookie", ""))


        response = self.client.get("/static/vi.js")
        try:
            self.assertEqual(response.status_code, 200)
            script = response.get_data(as_text=True)
            self.assertIn("Bảng điều khiển đăng ký GPT", script)
            self.assertIn("Kiểm thử cục bộ", script)
            self.assertIn("Trong thùng rác", script)
            self.assertIn("__translateVi", script)
            self.assertIn("MutationObserver", script)
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
