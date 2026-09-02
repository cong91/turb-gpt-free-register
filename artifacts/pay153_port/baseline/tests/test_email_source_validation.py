# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace

from webui.email_source_validation import validate_email_sources


class EmailSourceValidationTests(unittest.TestCase):
    def test_gmail_cdk_requires_cards_and_valid_limit(self):
        config = SimpleNamespace(
            GMAIL_123452026_API_BASE="https://mail.example.com/api",
            GMAIL_123452026_ALLOW_INSECURE_HTTP=False,
            GMAIL_123452026_ACCOUNTS_PER_CDK=6,
        )
        self.assertIn("CDK", validate_email_sources(["gmail_123452026"], config, []))

        config.GMAIL_123452026_ACCOUNTS_PER_CDK = 7
        self.assertIn("1 đến 6", validate_email_sources(["gmail_123452026"], config, ["SECRET"]))

    def test_gmail_cdk_accepts_http_endpoint_without_opt_in(self):
        config = SimpleNamespace(
            GMAIL_123452026_API_BASE="http://gmail.123452026.xyz/api",
            GMAIL_123452026_ALLOW_INSECURE_HTTP=False,
            GMAIL_123452026_ACCOUNTS_PER_CDK=6,
        )
        self.assertIsNone(validate_email_sources(["gmail_123452026"], config, ["SECRET"]))

        config.GMAIL_123452026_API_BASE = "ftp://gmail.123452026.xyz/api"
        self.assertIn("không hợp lệ", validate_email_sources(["gmail_123452026"], config, ["SECRET"]))

    def test_gmail_cdk_validates_real_routed_domains(self):
        config = SimpleNamespace(
            GMAIL_123452026_API_BASE="https://mail.example.com/api",
            GMAIL_123452026_ACCOUNTS_PER_CDK=3,
        )

        self.assertIsNone(validate_email_sources(
            ["gmail_123452026"],
            config,
            ["SECRET"],
            gmail_routed_domains=["relay-one.net", "relay-two.org"],
        ))
        self.assertIn("test", validate_email_sources(
            ["gmail_123452026"],
            config,
            ["SECRET"],
            gmail_routed_domains=["relay.test"],
        ))
        self.assertIn("tối đa 2", validate_email_sources(
            ["gmail_123452026"],
            config,
            ["SECRET"],
            gmail_routed_domains=["one.net", "two.net", "three.net"],
        ))

    def test_paymesh_requires_cards_and_valid_endpoint(self):
        config = SimpleNamespace(
            PAYMESH_API_BASE="https://sms.paymesh.cn",
            PAYMESH_ACCOUNTS_PER_CDK=6,
        )
        self.assertIn("MAIL card", validate_email_sources(["paymesh"], config, paymesh_cdks=[]))

        config.PAYMESH_ACCOUNTS_PER_CDK = 0
        self.assertIn("1 đến 6", validate_email_sources(["paymesh"], config, paymesh_cdks=["MAIL-ONE"]))

        config.PAYMESH_ACCOUNTS_PER_CDK = 6
        config.PAYMESH_API_BASE = "ftp://sms.paymesh.cn"
        self.assertIn("không hợp lệ", validate_email_sources(["paymesh"], config, paymesh_cdks=["MAIL-ONE"]))

        config.PAYMESH_API_BASE = "https://sms.paymesh.cn"
        self.assertIsNone(validate_email_sources(["paymesh"], config, paymesh_cdks=["MAIL-ONE"]))

    def test_paymesh_accepts_test_routed_domains(self):
        config = SimpleNamespace(
            PAYMESH_API_BASE="https://sms.paymesh.cn",
            PAYMESH_ACCOUNTS_PER_CDK=6,
        )
        self.assertIsNone(validate_email_sources(
            ["paymesh"], config, paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["test.com", "mail.invalid"],
        ))

    def test_paymesh_rejects_more_than_two_routed_domains(self):
        config = SimpleNamespace(
            PAYMESH_API_BASE="https://sms.paymesh.cn",
            PAYMESH_ACCOUNTS_PER_CDK=6,
        )
        err = validate_email_sources(
            ["paymesh"], config, paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["a.test", "b.test", "c.test"],
        )
        self.assertIsNotNone(err)
        self.assertIn("tối đa", err)

    def test_paymesh_rejects_ip_routed_domain(self):
        config = SimpleNamespace(
            PAYMESH_API_BASE="https://sms.paymesh.cn",
            PAYMESH_ACCOUNTS_PER_CDK=6,
        )
        err = validate_email_sources(
            ["paymesh"], config, paymesh_cdks=["MAIL-ONE"],
            paymesh_routed_domains=["127.0.0.1"],
        )
        self.assertIsNotNone(err)
        self.assertIn("IP", err)

    def test_tinyhost_has_no_card_prerequisite(self):
        config = SimpleNamespace(TINYHOST_API_BASE="https://tinyhost.shop")
        self.assertIsNone(validate_email_sources(["tinyhost"], config))


if __name__ == "__main__":
    unittest.main()
