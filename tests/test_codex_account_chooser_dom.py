"""Synthetic, offline DOM regressions for the Codex account chooser.

The markup uses observed structural markers, not a copy of the real page.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

TARGET = "target@example.invalid"
FIXTURE_URL = "https://auth.openai.com/__codex_chooser_fixture__"
JAPANESE_HEADING = "ログインするアカウントを選択"
ENGLISH_HEADING = "Choose an account"

# The page records synthetic clicks and prevents every link navigation.
CHOOSER_EVENTS = """
window.clicks = [];
document.addEventListener('click', event => {
  const control = event.target.closest('button, a');
  if (!control) return;
  event.preventDefault();
  window.clicks.push(control.id);
  if (control.name === 'session_id') {
    document.querySelector('main').innerHTML =
      '<h1>Enter password</h1><input type="password" name="password">';
  }
});
"""

EMAIL_EVENTS = """
window.clicks = [];
window.submittedEmail = null;
document.addEventListener('click', event => {
  const control = event.target.closest('button, a');
  if (!control) return;
  window.clicks.push(control.id);
  if (control.id !== 'submit-email') event.preventDefault();
});
document.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  window.submittedEmail = document.querySelector('input[type=email]').value;
  document.querySelector('main').innerHTML =
    '<h1>Enter password</h1><input type="password" name="password">';
});
"""


def setUpModule():
    """Isolate configuration and SQLite before importing project modules."""
    global _resources, credential_login, BrowserSeleniumDriver

    _resources = ExitStack()
    unittest.addModuleCleanup(_resources.close)
    root = Path(_resources.enter_context(tempfile.TemporaryDirectory()))
    env_file = root / "empty.env"
    env_file.touch()
    database = root / "turb.sqlite3"
    sqlite3.connect(database).close()

    required_env = (
        "PATH", "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "USERPROFILE", "HOME",
        "PLAYWRIGHT_BROWSERS_PATH", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    )
    isolated_env = {key: os.environ[key] for key in required_env if key in os.environ}
    isolated_env.update({
        "TURB_ENV_FILE": str(env_file),
        "ENABLE_HUMANIZE_BROWSER_ACTIONS": "False",
        "ENABLE_HUMANIZE_DELAY": "False",
    })
    _resources.enter_context(patch.dict(os.environ, isolated_env, clear=True))
    _resources.enter_context(patch.object(sys, "dont_write_bytecode", True))

    real_connect = sqlite3.connect

    def isolated_connect(path, *args, **kwargs):
        candidate = Path(os.fsdecode(path)).resolve()
        if not candidate.is_relative_to(root):
            raise AssertionError(f"SQLite access outside temporary directory: {candidate}")
        return real_connect(path, *args, **kwargs)

    _resources.enter_context(patch.object(sqlite3, "connect", side_effect=isolated_connect))

    from core import app_state_db

    _resources.enter_context(patch.object(app_state_db, "APP_STATE_DB_PATH", database))
    from core import browser_credential_login as credential_login
    from core.cloakbrowser_driver import BrowserSeleniumDriver


def chooser_markup(entries, *, heading=JAPANESE_HEADING):
    buttons = []
    for control_id, address, attributes in entries:
        buttons.append(
            f'<button type="button" name="session_id" id="{html.escape(control_id)}" '
            f'{attributes}><span>{html.escape(address)}</span></button>'
        )
    return (
        f'<h1>{html.escape(heading)}</h1>'
        + "".join(buttons)
        + '<a id="other-account" href="/log-in-or-create-account">別のアカウントを使用</a>'
        + '<button id="consent" type="button">権限を許可</button>'
        + '<button id="external-idp" type="button" data-provider="google">Google</button>'
    )


class CodexAccountChooserDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(
            executable_path=cls.playwright.chromium.executable_path,
            headless=True,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-domain-reliability",
                "--disable-quic",
                "--disable-preconnect",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--no-proxy-server",
                "--no-first-run",
            ],
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(offline=True, service_workers="block")
        self.addCleanup(self.context.close)
        self.requests = []
        self.context.on("request", lambda request: self.requests.append(request.url))
        self.context.route(
            "**/*",
            lambda route: route.fulfill(status=200, content_type="text/html", body="<!doctype html><main></main>")
            if route.request.url == FIXTURE_URL else route.abort(),
        )
        if hasattr(self.context, "route_web_socket"):
            self.context.route_web_socket("**/*", lambda socket: socket.close())
        self.context.add_init_script(
            "window.WebSocket = class { constructor() { throw new Error('WebSockets blocked'); } };"
        )
        self.page = self.context.new_page()
        self.page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        self.driver = BrowserSeleniumDriver(self.browser, self.context, self.page)
        self.addCleanup(self.assert_no_requests)

    def assert_no_requests(self):
        self.assertTrue(self.requests, "The synthetic fixture was not loaded")
        self.assertEqual(set(self.requests), {FIXTURE_URL}, "Unexpected network request")

    def set_page(self, markup, events=CHOOSER_EVENTS):
        self.page.set_content(f"<!doctype html><meta charset='utf-8'><main>{markup}</main><script>{events}</script>")
        self.assertEqual(self.driver.current_url, FIXTURE_URL)

    def install_page_on_navigation(self, markup, events):
        self.page.add_init_script(
            "window.addEventListener('DOMContentLoaded', () => {"
            f"document.body.innerHTML = '<main></main>'; document.querySelector('main').innerHTML = {json.dumps(markup)};"
            f"{events}"
            "}, {once: true});"
        )

    def click_ids(self):
        return self.page.evaluate("window.clicks")

    def assert_password_after_target_selection(self):
        self.assertTrue(credential_login._select_matching_account(self.driver, TARGET, timeout=1))
        self.assertEqual(self.click_ids(), ["target"])
        self.assertEqual(
            credential_login._wait_for_login_state(
                self.driver, timeout=2, ignored_states={"account_chooser"}
            ),
            "password",
        )
        self.assertEqual(self.driver.current_url, FIXTURE_URL)

    def test_japanese_structural_chooser_is_classified_and_selected(self):
        markup = chooser_markup([("target", TARGET, "")])
        self.set_page(markup)
        self.assertEqual(credential_login.classify_login_state(self.driver), "account_chooser")
        self.assertEqual(credential_login._wait_for_login_state(self.driver, timeout=1), "account_chooser")
        self.install_page_on_navigation(markup, CHOOSER_EVENTS)
        with patch.object(credential_login, "human_delay"):
            state = credential_login._open_and_submit_email(self.driver, TARGET, FIXTURE_URL)
        self.assertEqual(state, "account_chooser")
        self.assertEqual(self.click_ids(), [])
        self.assert_password_after_target_selection()

    def test_delayed_japanese_chooser_is_detected_after_initial_unknown(self):
        self.set_page("<p>Loading</p>")
        self.assertEqual(credential_login.classify_login_state(self.driver), "unknown")
        markup = chooser_markup([("target", TARGET, "")])
        self.page.evaluate(
            "markup => setTimeout(() => { document.querySelector('main').innerHTML = markup; }, 100)",
            markup,
        )
        state = credential_login._wait_for_login_state(self.driver, timeout=2)
        self.assertTrue(self.page.locator('button[name="session_id"]').is_visible())
        self.assertEqual(state, "account_chooser")
        self.assert_password_after_target_selection()

    def test_delayed_chooser_is_recognized_by_login_entry(self):
        markup = chooser_markup([("target", TARGET, "")])
        delayed_events = (
            "window.clicks = [];"
            f"setTimeout(() => {{ document.querySelector('main').innerHTML = {json.dumps(markup)}; }}, 100);"
            + CHOOSER_EVENTS
        )
        self.install_page_on_navigation("<p>Loading</p>", delayed_events)
        with patch.object(credential_login, "human_delay"):
            state = credential_login._open_and_submit_email(self.driver, TARGET, FIXTURE_URL)

        self.assertEqual(state, "account_chooser")
        self.assertEqual(self.click_ids(), [])
        self.assert_password_after_target_selection()

    def test_immediate_english_chooser_skips_email_entry_and_selects_account(self):
        markup = chooser_markup([("target", TARGET, "")], heading=ENGLISH_HEADING)
        self.install_page_on_navigation(markup, CHOOSER_EVENTS)
        with patch.object(credential_login, "human_delay"):
            state = credential_login._open_and_submit_email(self.driver, TARGET, FIXTURE_URL)
        self.assertEqual(state, "account_chooser")
        self.assertEqual(self.click_ids(), [])
        self.assert_password_after_target_selection()

    def test_substring_only_account_is_not_selected(self):
        self.set_page(chooser_markup([("lookalike", "nottarget@example.invalid", "")], heading=ENGLISH_HEADING))
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_exact_account_wins_over_preceding_substring_lookalike(self):
        self.set_page(chooser_markup([
            ("lookalike", "nottarget@example.invalid", ""),
            ("target", TARGET, ""),
        ], heading=ENGLISH_HEADING))
        self.assert_password_after_target_selection()

    def test_duplicate_matching_accounts_fail_closed(self):
        self.set_page(chooser_markup([
            ("first", TARGET, ""),
            ("second", TARGET.upper(), ""),
        ], heading=ENGLISH_HEADING))
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_wrong_account_only_fails_closed(self):
        self.set_page(chooser_markup([("other", "other@example.invalid", "")], heading=ENGLISH_HEADING))
        self.assertEqual(credential_login.classify_login_state(self.driver), "account_chooser")
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))
        self.assertEqual(self.driver.current_url, FIXTURE_URL)

    def test_hidden_and_disabled_matches_are_skipped_for_visible_match(self):
        self.set_page(chooser_markup([
            ("hidden", TARGET, "hidden"),
            ("disabled", TARGET, "disabled"),
            ("aria-disabled", TARGET, 'aria-disabled="true"'),
            ("target", TARGET.upper(), ""),
        ], heading=ENGLISH_HEADING))
        self.assert_password_after_target_selection()

    def test_hidden_and_disabled_matches_alone_fail_closed(self):
        self.set_page(chooser_markup([
            ("hidden", TARGET, "hidden"),
            ("disabled", TARGET, "disabled"),
            ("aria-disabled", TARGET, 'aria-disabled="true"'),
        ], heading=ENGLISH_HEADING))
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_reordered_chooser_never_clicks_a_different_account(self):
        from core.cloakbrowser_driver import BrowserElement

        self.set_page(chooser_markup([
            ("target", TARGET, ""),
            ("other", "other@example.invalid", ""),
        ], heading=ENGLISH_HEADING))
        original_click = BrowserElement.click

        def reorder_before_click(element):
            self.page.evaluate("""() => {
              const first = document.querySelector('#target');
              const other = document.querySelector('#other');
              first.before(other);
            }""")
            return original_click(element)

        with patch.object(BrowserElement, "click", reorder_before_click):
            selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.5)

        self.assertTrue(not selected or self.click_ids() == ["target"])
        self.assertNotIn("other", self.click_ids())

    def test_conflicting_visible_and_attribute_identity_fails_closed(self):
        self.set_page(chooser_markup([
            ("conflict", "Continue as other@example.invalid", f'data-email="{TARGET}"'),
        ], heading=ENGLISH_HEADING))
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_chooser_heading_containing_continue_to_codex_is_not_accepted(self):
        self.set_page(chooser_markup([
            ("other", "other@example.invalid", ""),
            ("target", TARGET, ""),
        ], heading="Choose an account to continue to Codex"))
        self.assertEqual(credential_login.classify_login_state(self.driver), "account_chooser")
        self.assert_password_after_target_selection()

    def test_chooser_inside_external_idp_link_fails_closed(self):
        self.set_page(
            '<h1>Choose an account</h1>'
            '<a href="https://accounts.google.invalid/login" data-provider="google">'
            f'<span role="button" name="session_id" id="external">{TARGET}</span></a>'
            '<a href="/log-in-or-create-account">Other account</a>'
        )
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_split_visible_conflicting_identity_fails_closed(self):
        self.set_page(
            '<h1>Choose an account</h1>'
            f'<button name="session_id" id="conflict"><span>{TARGET}</span>'
            '<span>other@<b>example.invalid</b></span></button>'
            '<a href="/log-in-or-create-account">Other account</a>'
        )
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_clipped_target_identity_is_not_selected(self):
        self.set_page(
            '<h1>Choose an account</h1><button name="session_id" id="clipped">'
            f'<span style="clip-path: inset(100%)">{TARGET}</span>Other user</button>'
            '<a href="/log-in-or-create-account">Other account</a>'
        )
        selected = credential_login._select_matching_account(self.driver, TARGET, timeout=0.2)
        self.assertEqual((selected, self.click_ids()), (False, []))

    def test_chooser_recovers_after_brief_remount(self):
        markup = chooser_markup([("target", TARGET, "")], heading=ENGLISH_HEADING)
        self.set_page(markup)
        original_state = credential_login.classify_login_state
        calls = 0

        def remount_once(driver):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.page.evaluate(
                    "markup => { document.querySelector('main').innerHTML = '<p>Loading</p>'; "
                    "setTimeout(() => { document.querySelector('main').innerHTML = markup; }, 150); }",
                    markup,
                )
            return original_state(driver)

        with patch.object(credential_login, "classify_login_state", side_effect=remount_once):
            selected = credential_login._select_matching_account(self.driver, TARGET, timeout=1)
        self.assertEqual((selected, self.click_ids()), (True, ["target"]))

    def test_account_name_grant_is_not_treated_as_oauth_action(self):
        self.set_page(
            '<h1>Choose an account</h1><button name="session_id" id="target">'
            f'<span>Grant Miller</span><span>{TARGET}</span></button>'
            '<a href="/log-in-or-create-account">Other account</a>'
        )
        self.assert_password_after_target_selection()

    def test_non_chooser_consent_page_is_left_untouched(self):
        self.set_page(
            '<h1>Allow access</h1>'
            f'<button id="consent" type="button">Authorize {TARGET}</button>'
            f'<button id="external-idp" type="button" data-provider="google">Google {TARGET}</button>'
        )
        state = credential_login._wait_for_login_state(self.driver, timeout=1)
        self.assertEqual(state, "accepted")
        self.assertEqual(credential_login._resolve_account_chooser(self.driver, TARGET, state), "accepted")
        self.assertEqual(self.click_ids(), [])
        self.assertEqual(self.driver.current_url, FIXTURE_URL)

    def test_login_entry_does_not_accept_oauth_consent(self):
        markup = '<h1>Allow access</h1><button id="consent" type="button">Accept</button>'
        self.install_page_on_navigation(markup, CHOOSER_EVENTS)
        with patch.object(credential_login, "human_delay"):
            state = credential_login._open_and_submit_email(self.driver, TARGET, FIXTURE_URL)

        self.assertEqual(state, "accepted")
        self.assertEqual(self.click_ids(), [])

    def test_unrelated_hosts_cannot_be_accepted_by_oauth_path(self):
        for url in (
            "https://evil.invalid/consent",
            "https://evil.invalid/workspace",
            "https://evil.invalid/add-phone",
            "https://evil.invalid/login?next=/consent",
        ):
            with self.subTest(url=url), patch.object(
                credential_login, "_page_state", return_value={"url": url, "body": "", "inputs": []}
            ):
                self.assertNotEqual(credential_login.classify_login_state(self.driver), "accepted")

    def test_normal_email_form_is_submitted_without_choosing_other_controls(self):
        markup = (
            '<h1>Log in</h1><form><input type="email" name="email" autocomplete="email">'
            '<button id="submit-email" type="submit">Continue</button></form>'
            '<button id="consent" type="button">権限を許可</button>'
            '<button id="external-idp" type="button" data-provider="google">Google</button>'
        )
        self.set_page(markup, EMAIL_EVENTS)
        self.assertEqual(credential_login.classify_login_state(self.driver), "unknown")
        self.install_page_on_navigation(markup, EMAIL_EVENTS)
        with patch.object(credential_login, "human_delay"):
            state = credential_login._open_and_submit_email(self.driver, TARGET, FIXTURE_URL)
        self.assertEqual(state, "email_submitted")
        self.assertEqual(self.page.evaluate("window.submittedEmail"), TARGET)
        self.assertEqual(self.click_ids(), ["submit-email"])
        self.assertEqual(credential_login._wait_for_login_state(self.driver, timeout=1), "password")
        self.assertEqual(self.driver.current_url, FIXTURE_URL)


if __name__ == "__main__":
    unittest.main()
