import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import env_loader, roxy_profile_manager
from core import roxy_profile_manager as roxy_profile_manager_service
from core.app_state_db import APP_STATE_DB_PATH
from webui import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_paymesh_otp_wait_env_override_is_integer(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PAYMESH_OTP_MAX_WAIT": 180}
        try:
            with patch.dict(os.environ, {"PAYMESH_OTP_MAX_WAIT": "240"}, clear=True):
                env_loader.apply_env_overrides(
                    namespace, {"PAYMESH_OTP_MAX_WAIT": "int"}
                )
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PAYMESH_OTP_MAX_WAIT"], 240)
        field = next(
            item for item in config_editor.EDITABLE_FIELDS
            if item["key"] == "PAYMESH_OTP_MAX_WAIT"
        )
        self.assertEqual(field["type"], "int")

    def test_tinyhost_config_fields_are_exposed(self):
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["TINYHOST_API_BASE"]["storage"], "sqlite")
        self.assertEqual(fields["TINYHOST_REQUEST_TIMEOUT"]["type"], "int")
        self.assertEqual(fields["TINYHOST_RANDOM_LOCAL_LENGTH"]["type"], "int")

    def test_twofa_otp_wait_default_and_webui_field_are_exposed(self):
        from config import twofa

        source = Path(twofa.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            config_editor._parse_value_from_source(source, "TWOFA_OTP_MAX_WAIT", "int"),
            90,
        )
        field = next(
            item for item in config_editor.EDITABLE_FIELDS
            if item["key"] == "TWOFA_OTP_MAX_WAIT"
        )
        self.assertEqual(field["file"], "twofa.py")
        self.assertEqual(field["type"], "int")

    def test_roxy_offline_open_is_enabled_after_parity_gate(self):
        self.assertTrue(roxy_profile_manager.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED)
        field = next(
            item for item in config_editor.EDITABLE_FIELDS
            if item["key"] == "ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED"
        )
        self.assertEqual(field["type"], "bool")

    def test_roxy_catalog_is_locked_to_central_database(self):
        self.assertEqual(roxy_profile_manager_service._store().path, APP_STATE_DB_PATH)
        self.assertFalse(hasattr(roxy_profile_manager, "ROXY_PROFILE_MANAGER_DB_PATH"))
        self.assertNotIn(
            "ROXY_PROFILE_MANAGER_DB_PATH",
            {item["key"] for item in config_editor.EDITABLE_FIELDS},
        )

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))


if __name__ == "__main__":
    unittest.main()
