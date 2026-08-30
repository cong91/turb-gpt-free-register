import json
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

from core import roxy_codex_oauth, roxy_phone_country


def _execute_country_script(script, *args):
    runner = r"""
const payload = JSON.parse(process.argv[1]);
globalThis.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
};
const result = new Function(payload.script).apply(null, payload.args);
process.stdout.write(JSON.stringify({ result }));
"""
    payload = json.dumps({"script": script, "args": list(args)})
    completed = subprocess.run(
        ["node", "-e", runner, payload],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout).get("result")


def _execute_virtualized_phone_country_script(script, phone):
    runner = r"""
const payload = JSON.parse(process.argv[1]);
const element = (values = {}) => ({
  getBoundingClientRect: () => ({ width: 100, height: 30, top: 0 }),
  getAttribute: () => '',
  querySelectorAll: () => [],
  scrollIntoView: () => {},
  click: () => {},
  innerText: '',
  textContent: '',
  parentElement: null,
  scrollTop: 0,
  scrollHeight: 0,
  clientHeight: 0,
  dispatchEvent: () => {},
  ...values,
});
const scrollContainer = element({ scrollHeight: 1200, clientHeight: 200 });
const wrapper = element({ parentElement: scrollContainer });
const listbox = element({ parentElement: wrapper });
const trigger = element({
  innerText: 'United States +1',
  textContent: 'United States +1',
});
const tel = element();
globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
globalThis.Event = class Event { constructor(type) { this.type = type; } };
globalThis.document = {
  querySelector: selector => selector.includes('tel') ? tel : null,
  querySelectorAll: selector => {
    if (selector === 'select') return [];
    if (selector.includes('combobox')) return [trigger];
    if (selector === '[role="listbox"]') return [listbox];
    return [];
  },
};
const result = new Function(payload.script).apply(null, [payload.phone]);
process.stdout.write(JSON.stringify({ result, scrollTop: scrollContainer.scrollTop }));
"""
    payload = json.dumps({"script": script, "phone": phone})
    completed = subprocess.run(
        ["node", "-e", runner, payload],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


class _ScriptDriver:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute_script(self, script, *args):
        self.calls.append(script)
        return self.results.pop(0)


class RoxyPhoneCountryTests(unittest.TestCase):
    def test_phone_country_error_does_not_duplicate_plus_prefix(self):
        driver = _ScriptDriver([{"ok": False, "phase": "waiting_for_phone_country_option"}] * 2)

        with self.assertRaisesRegex(RuntimeError, r"\+639050668667") as raised:
            roxy_codex_oauth.select_phone_country(driver, "+639050668667", timeout=0.3)

        self.assertNotIn("++", str(raised.exception))

    def test_country_scripts_return_their_result_to_selenium(self):
        vietnam_result = _execute_country_script(roxy_phone_country._SELECT_COUNTRY_SCRIPT)
        phone_result = _execute_country_script(
            roxy_phone_country._SELECT_PHONE_COUNTRY_SCRIPT,
            "+639050668667",
        )

        self.assertEqual(vietnam_result, {"ok": False, "phase": "trigger_missing"})
        self.assertEqual(phone_result, {"ok": False, "phase": "trigger_missing"})

    def test_phone_country_script_scrolls_the_virtualized_list_container(self):
        result = _execute_virtualized_phone_country_script(
            roxy_phone_country._SELECT_PHONE_COUNTRY_SCRIPT,
            "+639050668667",
        )

        self.assertEqual(
            result["result"],
            {"ok": False, "phase": "waiting_for_phone_country_option"},
        )
        self.assertGreater(result["scrollTop"], 0)

    def test_select_vietnam_country_waits_for_react_aria_selection(self):
        driver = _ScriptDriver([
            {"ok": False, "phase": "opened"},
            {"ok": True, "country": "Vietnam +84"},
        ])

        result = roxy_codex_oauth.select_vietnam_country(driver, timeout=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["country"], "Vietnam +84")
        self.assertEqual(len(driver.calls), 2)

    def test_select_phone_country_passes_e164_to_script(self):
        driver = _ScriptDriver([{"ok": True, "country": "Philippines +63", "dialCode": "63"}])

        result = roxy_codex_oauth.select_phone_country(driver, "+639050668667", timeout=1)

        self.assertEqual(result["dialCode"], "63")
        self.assertIn("arguments[0]", driver.calls[0])


class RoxyCodexPhoneViOtpTests(unittest.TestCase):
    def test_phone_flow_selects_vietnam_before_renting_and_polls_viotp(self):
        driver = Mock()
        http = Mock()
        events = []
        config = types.SimpleNamespace(
            SMS_PROVIDER="viotp",
            SMS_MAX_RETRIES=1,
            SMS_CODE_WAIT=30,
            SMS_POLL_INTERVAL=1,
        )
        phone_fill = {
            "e164": "+84987654321",
            "actualVisible": "987654321",
            "hiddenValue": "+84987654321",
            "dialCode": "84",
            "selectedText": "Vietnam +84",
        }

        def select_country(_driver):
            events.append("select_vietnam")
            return {"ok": True, "country": "Vietnam +84"}

        def acquire(_http, *, country=None, **_kwargs):
            events.append(("acquire", country))
            return "request-1", "84987654321"

        with patch.object(roxy_codex_oauth.sms_provider, "_cfg", config), \
             patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http), \
             patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True), \
             patch.object(roxy_codex_oauth, "_ensure_add_phone_input", side_effect=lambda *_args, **_kwargs: events.append("ensure_phone")), \
             patch.object(roxy_codex_oauth, "select_vietnam_country", side_effect=select_country, create=True), \
             patch.object(roxy_codex_oauth.sms_provider, "acquire_number", side_effect=acquire) as acquire_number, \
             patch.object(roxy_codex_oauth, "_set_phone_value", return_value=phone_fill), \
             patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"), \
             patch.object(roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"ok": True}), \
             patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"), \
             patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True}), \
             patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"), \
             patch.object(roxy_codex_oauth, "_wait_after_phone_send"), \
             patch.object(roxy_codex_oauth.sms_provider, "set_status"), \
             patch.object(roxy_codex_oauth.sms_provider, "wait_for_sms_code", return_value="123456"), \
             patch.object(roxy_codex_oauth, "_type_otp"), \
             patch.object(roxy_codex_oauth, "_click_if_present", return_value=True), \
             patch.object(roxy_codex_oauth, "_wait_after_phone_otp_submit", return_value="left_phone_flow"), \
             patch.object(roxy_codex_oauth.sms_provider, "complete"), \
             patch.object(roxy_codex_oauth.time, "sleep"):
            roxy_codex_oauth._do_phone_verification_if_present(driver)

        self.assertEqual(events, ["ensure_phone", "select_vietnam", ("acquire", "vn")])
        acquire_number.assert_called_once_with(
            http,
            country="vn",
            lane_key=roxy_codex_oauth.sms_provider.default_lane_key(),
        )


class RoxyCodexPhoneRetryTests(unittest.TestCase):
    def test_set_phone_value_reads_custom_country_combobox_dial_code(self):
        driver = _ScriptDriver([
            True,
            {
                "ok": True,
                "e164": "+5512920041678",
                "visibleValue": "12920041678",
                "actualVisible": "12920041678",
                "hiddenValue": "",
                "dialCode": "55",
            },
        ])

        result = roxy_codex_oauth._set_phone_value(driver, "+5512920041678")

        self.assertEqual(result["dialCode"], "55")
        self.assertIn('button[aria-haspopup="listbox"]', driver.calls[1])
        self.assertIn('[role="combobox"]', driver.calls[1])

    def test_phone_body_whatsapp_copy_does_not_override_selected_sms(self):
        state = {
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ],
            "bodyText": "Choose SMS or WhatsApp to continue",
        }

        self.assertEqual(roxy_codex_oauth._classify_phone_page_failure(state), "")

    def test_phone_body_whatsapp_is_failure_when_sms_is_unavailable(self):
        state = {
            "radios": [{"value": "whatsapp", "checked": True}],
            "bodyText": "WhatsApp verification is required",
        }

        self.assertEqual(
            roxy_codex_oauth._classify_phone_page_failure(state),
            "whatsapp_channel",
        )

    def test_phone_body_copy_without_radio_metadata_is_not_channel_failure(self):
        state = {"radios": [], "bodyText": "Phone number and WhatsApp"}

        self.assertEqual(roxy_codex_oauth._classify_phone_page_failure(state), "")

    def test_phone_retry_recovery_prefers_history_back_before_direct_add_phone(self):
        driver = Mock(current_url="https://auth.openai.com/phone-verification")
        phone_input = object()

        with patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=False), \
             patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=True), \
             patch.object(roxy_codex_oauth, "_find_any", return_value=phone_input), \
             patch.object(roxy_codex_oauth, "human_delay"):
            result = roxy_codex_oauth._ensure_add_phone_input(driver, reason="sms-timeout")

        self.assertIs(result, phone_input)
        driver.back.assert_called_once_with()
        driver.get.assert_not_called()

    def test_refresh_phone_retry_uses_history_before_refresh(self):
        driver = Mock(current_url="https://auth.openai.com/phone-verification")
        phone_input = object()

        with patch.object(roxy_codex_oauth, "_is_phone_code_page", return_value=True), \
             patch.object(roxy_codex_oauth, "_find_any", return_value=phone_input), \
             patch.object(roxy_codex_oauth, "human_delay"):
            roxy_codex_oauth._refresh_add_phone_for_retry(driver, reason="sms-timeout")

        driver.back.assert_called_once_with()
        driver.refresh.assert_not_called()
        driver.get.assert_not_called()

    def test_phone_auth_state_failure_is_marked_for_full_oauth_retry(self):
        driver = Mock()
        http = Mock()
        config = types.SimpleNamespace(
            SMS_PROVIDER="hero",
            SMS_MAX_RETRIES=3,
            SMS_CODE_WAIT=30,
            SMS_POLL_INTERVAL=1,
        )

        with patch.object(roxy_codex_oauth.sms_provider, "_cfg", config), \
             patch.object(roxy_codex_oauth.sms_provider, "_http", return_value=http), \
             patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True), \
             patch.object(roxy_codex_oauth, "_ensure_add_phone_input"), \
             patch.object(roxy_codex_oauth.sms_provider, "acquire_number", return_value=("hero-1", "56850211860")), \
             patch.object(roxy_codex_oauth, "select_phone_country", return_value={"country": "Chile +56"}), \
             patch.object(roxy_codex_oauth, "_set_phone_value", return_value={"e164": "+56850211860"}), \
             patch.object(roxy_codex_oauth, "_blur_active_input_and_wait"), \
             patch.object(roxy_codex_oauth, "_verify_add_phone_value_before_submit", return_value={"ok": True}), \
             patch.object(roxy_codex_oauth, "_select_sms_channel_or_raise"), \
             patch.object(roxy_codex_oauth, "_click_add_phone_continue_button", return_value={"ok": True}), \
             patch.object(roxy_codex_oauth, "_wait_page_settle_after_submit"), \
             patch.object(roxy_codex_oauth, "_wait_after_phone_send", side_effect=RuntimeError("invalid_auth_step: retry")), \
             patch.object(roxy_codex_oauth.sms_provider, "cancel"), \
             patch.object(roxy_codex_oauth.time, "sleep"), \
             self.assertRaisesRegex(RuntimeError, "phone_auth_state_reset_required"):
            roxy_codex_oauth._do_phone_verification_if_present(driver)

    def test_outer_oauth_retries_after_phone_auth_state_reset(self):
        failed = {
            "ok": False,
            "status": "failed",
            "message": "PhoneAuthStateError: phone_auth_state_reset_required: invalid_auth_step",
        }
        succeeded = {"ok": True, "status": "success", "message": "ok"}

        with patch.object(roxy_codex_oauth, "_run_roxy_codex_oauth_once", side_effect=[failed, succeeded]) as run_once:
            result = roxy_codex_oauth.run_roxy_codex_oauth("user@example.com", force=True)

        self.assertTrue(result["ok"])
        self.assertEqual(run_once.call_count, 2)


if __name__ == "__main__":
    unittest.main()
