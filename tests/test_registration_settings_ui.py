import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = PROJECT_ROOT / "webui" / "templates" / "index.html"
VI_TRANSLATION = PROJECT_ROOT / "webui" / "static" / "vi.js"
CONFIG_EDITOR = PROJECT_ROOT / "webui" / "config_editor.py"


class RegistrationSettingsUiTests(unittest.TestCase):
    def test_registration_settings_render_free_codex_toggle(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        start = source.index("function renderRegistrationSettingsSection")
        end = source.index("function renderFeatureSwitchField", start)
        renderer = source[start:end]

        self.assertIn("AUTO_PLAN_CHECK_AFTER_REGISTER", renderer)
        self.assertIn("AUTO_CODEX_FOR_FREE_AFTER_REGISTER", renderer)
        self.assertIn("const switches = [autoPlan, autoCodex].filter(Boolean)", renderer)
        self.assertIn("switches.map(f => renderFeatureSwitchField(f", renderer)

    def test_proxy_settings_render_rotating_proxy_status_tools(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("Proxy.vn 代理旋转状态", source)
        self.assertIn("/api/proxy/rotating/refresh", source)
        self.assertIn("bindRotatingProxyToolsV2", source)

    def test_proxy_settings_are_split_into_static_and_rotating_tabs(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("let CONFIG_PROXY_ACTIVE_SECTION_V2 = '代理池';", source)
        self.assertIn("function proxyConfigSectionForKey(key)", source)
        self.assertIn("['代理池', 'Proxy.vn 代理旋转']", source)
        self.assertIn("data-proxy-section-v2", source)

    def test_rotating_proxy_tab_has_vietnamese_label(self):
        source = VI_TRANSLATION.read_text(encoding="utf-8")

        self.assertIn("'Proxy.vn 代理旋转': 'Proxy xoay Proxy.vn'", source)

    def test_roxy_profile_manager_owner_prefix_has_vietnamese_label(self):
        source = CONFIG_EDITOR.read_text(encoding="utf-8")

        self.assertIn('"label": "Tiền tố nhận diện"', source)
        self.assertIn('"help": "Ghi dấu nhận diện của trình quản lý vào remark của Roxy"', source)


if __name__ == "__main__":
    unittest.main()
