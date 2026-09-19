import string
import unittest

from core import browser_registration, browser_use_registration, roxy_registration


class RegistrationPasswordTests(unittest.TestCase):
    """Mật khẩu đăng ký chỉ gồm chữ hoa/thường + số, không ký tự đặc biệt."""

    def test_roxy_password_is_alphanumeric_with_all_groups(self):
        password = roxy_registration._generate_roxy_password()

        self.assertEqual(len(password), 14)
        self.assertTrue(set(password) <= set(string.ascii_letters + string.digits))
        self.assertTrue(any(c.isupper() for c in password))
        self.assertTrue(any(c.islower() for c in password))
        self.assertTrue(any(c.isdigit() for c in password))

    def test_browser_password_is_alphanumeric_with_all_groups(self):
        password = browser_registration._generate_roxy_password()

        self.assertEqual(len(password), 14)
        self.assertTrue(set(password) <= set(string.ascii_letters + string.digits))
        self.assertTrue(any(c.isupper() for c in password))
        self.assertTrue(any(c.islower() for c in password))
        self.assertTrue(any(c.isdigit() for c in password))

    def test_browser_use_password_is_alphanumeric_with_all_groups(self):
        password = browser_use_registration._generate_password()

        self.assertEqual(len(password), 14)
        self.assertTrue(set(password) <= set(string.ascii_letters + string.digits))
        self.assertTrue(any(c.isupper() for c in password))
        self.assertTrue(any(c.islower() for c in password))
        self.assertTrue(any(c.isdigit() for c in password))

    def test_browser_use_password_respects_custom_length(self):
        password = browser_use_registration._generate_password(length=16)

        self.assertEqual(len(password), 16)
        self.assertTrue(set(password) <= set(string.ascii_letters + string.digits))


if __name__ == "__main__":
    unittest.main()
