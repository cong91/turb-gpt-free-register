import unittest

from core.registration_retry_policy import should_auto_retry_registration_failure


class RegistrationRetryPolicyTests(unittest.TestCase):
    def test_retries_transient_browser_registration_failure_once(self):
        self.assertTrue(
            should_auto_retry_registration_failure(
                "密码页提交失败：vui lòng thử lại",
                email_source="outlook",
                retry_attempt=0,
                max_attempts=1,
            )
        )

    def test_does_not_retry_terminal_or_exhausted_failure(self):
        self.assertFalse(
            should_auto_retry_registration_failure(
                "account deactivated",
                email_source="outlook",
                retry_attempt=0,
                max_attempts=1,
            )
        )
        self.assertFalse(
            should_auto_retry_registration_failure(
                "密码页提交失败：vui lòng thử lại",
                email_source="outlook",
                retry_attempt=1,
                max_attempts=1,
            )
        )

    def test_retries_602_only_for_url_backed_gmail_sources(self):
        for source in ("gmail_api_url", "qan8_gmail_api"):
            with self.subTest(source=source):
                self.assertTrue(
                    should_auto_retry_registration_failure(
                        "Provider error code=602",
                        email_source=source,
                        retry_attempt=0,
                        max_attempts=1,
                    )
                )
        self.assertFalse(
            should_auto_retry_registration_failure(
                "Provider error code=602",
                email_source="outlook",
                retry_attempt=0,
                max_attempts=1,
            )
        )


if __name__ == "__main__":
    unittest.main()
