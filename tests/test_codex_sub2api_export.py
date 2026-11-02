import io
import json
import unittest
import zipfile
from unittest.mock import patch

from core.codex_oauth import to_sub2api_data_account, to_sub2api_data_payload
from webui.app import create_app


class CodexSub2apiExportTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_to_sub2api_data_account_maps_credentials_and_requires_tokens(self):
        account = to_sub2api_data_account({
            "id_token": "id-token",
            "access_token": "at",
            "refresh_token": "rt",
            "account_id": "acct",
            "last_refresh": "2026-10-22T00:00:00Z",
            "email": "one@example.com",
            "type": "codex",
            "expired": "2026-11-18T00:00:00Z",
            "plan_type": "plus",
        })
        self.assertEqual(account, {
            "name": "one@example.com",
            "platform": "openai",
            "type": "oauth",
            "concurrency": 10,
            "priority": 1,
            "credentials": {
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": "id-token",
                "email": "one@example.com",
                "chatgpt_account_id": "acct",
                "expires_at": "2026-11-18T00:00:00Z",
                "plan_type": "plus",
            },
            "extra": {
                "email_key": "one_example_com",
                "last_refresh": "2026-10-22T00:00:00Z",
            },
        })
        with self.assertRaises(ValueError):
            to_sub2api_data_account({"email": "one@example.com"})

    def test_to_sub2api_data_payload_wraps_accounts(self):
        payload = to_sub2api_data_payload(
            [
                {"access_token": "at", "refresh_token": "rt", "email": "a@example.com", "account_id": "acct-a"},
                {"access_token": "at2", "refresh_token": "rt2", "email": "b@example.com"},
            ],
            "2026-10-26T00:00:00Z",
        )
        self.assertEqual(payload["type"], "sub2api-data")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["exported_at"], "2026-10-26T00:00:00Z")
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(payload["accounts"][0]["credentials"]["chatgpt_account_id"], "acct-a")
        self.assertEqual(payload["accounts"][1]["name"], "b@example.com")

    def test_sub2api_bulk_export_requires_webui_authorization(self):
        response = self.client.post(
            "/api/codex/download-bulk-sub2api",
            json={"filenames": ["codex-one.json"]},
        )

        self.assertEqual(response.status_code, 401)

    @patch("webui.app.db.mark_codex_exported")
    @patch("webui.app.db.read_codex_credential")
    def test_sub2api_bulk_export_returns_sub2api_data_zip(
        self, read_codex_credential, mark_codex_exported,
    ):
        read_codex_credential.side_effect = [
            (json.dumps({
                "id_token": "id-one",
                "access_token": "at-one",
                "refresh_token": "rt-one",
                "account_id": "acct-one",
                "last_refresh": "2026-10-22T00:00:00Z",
                "email": "one@example.com",
                "type": "codex",
            }), "codex-one.json"),
            (json.dumps({
                "access_token": "at-two",
                "refresh_token": "rt-two",
                "email": "two@example.com",
                "type": "codex",
            }), "codex-two.json"),
        ]

        response = self.client.post(
            "/api/codex/download-bulk-sub2api",
            json={"filenames": ["codex-one.json", "codex-two.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "2")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "0")
        self.assertEqual(
            mark_codex_exported.call_args_list,
            [unittest.mock.call("codex-one.json"), unittest.mock.call("codex-two.json")],
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            members = bundle.namelist()
            self.assertEqual(members, ["sub2api-data.json", "manifest.json"])
            payload = json.loads(bundle.read("sub2api-data.json"))
            manifest = json.loads(bundle.read("manifest.json"))

        self.assertEqual(payload["type"], "sub2api-data")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 2)
        first = payload["accounts"][0]
        self.assertEqual(first["name"], "one@example.com")
        self.assertEqual(first["platform"], "openai")
        self.assertEqual(first["type"], "oauth")
        self.assertEqual(first["credentials"], {
            "access_token": "at-one",
            "refresh_token": "rt-one",
            "id_token": "id-one",
            "email": "one@example.com",
            "chatgpt_account_id": "acct-one",
        })
        self.assertNotIn("expires_at", first["credentials"])
        self.assertEqual(manifest["source"], "sub2api")
        self.assertEqual(manifest["count"], 2)
        self.assertFalse(manifest["errors"])

    @patch("webui.app.db.mark_codex_exported")
    @patch("webui.app.db.read_codex_credential")
    def test_sub2api_bulk_export_rejects_over_limit_and_counts_duplicates(
        self, read_codex_credential, mark_codex_exported,
    ):
        response = self.client.post(
            "/api/codex/download-bulk-sub2api",
            json={"filenames": [f"codex-{i}.json" for i in range(1001)]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 400)

        read_codex_credential.return_value = (
            json.dumps({"access_token": "at", "refresh_token": "rt", "email": "one@example.com"}),
            "codex-one.json",
        )
        response = self.client.post(
            "/api/codex/download-bulk-sub2api",
            json={"filenames": ["codex-one.json", "codex-one.json"] + [f"codex-{i}.json" for i in range(998)]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "999")

    @patch("webui.app.db.mark_codex_exported")
    @patch("webui.app.db.read_codex_credential")
    def test_sub2api_bulk_export_skips_unreadable_and_tokenless_credentials(
        self, read_codex_credential, mark_codex_exported,
    ):
        read_codex_credential.side_effect = [
            (json.dumps({"email": "tokenless@example.com", "type": "codex"}), "codex-tokenless.json"),
            ValueError("文件不存在: codex-missing.json"),
            (json.dumps({"access_token": "at-ok", "email": "ok@example.com"}), "codex-ok.json"),
        ]

        response = self.client.post(
            "/api/codex/download-bulk-sub2api",
            json={"filenames": ["codex-tokenless.json", "codex-missing.json", "codex-ok.json"]},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Codex-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Codex-Skipped-Count"], "2")
        mark_codex_exported.assert_called_once_with("codex-ok.json")
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        self.assertEqual(manifest["count"], 1)
        self.assertEqual(
            {err["filename"] for err in manifest["errors"]},
            {"codex-tokenless.json", "codex-missing.json"},
        )


if __name__ == "__main__":
    unittest.main()
