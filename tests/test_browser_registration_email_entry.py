import unittest
from unittest.mock import patch

from core import browser_registration


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

        found = browser_registration._wait_for_email_input(driver, timeout=1)

        self.assertIs(found, element)

    def test_set_element_value_reports_a_lost_dom_handle_without_reading_tag_name(self):
        class Driver:
            def execute_script(self, script, *_args):
                self.script = script
                return False

        driver = Driver()
        result = browser_registration._set_element_value(driver, None, "user@example.com")

        self.assertFalse(result)
        self.assertIn("if (!el) return false", driver.script)

    def test_email_entry_requeries_after_a_replaced_input_handle(self):
        first = object()
        replacement = object()
        driver = object()
        email = "user@example.com"

        with (
            patch.object(
                browser_registration,
                "_wait_for_email_input",
                side_effect=[first, replacement],
            ),
            patch.object(
                browser_registration,
                "_human_type_text",
                side_effect=[RuntimeError("JavascriptException: element became null"), None],
            ) as type_text,
            patch.object(browser_registration.time, "sleep"),
        ):
            browser_registration._type_email_address(driver, email, timeout=1)

        self.assertEqual(type_text.call_count, 2)

if __name__ == "__main__":
    unittest.main()
