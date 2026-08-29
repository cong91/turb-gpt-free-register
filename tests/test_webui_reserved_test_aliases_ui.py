# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "webui" / "templates" / "index.html"


class ReservedTestAliasUiTests(unittest.TestCase):
    def test_template_wires_runtime_tool(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('data-tab="tools"', html)
        self.assertIn("{% include '_reserved_test_aliases_tool.html' %}", html)
        self.assertIn("'tools'", html)
        self.assertIn("reserved_test_aliases.css", html)
        self.assertIn("reserved_test_aliases.js", html)

    def test_tool_partial_exposes_manual_domains_count_and_copy_controls(self):
        html = (ROOT / "webui" / "templates" / "_reserved_test_aliases_tool.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="tab-tools"', html)
        self.assertIn('id="reservedTestAliasBase"', html)
        self.assertIn('id="reservedTestAliasDomain1"', html)
        self.assertIn('id="reservedTestAliasDomain2"', html)
        self.assertIn('id="reservedTestAliasLimit"', html)
        self.assertIn('min="1" max="1000" value="6"', html)
        self.assertIn('id="btnGenerateReservedTestAliases"', html)
        self.assertIn('id="btnCopyReservedTestAliases"', html)
        self.assertNotIn("/api/jobs", html)

    def test_registration_form_exposes_local_test_mode_and_two_domains(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('value="local_test"', html)
        self.assertIn('data-provider-input="local_test"', html)
        self.assertIn('data-local-test-base', html)
        self.assertEqual(html.count('<input data-local-test-domain'), 2)
        self.assertIn("local_test_base", html)
        self.assertIn("local_test_domains", html)
        self.assertIn("Local test", html)

    def test_runtime_script_calls_only_preview_endpoint(self):
        script = (ROOT / "webui" / "static" / "reserved_test_aliases.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("/api/tools/reserved-test-aliases/preview", script)
        self.assertIn("copyText(aliases.join('\\n'))", script)
        self.assertNotIn("/api/jobs", script)
        self.assertNotIn("submit_registration", script)
        self.assertNotIn("acquire_email", script)


if __name__ == "__main__":
    unittest.main()
