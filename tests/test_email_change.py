import unittest

from core.email_change import EmailChangeInput, parse_email_change_inputs


class EmailChangeInputTests(unittest.TestCase):
    def test_pairs_credentials_with_gmail_api_url_by_line(self):
        result = parse_email_change_inputs(
            "old@example.com|secret|JBSWY3DPEHPK3PXP\n",
            "new@example.com----https://mail.example/otp/1\n",
        )

        self.assertEqual(
            result,
            [
                EmailChangeInput(
                    old_email="old@example.com",
                    password="secret",
                    totp_secret="JBSWY3DPEHPK3PXP",
                    new_email="new@example.com",
                    code_url="https://mail.example/otp/1",
                    gmail_source_email="new@example.com",
                )
            ],
        )

    def test_pairs_dash_delimited_credentials_with_gmail_api_url(self):
        result = parse_email_change_inputs(
            "backend_export.3a@icloud.com----B5H6!!a$ul5Wgv----ATY3FIMESST7OQTCQI7OLYNT7X62UEFH\n",
            "new@example.com----https://mail.example/otp/1\n",
        )

        self.assertEqual(result[0].old_email, "backend_export.3a@icloud.com")
        self.assertEqual(result[0].password, "B5H6!!a$ul5Wgv")
        self.assertEqual(result[0].totp_secret, "ATY3FIMESST7OQTCQI7OLYNT7X62UEFH")

    def test_preserves_other_delimiter_inside_dash_delimited_password(self):
        result = parse_email_change_inputs(
            "old@example.com----pa|ss----JBSWY3DPEHPK3PXP\n",
            "new@example.com----https://mail.example/otp/1\n",
        )

        self.assertEqual(result[0].password, "pa|ss")

    def test_quota_defaults_to_one_and_expands_one_gmail_source_up_to_six(self):
        credentials = "\n".join(
            f"old{i}@example.com|secret|JBSWY3DPEHPK3PXP" for i in range(6)
        )
        result = parse_email_change_inputs(
            credentials,
            "targetaccount@gmail.com----https://mail.example/otp/1",
            quota=6,
        )

        self.assertEqual(len(result), 6)
        self.assertEqual(result[0].new_email, "targetaccount@gmail.com")
        self.assertEqual(len({item.new_email for item in result}), 6)
        self.assertEqual({item.code_url for item in result}, {"https://mail.example/otp/1"})
        self.assertEqual({item.gmail_source_email for item in result}, {"targetaccount@gmail.com"})

    def test_quota_rejects_values_outside_one_to_six(self):
        for quota in (0, 7):
            with self.subTest(quota=quota), self.assertRaisesRegex(ValueError, "between 1 and 6"):
                parse_email_change_inputs(
                    "old@example.com|secret|JBSWY3DPEHPK3PXP",
                    "targetaccount@gmail.com----https://mail.example/otp/1",
                    quota=quota,
                )

    def test_quota_above_one_requires_enough_credentials_and_a_gmail_source(self):
        with self.assertRaisesRegex(ValueError, "2 credential lines"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "targetaccount@gmail.com----https://mail.example/otp/1",
                quota=2,
            )
        with self.assertRaisesRegex(ValueError, "Gmail source"):
            parse_email_change_inputs(
                "old1@example.com|secret|JBSWY3DPEHPK3PXP\nold2@example.com|secret|JBSWY3DPEHPK3PXP",
                "target@example.com----https://mail.example/otp/1",
                quota=2,
            )

    def test_preserves_pipe_in_password_and_rejects_mismatched_counts(self):
        result = parse_email_change_inputs(
            "old@example.com|pa|ss|JBSWY3DPEHPK3PXP",
            "new@example.com----https://mail.example/otp/1",
        )
        self.assertEqual(result[0].password, "pa|ss")

        with self.assertRaisesRegex(ValueError, "require 1 credential lines"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP\nsecond@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1",
            )
        with self.assertRaisesRegex(ValueError, "credential line 1"):
            parse_email_change_inputs(
                "broken credential\nold@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1\nsecond@example.com----https://mail.example/otp/2",
            )
        with self.assertRaisesRegex(ValueError, "code URL must be unique"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP\nsecond@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----https://mail.example/otp/1\nsecond-new@example.com----https://mail.example/otp/1",
            )

    def test_rejects_invalid_target_email_and_non_http_otp_url(self):
        with self.assertRaisesRegex(ValueError, "target email"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "not-an-email----https://mail.example/otp/1",
            )
        with self.assertRaisesRegex(ValueError, "HTTP"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----file:///otp/1",
            )
        with self.assertRaisesRegex(ValueError, "public host"):
            parse_email_change_inputs(
                "old@example.com|secret|JBSWY3DPEHPK3PXP",
                "new@example.com----http://127.0.0.1:8080/otp/1",
            )
