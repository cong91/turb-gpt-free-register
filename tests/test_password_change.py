"""Unit tests cho core.password_change: parse input, resolve DB, browser flow, persist."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db
from core.password_change import (
    PasswordChangeInput,
    change_password_in_browser,
    parse_password_change_inputs,
    resolve_password_change_input,
)

_EMAIL_OTP_URL = "https://auth.openai.com/email-verification?state=otp"
_LOGIN_PASSWORD_URL = "https://auth.openai.com/log-in/password?state=login"
_MFA_URL = "https://auth.openai.com/mfa-challenge?state=mfa"
_NEW_PASSWORD_URL = "https://auth.openai.com/reset-password/new-password?state=new"
_AUTHORIZE_URL = "https://auth.openai.com/authorize?client_id=demo"
_CHATGPT_HOME = "https://chatgpt.com/"
_LOGOUT_URL = "https://chatgpt.com/auth/logout"


class _FakeTime:
    """Đồng hồ ảo: sleep chỉ đẩy mốc thời gian để test không chờ thật."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        self.now += 0.5
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class _FakePasswordDriver:
    """Driver tối thiểu: chatgpt.com đã login, authorize URL redirect tới challenge."""

    def __init__(self, *, landing_url: str = _NEW_PASSWORD_URL, body_text: str = "") -> None:
        self.current_url = _CHATGPT_HOME
        self.landing_url = landing_url
        self.body_text = body_text
        self.visited: list[str] = []
        self.signin_requests: list[tuple[str, str]] = []

    def get(self, url: str) -> None:
        self.visited.append(url)
        if "auth.openai.com/authorize" in url and self.landing_url:
            self.current_url = self.landing_url
        else:
            self.current_url = url

    def set_page_load_timeout(self, timeout: int) -> None:
        return None

    def set_script_timeout(self, timeout: int) -> None:
        return None

    def execute_script(self, script: str, *args):
        if "oaicom_stable_id" in script:
            return "device-fixed"
        if 'input[type="password"' in script:
            return {"url": self.current_url, "inputs": [{"id": "pw-input"}], "button": {"id": "submit-btn"}}
        if "innerText" in script:
            return self.body_text
        return None

    def execute_async_script(self, script: str, *args):
        if "/api/auth/csrf" in script:
            return {"ok": True, "status": 200, "data": {"csrfToken": "csrf-token-1"}}
        if "method: 'POST'" in script:
            self.signin_requests.append((str(args[0]), str(args[1])))
            return {"ok": True, "status": 200, "data": {"url": _AUTHORIZE_URL}, "body": "{}"}
        return {"ok": False, "status": 0, "error": "unexpected async script"}


class PasswordChangeInputTests(unittest.TestCase):
    """parse_password_change_inputs + resolve_password_change_input."""

    def test_parse_accepts_one_to_three_column_lines(self):
        items = parse_password_change_inputs(
            "# ghi chú\n"
            "\n"
            "solo@example.com\n"
            "two@example.com----current-two\n"
            "three@example.com----current-three----TOTPBASE32"
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].email, "solo@example.com")
        self.assertEqual(items[0].current_password, "")
        self.assertIsNone(items[0].totp_secret)
        self.assertEqual(items[1].email, "two@example.com")
        self.assertEqual(items[1].current_password, "current-two")
        self.assertIsNone(items[1].totp_secret)
        self.assertEqual(items[2].current_password, "current-three")
        self.assertEqual(items[2].totp_secret, "TOTPBASE32")

    def test_parse_rejects_invalid_email(self):
        with self.assertRaises(ValueError):
            parse_password_change_inputs("not-an-email")

    def test_parse_rejects_lines_with_more_than_three_columns(self):
        with self.assertRaises(ValueError):
            parse_password_change_inputs("a@example.com----cur----totp----extra")

    def test_parse_rejects_more_than_50_items(self):
        text = "\n".join(f"user{i}@example.com" for i in range(51))

        with self.assertRaises(ValueError):
            parse_password_change_inputs(text)

    def test_parse_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            parse_password_change_inputs("")
        with self.assertRaises(ValueError):
            parse_password_change_inputs("   \n  \n")

    def test_parse_rejects_duplicate_email(self):
        with self.assertRaises(ValueError):
            parse_password_change_inputs("a@example.com\nA@example.com")

    def test_resolve_fills_current_password_from_db_and_uses_reset_mode(self):
        with (
            patch(
                "core.password_change.db.get_account_by_email",
                return_value={"registration_password": "reg-pw", "totp_secret": None},
            ),
            patch(
                "core.registration_flow._generate_registration_password",
                return_value="generated-pw",
            ),
        ):
            resolved = resolve_password_change_input(PasswordChangeInput(email="user@example.com"))

        self.assertEqual(resolved.current_password, "reg-pw")
        self.assertEqual(resolved.mode, "post_login_password_reset")
        self.assertEqual(resolved.new_password, "generated-pw")
        self.assertIsNone(resolved.totp_secret)

    def test_resolve_without_password_uses_add_mode(self):
        with (
            patch("core.password_change.db.get_account_by_email", return_value={}),
            patch(
                "core.registration_flow._generate_registration_password",
                return_value="generated-pw",
            ),
        ):
            resolved = resolve_password_change_input(PasswordChangeInput(email="user@example.com"))

        self.assertEqual(resolved.current_password, "")
        self.assertEqual(resolved.mode, "post_login_add_password")

    def test_resolve_missing_account_still_uses_add_mode(self):
        with (
            patch("core.password_change.db.get_account_by_email", return_value=None),
            patch(
                "core.registration_flow._generate_registration_password",
                return_value="generated-pw",
            ),
        ):
            resolved = resolve_password_change_input(PasswordChangeInput(email="user@example.com"))

        self.assertEqual(resolved.mode, "post_login_add_password")

    def test_resolve_takes_totp_secret_from_db_when_missing(self):
        with (
            patch(
                "core.password_change.db.get_account_by_email",
                return_value={"registration_password": "reg-pw", "totp_secret": "DBTOTP-SECRET"},
            ),
            patch(
                "core.registration_flow._generate_registration_password",
                return_value="generated-pw",
            ),
        ):
            resolved = resolve_password_change_input(PasswordChangeInput(email="user@example.com"))

        self.assertEqual(resolved.totp_secret, "DBTOTP-SECRET")

    def test_resolve_generates_a_different_password_per_account(self):
        with (
            patch("core.password_change.db.get_account_by_email", return_value=None),
            patch(
                "core.registration_flow._generate_registration_password",
                side_effect=["pw-one", "pw-two", "pw-three"],
            ) as generate,
        ):
            resolved = [
                resolve_password_change_input(PasswordChangeInput(email=f"user{i}@example.com"))
                for i in range(3)
            ]

        self.assertEqual([item.new_password for item in resolved], ["pw-one", "pw-two", "pw-three"])
        self.assertEqual(len({item.new_password for item in resolved}), 3)
        self.assertEqual(generate.call_count, 3)

    def test_resolve_rejects_unknown_mode(self):
        with (
            patch("core.password_change.db.get_account_by_email", return_value=None),
            patch(
                "core.registration_flow._generate_registration_password",
                return_value="generated-pw",
            ),self.assertRaises(ValueError)
        ):
            resolve_password_change_input(
                PasswordChangeInput(email="user@example.com", mode="bogus")
            )


class PasswordChangeDbTests(unittest.TestCase):
    """db.update_account_password ghi registration_password + merge extra_json."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        patches = (
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            patch.object(db, "_SQLITE_PATH", root / "turb.sqlite3"),
            patch.object(db, "_DEFAULT_SQLITE_PATH", root / "turb.sqlite3"),
            patch.object(db, "_SQLITE_READY", False),
            patch.object(db, "_SQLITE_READY_PATH", None),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_update_account_password_persists_and_merges_extra_without_touching_twofa(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="browser-token",
            registration_password="old-pw",
            totp_secret="TOTP-KEEP",
            twofa_status="active",
            extra={"note": "keepme"},
        )

        self.assertTrue(
            db.update_account_password(
                account_id,
                password="new-pw",
                extra_updates={"changed_via": "password-change"},
            )
        )
        row = db.get_account(account_id)
        self.assertEqual(row["registration_password"], "new-pw")
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["note"], "keepme")
        self.assertEqual(extra["changed_via"], "password-change")
        self.assertTrue(extra.get("password_changed_at"))
        self.assertEqual(row["totp_secret"], "TOTP-KEEP")
        self.assertEqual(row["twofa_status"], "active")

    def test_update_account_password_rejects_blank_password(self):
        account_id = db.insert_account(
            email="user@example.com",
            access_token="browser-token",
            registration_password="old-pw",
        )

        self.assertFalse(db.update_account_password(account_id, password="   "))
        row = db.get_account(account_id)
        self.assertEqual(row["registration_password"], "old-pw")


class PasswordChangeBrowserTests(unittest.TestCase):
    """change_password_in_browser với fake driver; patch collaborator tại use site."""

    def setUp(self):
        self.driver = _FakePasswordDriver()
        self._apply("core.password_change.time", _FakeTime())
        self._apply("core.password_change.human_delay", lambda *args, **kwargs: None)
        self._apply("core.password_change._page_warmup", MagicMock())
        self._apply("core.password_change._maybe_accept", MagicMock())
        self._apply("core.password_change._wait_for_browser_challenge", MagicMock())
        self._apply(
            "core.password_change.snapshot_verification_code", MagicMock(return_value=None)
        )
        self.acknowledge = self._apply(
            "core.password_change.acknowledge_verification_code", MagicMock()
        )
        self.wait_for_otp = self._apply(
            "core.password_change.wait_for_otp", MagicMock(return_value="654321")
        )
        self._apply(
            "core.password_change.fetch_session",
            MagicMock(return_value={"accessToken": "browser-token"}),
        )
        self._apply(
            "core.password_change._fetch_chatgpt_session",
            MagicMock(return_value={"accessToken": "final-token"}),
        )
        self._apply("core.password_change._human_type_text", MagicMock())
        self.human_click = self._apply("core.password_change._human_click", MagicMock())
        self.click_continue = self._apply("core.password_change._click_continue", MagicMock())
        self._apply("core.password_change._clear_otp_inputs", MagicMock())
        self.type_otp = self._apply("core.password_change._type_otp", MagicMock())
        self._apply("core.password_change._click_resend_email_otp", MagicMock())
        self._apply(
            "core.password_change._wait_after_email_otp_submit", MagicMock(return_value="accepted")
        )

    def _apply(self, target: str, replacement):
        patcher = patch(target, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def _jump_to(self, url: str):
        def _flip(*args, **kwargs):
            self.driver.current_url = url

        return _flip

    def _item(self, **overrides) -> PasswordChangeInput:
        fields = {
            "email": "user@example.com",
            "current_password": "cur-pw",
            "new_password": "NEWPW-123",
            "mode": "post_login_password_reset",
        }
        fields.update(overrides)
        return PasswordChangeInput(**fields)

    def test_email_otp_landing_changes_password_and_keeps_session(self):
        self.driver.landing_url = _EMAIL_OTP_URL
        self.click_continue.side_effect = self._jump_to(_NEW_PASSWORD_URL)
        self.human_click.side_effect = self._jump_to(_CHATGPT_HOME)

        result = change_password_in_browser(self.driver, self._item())

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(result["mode"], "post_login_password_reset")
        self.assertEqual(result["new_password"], "NEWPW-123")
        self.assertEqual(result["access_token"], "final-token")
        self.assertNotIn("already_set", result)
        self.wait_for_otp.assert_called_once()
        self.assertEqual(self.wait_for_otp.call_args.args[0], "user@example.com")
        self.type_otp.assert_called_once_with(self.driver, "654321")
        self.acknowledge.assert_called_once_with(
            "user@example.com", "654321", stage="password_change_email_otp"
        )
        signin_url, signin_body = self.driver.signin_requests[0]
        self.assertIn("/api/auth/signin/openai", signin_url)
        self.assertIn("post_login_password_reset=true", signin_url)
        self.assertIn("login_hint=user%40example.com", signin_url)
        self.assertIn("reauth=password", signin_url)
        self.assertIn("csrfToken=csrf-token-1", signin_body)
        self.assertEqual(self.driver.current_url, _LOGOUT_URL)

    def test_login_password_landing_submits_current_password(self):
        self.driver.landing_url = _LOGIN_PASSWORD_URL
        self.human_click.side_effect = self._jump_to(_CHATGPT_HOME)
        with patch(
            "core.browser_twofa_login._login_password",
            side_effect=self._jump_to(_NEW_PASSWORD_URL),
        ) as login_password:
            result = change_password_in_browser(self.driver, self._item())

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["mode"], "post_login_password_reset")
        self.assertEqual(result["access_token"], "final-token")
        login_password.assert_called_once_with(self.driver, "cur-pw")

    def test_totp_landing_without_secret_fails_without_leaking_secrets(self):
        self.driver.landing_url = _MFA_URL
        with patch("core.password_change.classify_login_state", return_value="totp"):
            result = change_password_in_browser(
                self.driver,
                self._item(new_password="NEWPW-SECRET-123"),
            )

        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])
        self.assertIn("2FA", result["error"])
        self.assertNotIn("NEWPW-SECRET-123", result["error"])
        self.assertNotIn("cur-pw", result["error"])
        self.assertEqual(result["access_token"], "browser-token")

    def test_password_already_set_marks_result(self):
        self.driver.landing_url = _NEW_PASSWORD_URL
        self.driver.body_text = "Reset your password. Password_Already_Set"

        result = change_password_in_browser(
            self.driver,
            self._item(mode="post_login_add_password", current_password=""),
        )

        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result["already_set"])
        self.assertEqual(result["mode"], "post_login_add_password")
        self.assertEqual(result["new_password"], "NEWPW-123")

    def test_stuck_new_password_page_reports_failure(self):
        self.driver.landing_url = _NEW_PASSWORD_URL
        self.driver.body_text = "unexpected error page"

        result = change_password_in_browser(
            self.driver,
            self._item(new_password="NEWPW-SECRET-123"),
        )

        self.assertFalse(result["ok"])
        self.assertIn("chưa hoàn tất", result["error"])
        self.assertNotIn("NEWPW-SECRET-123", result["error"])


if __name__ == "__main__":
    unittest.main()
