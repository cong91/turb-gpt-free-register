import unittest
from unittest.mock import Mock, call, patch

from core import browser_email_entry, browser_page_actions


class _VisibleEmailElement:
    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class _GenericSeleniumDriver:
    def __init__(self, element):
        self.element = element

    def find_elements(self, _by, selector):
        return [self.element] if selector == "input[type='email']" else []


class BrowserRegistrationEmailEntryTests(unittest.TestCase):
    def test_email_lookup_uses_native_locator_for_every_browser_provider(self):
        element = _VisibleEmailElement()
        driver = _GenericSeleniumDriver(element)

        found = browser_email_entry._wait_for_email_input(driver, timeout=1)

        self.assertIs(found, element)

    def test_set_element_value_reports_a_lost_dom_handle_without_reading_tag_name(self):
        class Driver:
            def execute_script(self, script, *_args):
                self.script = script
                return False

        driver = Driver()
        result = browser_page_actions._set_element_value(driver, None, "user@example.com")

        self.assertFalse(result)
        self.assertIn("if (!el) return false", driver.script)

    def test_email_entry_requeries_after_a_replaced_input_handle(self):
        first = object()
        replacement = object()
        driver = object()
        email = "user@example.com"

        with (
            patch.object(
                browser_email_entry,
                "_wait_for_email_input",
                side_effect=[first, replacement],
            ),
            patch.object(
                browser_email_entry,
                "_human_type_text",
                side_effect=[RuntimeError("JavascriptException: element became null"), None],
            ) as type_text,
            patch.object(browser_email_entry.time, "sleep"),
        ):
            browser_email_entry._type_email_address(driver, email, timeout=1)

        self.assertEqual(type_text.call_count, 2)

    def test_before_action_blocks_returning_a_newly_found_input(self):
        callback = Mock(side_effect=[None, RuntimeError("chooser appeared")])
        element = object()
        with (
            patch.object(browser_email_entry, "_find_any", return_value=element),
            self.assertRaisesRegex(RuntimeError, "chooser appeared"),
        ):
            browser_email_entry._wait_for_email_input(object(), timeout=1, before_action=callback)

        self.assertEqual(callback.call_count, 2)

    def test_before_action_blocks_entry_click_after_lookup_failure(self):
        callback = Mock(side_effect=[None, None, RuntimeError("chooser appeared")])
        with (
            patch.object(browser_email_entry, "_find_any", side_effect=RuntimeError("not mounted")),
            patch.object(browser_email_entry, "_email_entry_state", return_value={}),
            patch.object(browser_email_entry, "_browser_challenge_state", return_value={}),
            patch.object(browser_email_entry, "_click_email_entry_option") as click,
            self.assertRaisesRegex(RuntimeError, "chooser appeared"),
        ):
            browser_email_entry._wait_for_email_input(object(), timeout=1, before_action=callback)

        click.assert_not_called()

    def test_before_action_interrupts_requery_before_replacement_is_typed(self):
        first = object()
        callback = Mock(side_effect=[None, RuntimeError("chooser appeared")])
        with (
            patch.object(browser_email_entry, "_wait_for_email_input", return_value=first) as find,
            patch.object(browser_email_entry, "_human_type_text", side_effect=RuntimeError("detached")) as type_text,
            self.assertRaisesRegex(RuntimeError, "chooser appeared"),
        ):
            browser_email_entry._type_email_address(object(), "user@example.com", timeout=8, before_action=callback)

        self.assertEqual(find.call_args_list, [
            call(unittest.mock.ANY, timeout=8, before_action=callback),
            call(unittest.mock.ANY, timeout=5, before_action=callback),
        ])
        type_text.assert_called_once()

    def test_before_action_propagates_during_input_wait(self):
        callback = Mock(side_effect=RuntimeError("chooser appeared"))
        with (
            patch.object(browser_email_entry, "_find_any") as find,
            self.assertRaisesRegex(RuntimeError, "chooser appeared"),
        ):
            browser_email_entry._wait_for_email_input(object(), timeout=1, before_action=callback)

        find.assert_not_called()

if __name__ == "__main__":
    unittest.main()
