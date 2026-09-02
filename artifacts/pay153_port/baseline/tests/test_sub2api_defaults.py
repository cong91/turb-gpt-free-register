import unittest
from unittest.mock import patch

from config import sub2api as sub2api_config
from core import codex_agent, codex_oauth
from webui import config_editor


AUTH_JSON = {
    "agent_identity": {
        "agent_runtime_id": "runtime-123",
        "agent_private_key": "private-key",
        "account_id": "account-123",
        "chatgpt_user_id": "user-123",
        "email": "user@example.com",
        "plan_type": "free",
    }
}


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class Sub2APIDefaultsTests(unittest.TestCase):
    def test_invalid_group_and_priority_values_fall_back_to_safe_defaults(self):
        with patch.dict(
            vars(sub2api_config),
            {
                "SUB2API_GROUP_IDS": "not-a-group",
                "SUB2API_PRIORITY": "invalid",
                "SUB2API_MODEL": "  ",
            },
        ):
            defaults = sub2api_config.get_sub2api_account_defaults()

        self.assertEqual(defaults, {"group_ids": [14], "priority": 1, "model_mapping": {}})

    def test_empty_group_list_is_preserved_as_an_explicit_choice(self):
        with patch.dict(vars(sub2api_config), {"SUB2API_GROUP_IDS": []}):
            defaults = sub2api_config.get_sub2api_account_defaults()

        self.assertEqual(defaults["group_ids"], [])

    def test_loaded_model_list_splits_commas_and_newlines(self):
        with patch.dict(
            vars(sub2api_config),
            {"SUB2API_MODEL": ["gpt-5.4-mini,gpt-5.5", "gpt-5.6-luna\ngpt-5.6-terra"]},
        ):
            defaults = sub2api_config.get_sub2api_account_defaults()

        self.assertEqual(
            defaults["model_mapping"],
            {
                "gpt-5.4-mini": "gpt-5.4-mini",
                "gpt-5.5": "gpt-5.5",
                "gpt-5.6-luna": "gpt-5.6-luna",
                "gpt-5.6-terra": "gpt-5.6-terra",
            },
        )

    def test_sub2api_settings_are_exposed_in_config_editor(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        self.assertEqual(fields["SUB2API_GROUP_IDS"]["type"], "list_str_multiline")
        self.assertEqual(fields["SUB2API_PRIORITY"]["type"], "int")
        self.assertEqual(fields["SUB2API_MODEL"]["type"], "list_str_multiline")

    def test_sub2api_callback_secret_is_exposed_as_a_persisted_secret_setting(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        callback_secret = fields["SUB2API_AUTOMATION_CALLBACK_SECRET"]
        self.assertEqual(callback_secret["type"], "str")
        self.assertEqual(callback_secret["storage"], "sqlite")
        self.assertTrue(callback_secret["secret"])

    def test_agent_account_entry_uses_configured_group_priority_and_model(self):
        with patch.dict(
            vars(sub2api_config),
            {
                "SUB2API_GROUP_IDS": ["3", "9"],
                "SUB2API_PRIORITY": "7",
                "SUB2API_MODEL": "gpt-5.4-mini,gpt-5.5,gpt-5.6-luna,gpt-5.6-terra",
            },
        ):
            entry = codex_agent.build_sub2api_account_entry(AUTH_JSON)

        self.assertEqual(entry["group_ids"], [3, 9])
        self.assertEqual(entry["priority"], 7)
        self.assertEqual(
            entry["credentials"]["model_mapping"],
            {
                "gpt-5.4-mini": "gpt-5.4-mini",
                "gpt-5.5": "gpt-5.5",
                "gpt-5.6-luna": "gpt-5.6-luna",
                "gpt-5.6-terra": "gpt-5.6-terra",
            },
        )

    def test_codex_session_import_uses_configured_defaults(self):
        with (
            patch.dict(
                vars(sub2api_config),
                {
                    "SUB2API_GROUP_IDS": [14],
                    "SUB2API_PRIORITY": 1,
                    "SUB2API_MODEL": "gpt-5.4-mini,gpt-5.5,gpt-5.6-luna,gpt-5.6-terra",
                },
            ),
            patch("core.codex_agent.requests.post", return_value=FakeResponse()) as post,
        ):
            codex_agent.upload_sub2api_account(
                AUTH_JSON,
                "http://sub2api.test/api/v1/admin/accounts/import/codex-session",
                payload_mode="codex_session_import",
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["group_ids"], [14])
        self.assertEqual(payload["priority"], 1)
        self.assertEqual(
            payload["credential_extras"]["model_mapping"],
            {
                "gpt-5.4-mini": "gpt-5.4-mini",
                "gpt-5.5": "gpt-5.5",
                "gpt-5.6-luna": "gpt-5.6-luna",
                "gpt-5.6-terra": "gpt-5.6-terra",
            },
        )

    def test_oauth_callback_uses_defaults_and_applies_model_mapping(self):
        callback_url = "http://localhost:1455/auth/callback?code=code-123&state=state-123"
        with (
            patch.dict(
                vars(sub2api_config),
                {
                    "SUB2API_GROUP_IDS": [14],
                    "SUB2API_PRIORITY": 1,
                    "SUB2API_MODEL": "gpt-5.4-mini,gpt-5.5,gpt-5.6-luna,gpt-5.6-terra",
                    "SUB2_CODEX_CALLBACK_PATH": "/api/v1/admin/openai/create-from-oauth",
                    "SUB2_CODEX_CALLBACK_PAYLOAD_MODE": "create_from_oauth",
                },
            ),
            patch.object(codex_oauth._cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 1),
            patch.object(
                codex_oauth,
                "_sub2_codex_request_json",
                side_effect=[{"data": {"id": 42}}, {}],
            ) as request,
        ):
            result = codex_oauth._submit_sub2_callback(
                callback_url,
                session_id="session-123",
            )

        self.assertEqual(result, {"data": {"id": 42}})
        self.assertEqual(request.call_count, 2)
        callback_call = request.call_args_list[0]
        self.assertEqual(callback_call.args[:2], ("POST", "/api/v1/admin/openai/create-from-oauth"))
        self.assertEqual(callback_call.args[2]["group_ids"], [14])
        self.assertEqual(callback_call.args[2]["priority"], 1)
        model_call = request.call_args_list[1]
        self.assertEqual(model_call.args[:2], ("PUT", "/api/v1/admin/accounts/42"))
        self.assertEqual(
            model_call.args[2],
            {
                "credentials": {
                    "model_mapping": {
                        "gpt-5.4-mini": "gpt-5.4-mini",
                        "gpt-5.5": "gpt-5.5",
                        "gpt-5.6-luna": "gpt-5.6-luna",
                        "gpt-5.6-terra": "gpt-5.6-terra",
                    }
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
