import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = PROJECT_ROOT / "webui" / "templates" / "index.html"
VI_TRANSLATION = PROJECT_ROOT / "webui" / "static" / "vi.js"
CONFIG_EDITOR = PROJECT_ROOT / "webui" / "config_editor.py"


class RegistrationSettingsUiTests(unittest.TestCase):
    def test_config_choices_use_pending_select_save_collector(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        renderer_start = source.index("function renderConfigPlainFieldV2")
        renderer_end = source.index("function renderMixedConfigSectionV2", renderer_start)
        renderer = source[renderer_start:renderer_end]
        reader_start = source.index("function readConfigElementValue")
        reader_end = source.index("function gmailApiUrlAvailableCount", reader_start)
        reader = source[reader_start:reader_end]
        collector_start = source.index("function trackConfigFieldChange")
        collector_end = source.index("$('#tab-config').addEventListener('input'", collector_start)
        collector = source[collector_start:collector_end]
        save_start = source.index("async function saveConfigUpdates")
        save_end = source.index("$('#tab-config').addEventListener('click'", save_start)
        saver = source[save_start:save_end]

        self.assertIn("Array.isArray(f.choices) && f.choices.length", renderer)
        self.assertIn("<select data-key=", renderer)
        self.assertIn("choice.value", renderer)
        self.assertIn("choice.label", renderer)
        self.assertIn("CONFIG_PENDING_UPDATES[f.key]", renderer)
        self.assertIn("CONFIG_PENDING_UPDATES[f.key] = readConfigElementValue(el, f)", collector)
        self.assertIn("updates[f.key] = readConfigElementValue(el, f)", saver)
        self.assertIn("JSON.stringify({updates})", saver)
        self.assertIn("f.key === 'CODEX_OAUTH_DRIVER'", source)

    def test_registration_settings_render_free_codex_toggle(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        start = source.index("function renderRegistrationSettingsSection")
        end = source.index("function renderFeatureSwitchField", start)
        renderer = source[start:end]

        self.assertIn("AUTO_PLAN_CHECK_AFTER_REGISTER", renderer)
        self.assertIn("AUTO_CODEX_FOR_FREE_AFTER_REGISTER", renderer)
        self.assertIn("AUTO_PAY153_FOR_FREE_TRIAL_AFTER_REGISTER", renderer)
        self.assertIn("const switches = [autoPlan, autoCodex, autoPay153].filter(Boolean)", renderer)
        self.assertIn("switches.map(f => renderFeatureSwitchField(f", renderer)

    def test_proxy_settings_render_rotating_proxy_status_tools(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("Trạng thái Proxy.vn Proxy xoay", source)
        self.assertIn("Đang đọc trạng thái key/lease local…", source)
        self.assertIn("/api/proxy/rotating/refresh", source)
        self.assertIn("bindRotatingProxyToolsV2", source)

    def test_proxy_settings_expose_one_account_per_ip_mode(self):
        source = CONFIG_EDITOR.read_text(encoding="utf-8")
        self.assertIn('"key": "ROTATING_PROXY_ONE_ACCOUNT_PER_IP"', source)

    def test_proxy_settings_are_split_into_static_and_rotating_tabs(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("let CONFIG_PROXY_ACTIVE_SECTION_V2 = '代理池';", source)
        self.assertIn("function proxyConfigSectionForKey(key)", source)
        self.assertIn("['代理池', 'Proxy.vn Proxy xoay']", source)
        self.assertIn("active === 'Proxy.vn Proxy xoay'", source)
        self.assertIn("data-proxy-section-v2", source)

    def test_rotating_proxy_settings_are_vietnamese(self):
        source = CONFIG_EDITOR.read_text(encoding="utf-8")

        self.assertIn('"label": "Bật proxy xoay Proxy.vn"', source)
        self.assertIn('"label": "API Key chính của Proxy.vn"', source)
        self.assertIn('"label": "Nhà mạng"', source)
        self.assertIn('"label": "Mã tỉnh/thành"', source)
        self.assertNotIn('"label": "启用 Proxy.vn 代理旋转"', source)

    def test_rotating_proxy_tab_has_vietnamese_label(self):
        source = VI_TRANSLATION.read_text(encoding="utf-8")

        self.assertIn("'Proxy.vn 代理旋转': 'Proxy xoay Proxy.vn'", source)

    def test_email_provider_settings_have_dedicated_tabs(self):
        template = INDEX_TEMPLATE.read_text(encoding="utf-8")
        translations = VI_TRANSLATION.read_text(encoding="utf-8")

        self.assertIn("if (key.startsWith('EMAIL_API_')) return ['Automated Email API'", template)
        self.assertIn("if (key.startsWith('OTPGMAIL_')) return ['OTPGmail'", template)
        self.assertIn("'Automated Email API', 'OTPGmail'", template)
        self.assertIn("'Automated Email API': 'Automated Email API'", translations)
        self.assertIn("'OTPGmail': 'OTPGmail'", translations)

    def test_bamboommo_is_available_in_registration_and_pool_selectors(self):
        template = INDEX_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn('<option value="bamboommo">BambooMMO Gmail</option>', template)
        self.assertIn('data-value="bamboommo" role="option">BambooMMO Gmail 邮箱池</button>', template)
        self.assertIn("bamboommo: 'BambooMMO Gmail 邮箱池'", template)

    def test_roxy_profile_manager_owner_prefix_has_vietnamese_label(self):
        source = CONFIG_EDITOR.read_text(encoding="utf-8")

        self.assertIn('"label": "Tiền tố nhận diện"', source)
        self.assertIn('"help": "Ghi dấu nhận diện của trình quản lý vào remark của Roxy"', source)


if __name__ == "__main__":
    unittest.main()
