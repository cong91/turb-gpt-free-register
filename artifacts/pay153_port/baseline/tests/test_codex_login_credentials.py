import unittest
from unittest.mock import patch

import pyotp

from core import codex_login_credentials
from core.codex_login_credentials import CodexLoginCredentials


class CodexLoginCredentialsTests(unittest.TestCase):
    def test_normalizes_base32_and_otpauth_secrets(self):
        normalize = getattr(codex_login_credentials, "normalize_totp_secret", None)
        self.assertTrue(callable(normalize))
        self.assertEqual(normalize("jbsw y3dp-ehpk 3pxp"), "JBSWY3DPEHPK3PXP")
        self.assertEqual(
            normalize("otpauth://totp/OpenAI:user@example.com?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"),
            "JBSWY3DPEHPK3PXP",
        )

    def test_generates_totp_after_waiting_past_rollover(self):
        generate = getattr(codex_login_credentials, "generate_totp_code", None)
        self.assertTrue(callable(generate))
        secret = "JBSWY3DPEHPK3PXP"

        with patch.object(codex_login_credentials.time, "time", side_effect=[29.0, 31.0]), patch.object(
            codex_login_credentials.time, "sleep"
        ) as sleep:
            code = generate(secret, min_remaining=2.0)

        sleep.assert_called_once()
        self.assertEqual(code, pyotp.TOTP(secret).at(31.0))

    def test_waits_for_fresh_step_when_previous_code_matches(self):
        secret = "JBSWY3DPEHPK3PXP"
        previous_code = pyotp.TOTP(secret).at(10.0)

        with patch.object(codex_login_credentials.time, "time", side_effect=[10.0, 31.0]), patch.object(
            codex_login_credentials.time, "sleep"
        ) as sleep:
            code = codex_login_credentials.generate_totp_code(secret, previous_code=previous_code)

        sleep.assert_called_once()
        self.assertEqual(code, pyotp.TOTP(secret).at(31.0))
        self.assertNotEqual(code, previous_code)

    def test_repr_redacts_password_and_totp_secret(self):
        credentials = CodexLoginCredentials(
            email="user@example.com",
            password="openai-password",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

        shown = repr(credentials)
        self.assertNotIn("openai-password", shown)
        self.assertNotIn("JBSWY3DPEHPK3PXP", shown)
        self.assertIn("user@example.com", shown)


if __name__ == "__main__":
    unittest.main()
