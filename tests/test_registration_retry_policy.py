import unittest

from core.registration_retry_policy import should_auto_retry_registration_failure


class RegistrationRetryPolicyTests(unittest.TestCase):
    def test_retries_transient_browser_registration_failure_once(self):
        self.assertTrue(
            should_auto_retry_registration_failure(
                "密码页提交失败：vui lòng thử lại",
                retry_attempt=0,
                max_attempts=1,
            )
        )

    def test_does_not_retry_terminal_or_exhausted_failure(self):
        self.assertFalse(
            should_auto_retry_registration_failure(
                "account deactivated",
                retry_attempt=0,
                max_attempts=1,
            )
        )
        self.assertFalse(
            should_auto_retry_registration_failure(
                "密码页提交失败：vui lòng thử lại",
                retry_attempt=1,
                max_attempts=1,
            )
        )


if __name__ == "__main__":
    unittest.main()
