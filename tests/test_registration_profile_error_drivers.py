import unittest
from typing import ClassVar

from core import (
    browser_use_registration,
    cloakbrowser_registration,
    registration_flow,
)


class RegistrationProfileErrorDriverMirrorTests(unittest.TestCase):
    """Mỗi driver phải phân loại được user_already_exists qua binding riêng của nó
    (mirror tests/test_roxy_profile_error.py — batch 2026-09-21: trang lỗi render
    chậm, alias đã có account server-side phải bỏ và lấy alias mới)."""

    SNAPSHOT: ClassVar[dict] = {
        "url": "https://auth.openai.com/about-you",
        "text": (
            "Oops, an error occurred! An account already exists for this "
            "email address or phone number. Please log in instead. "
            "error_code: user_already_exists"
        ),
        "errors": [],
    }

    def test_registration_flow_classifies_user_already_exists(self):
        error = registration_flow._profile_submission_error(dict(self.SNAPSHOT))

        self.assertIsNotNone(error)
        self.assertIn("already exists for this email", error)

    def test_cloakbrowser_registration_classifies_user_already_exists(self):
        error = cloakbrowser_registration._profile_submission_error(dict(self.SNAPSHOT))

        self.assertIsNotNone(error)
        self.assertIn("already exists for this email", error)

    def test_browser_use_registration_classifies_user_already_exists(self):
        error = browser_use_registration._profile_submission_error(dict(self.SNAPSHOT))

        self.assertIsNotNone(error)
        self.assertIn("already exists for this email", error)


if __name__ == "__main__":
    unittest.main()
