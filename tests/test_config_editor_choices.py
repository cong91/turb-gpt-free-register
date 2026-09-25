import unittest
from unittest.mock import patch

from webui import config_editor
from webui.app import create_app


class ConfigEditorChoicesTests(unittest.TestCase):
    def test_valid_choices_saved_and_pool_are_written(self):
        with patch("config.env_loader.write_env_values", return_value=["TWOFA_PROXY_MODE"]):
            result = config_editor.update_config({"TWOFA_PROXY_MODE": "saved"})
        self.assertEqual(result["updated"], ["TWOFA_PROXY_MODE"])

        with patch("config.env_loader.write_env_values", return_value=["TWOFA_PROXY_MODE"]):
            result = config_editor.update_config({"TWOFA_PROXY_MODE": "pool"})
        self.assertEqual(result["updated"], ["TWOFA_PROXY_MODE"])

    def test_invalid_choice_rejects_entire_batch_before_write(self):
        with patch("config.env_loader.write_env_values") as write_env_values:
            with self.assertRaises(ValueError) as ctx:
                config_editor.update_config(
                    {"TWOFA_PROXY_MODE": "not-valid", "TWOFA_WORKERS": 4}
                )
        write_env_values.assert_not_called()
        self.assertEqual(str(ctx.exception), "配置选项无效")

    def test_invalid_workers_or_queue_limit_rejects_entire_batch_before_write(self):
        invalid_updates = (
            {"TWOFA_WORKERS": 0, "TWOFA_PROXY_MODE": "pool"},
            {"TWOFA_WORKERS": 17, "TWOFA_PROXY_MODE": "pool"},
            {"TWOFA_QUEUE_LIMIT": 0, "TWOFA_PROXY_MODE": "saved"},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                with patch("config.env_loader.write_env_values") as write_env_values:
                    with self.assertRaises(ValueError) as ctx:
                        config_editor.update_config(updates)
                write_env_values.assert_not_called()
                self.assertEqual(str(ctx.exception), "配置数值无效")

    def test_queue_limit_must_cover_workers(self):
        with patch("config.env_loader.write_env_values") as write_env_values:
            with self.assertRaises(ValueError) as ctx:
                config_editor.update_config({"TWOFA_WORKERS": 4, "TWOFA_QUEUE_LIMIT": 3})
        write_env_values.assert_not_called()
        self.assertEqual(str(ctx.exception), "配置数值无效")

    def test_api_returns_400_without_echoing_secret_or_proxy_value(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        submitted = "http://secret-proxy.example:8080/otp-secret"
        with patch("webui.config_editor.update_config", side_effect=ValueError("配置选项无效")):
            response = client.post(
                "/api/config",
                json={"updates": {"TWOFA_PROXY_MODE": submitted, "WEBUI_SESSION_SECRET": "secret"}},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "配置选项无效")
        self.assertNotIn(submitted, response.get_data(as_text=True))
        self.assertNotIn("secret", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
