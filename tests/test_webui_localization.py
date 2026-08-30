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
            self.assertIn("MutationObserver", script)
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
