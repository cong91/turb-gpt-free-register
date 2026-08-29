# -*- coding: utf-8 -*-
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from config import email as email_config
from core import tinyhost_mail_client


class TinyHostMailClientTests(unittest.TestCase):
    def setUp(self):
        self.health_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.health_dir.cleanup)
        self.health_path = Path(self.health_dir.name) / "tinyhost_domain_health.json"
        self.health_path_patch = patch.object(
            tinyhost_mail_client, "_DOMAIN_HEALTH_PATH", self.health_path
        )
        self.health_path_patch.start()
        self.addCleanup(self.health_path_patch.stop)
        tinyhost_mail_client._CONTEXT_CACHE.clear()
        tinyhost_mail_client._UNSUPPORTED_DOMAINS.clear()
        tinyhost_mail_client._SUPPORTED_DOMAINS.clear()
        tinyhost_mail_client._TESTING_DOMAINS.clear()
        tinyhost_mail_client._DOMAIN_HEALTH_LOADED = False

    def test_create_account_reads_all_domains_without_suffix_filter(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "domains": ["example.com", "mail.example.net", "usable.shop", "also.xyz"],
            "total": 4,
        }

        with patch.object(email_config, "TINYHOST_API_BASE", "https://tinyhost.shop"), patch.object(
            email_config, "TINYHOST_RANDOM_LOCAL_LENGTH", 8
        ), patch("core.tinyhost_mail_client.requests.get", return_value=response) as get, patch(
            "core.tinyhost_mail_client.random.choice", return_value="usable.shop"
        ), patch("core.tinyhost_mail_client.secrets.choice", return_value="a"):
            account = tinyhost_mail_client.create_account()

        self.assertEqual(account.domain, "usable.shop")
        get.assert_called_once_with(
            "https://tinyhost.shop/api/all-domains/",
            params=None,
            headers={"Accept": "application/json"},
            timeout=20,
        )

    def test_disabled_domain_is_skipped_after_chatgpt_rejection(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "domains": ["rejected.example", "usable.example"],
            "total": 2,
        }
        account = tinyhost_mail_client.TinyHostAccount(
            email="user@rejected.example",
            domain="rejected.example",
            user="user",
        )
        tinyhost_mail_client._CONTEXT_CACHE[account.email] = account

        with patch("core.tinyhost_mail_client.requests.get", return_value=response), patch(
            "core.tinyhost_mail_client.random.choice", return_value="usable.example"
        ) as choose_domain, patch("core.tinyhost_mail_client.secrets.choice", return_value="a"):
            tinyhost_mail_client.release_account(account.email, status="disabled", note="about-you: unsupported email")
            created = tinyhost_mail_client.create_account()

        self.assertEqual(created.domain, "usable.example")
        choose_domain.assert_called_once_with(["usable.example"])
        self.assertIn("rejected.example", json.loads(self.health_path.read_text(encoding="utf-8"))["unsupported_domains"])

    def test_supported_domain_is_persisted_and_skipped_during_first_pass(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "domains": ["already-good.example", "untested.example"],
            "total": 2,
        }
        tinyhost_mail_client.mark_domain_supported("user@already-good.example")
        tinyhost_mail_client._SUPPORTED_DOMAINS.clear()
        tinyhost_mail_client._DOMAIN_HEALTH_LOADED = False

        with patch("core.tinyhost_mail_client.requests.get", return_value=response), patch(
            "core.tinyhost_mail_client.random.choice", return_value="untested.example"
        ) as choose_domain, patch("core.tinyhost_mail_client.secrets.choice", return_value="a"):
            created = tinyhost_mail_client.create_account()

        self.assertEqual(created.domain, "untested.example")
        choose_domain.assert_called_once_with(["untested.example"])

    def test_fetch_latest_otp_reads_tinyhost_inbox_fields(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        response = Mock(status_code=200)
        response.json.return_value = {
            "emails": [
                {
                    "id": 123,
                    "subject": "Your ChatGPT verification code",
                    "sender": "noreply@openai.com",
                    "date": now,
                    "body": "Your verification code is 654321",
                    "html_body": "<p>Your verification code is <b>654321</b></p>",
                }
            ],
            "total": 1,
            "page": 1,
            "limit": 20,
            "has_more": False,
        }
        account = tinyhost_mail_client.TinyHostAccount(
            email="testuser@example.com",
            domain="example.com",
            user="testuser",
        )
        tinyhost_mail_client._CONTEXT_CACHE[account.email] = account

        with patch.object(email_config, "TINYHOST_API_BASE", "https://tinyhost.shop"), patch.object(
            email_config, "TINYHOST_REQUEST_TIMEOUT", 25
        ), patch("core.tinyhost_mail_client.requests.get", return_value=response) as get:
            code = tinyhost_mail_client.fetch_latest_otp(
                account.email,
                after_ts=0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")
        get.assert_called_once_with(
            "https://tinyhost.shop/api/email/example.com/testuser/",
            params={"page": 1, "limit": 20},
            headers={"Accept": "application/json"},
            timeout=25,
        )

    def test_create_account_rejects_empty_domain_response(self):
        response = Mock(status_code=200)
        response.json.return_value = {"domains": [], "total": 0}

        with patch("core.tinyhost_mail_client.requests.get", return_value=response), self.assertRaisesRegex(
            tinyhost_mail_client.TinyHostError, "域名"
        ):
            tinyhost_mail_client.create_account()


if __name__ == "__main__":
    unittest.main()
