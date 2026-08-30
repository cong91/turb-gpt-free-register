# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import env_loader
from core import app_state_db
from webui import config_editor


class RuntimeSettingsStorageTests(unittest.TestCase):
    def test_write_env_values_persists_settings_in_canonical_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "turb.env"
            database = Path(tmp) / "turb.sqlite3"
            env_path.write_text("NORDVPN_WG_ENABLED=old\n", encoding="utf-8")

            with (
                patch.object(env_loader, "_ENV_PATH", env_path),
                patch.object(app_state_db, "APP_STATE_DB_PATH", database),
                patch.object(env_loader, "load_env") as load_env,
            ):
                written = env_loader.write_env_values(
                    {"NORDVPN_WG_ENABLED": "True", "EXISTING_SETTING": "keep"}
                )
                self.assertEqual(
                    written,
                    ["NORDVPN_WG_ENABLED", "EXISTING_SETTING"],
                )
                self.assertEqual(
                    app_state_db.get_named_document(
                        env_loader.RUNTIME_SETTINGS_DOCUMENT_KEY,
                        {},
                    ),
                    {
                        "NORDVPN_WG_ENABLED": "True",
                        "EXISTING_SETTING": "keep",
                    },
                )
                load_env.assert_called_once_with(override=True)
                self.assertEqual(
                    env_path.read_text(encoding="utf-8"),
                    "NORDVPN_WG_ENABLED=old\n",
                )

    def test_runtime_document_uses_one_named_sqlite_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "turb.sqlite3"
            with patch.object(app_state_db, "APP_STATE_DB_PATH", database):
                app_state_db.set_named_document(
                    env_loader.RUNTIME_SETTINGS_DOCUMENT_KEY,
                    {"NORDVPN_WG_ENABLED": "True"},
                )

                self.assertEqual(
                    app_state_db.get_named_document(
                        env_loader.RUNTIME_SETTINGS_DOCUMENT_KEY,
                        {},
                    ),
                    {"NORDVPN_WG_ENABLED": "True"},
                )

            self.assertTrue(database.exists())

    def test_write_does_not_call_legacy_env_file_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "turb.env"
            env_path.write_text("NORDVPN_WG_ENABLED=old\n", encoding="utf-8")

            with (
                patch.object(env_loader, "_ENV_PATH", env_path),
                patch.object(env_loader, "load_env"),
                patch.object(env_loader, "_write_runtime_settings") as write_runtime,
            ):
                write_runtime.return_value = ["NORDVPN_WG_ENABLED"]
                written = env_loader.write_env_values(
                    {"NORDVPN_WG_ENABLED": "True"}
                )

            write_runtime.assert_called_once_with({"NORDVPN_WG_ENABLED": "True"})
            self.assertEqual(written, ["NORDVPN_WG_ENABLED"])
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "NORDVPN_WG_ENABLED=old\n",
            )

    def test_sub2api_callback_secret_round_trips_through_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "turb.env"
            database = Path(tmp) / "turb.sqlite3"

            with (
                patch.object(env_loader, "_ENV_PATH", env_path),
                patch.object(app_state_db, "APP_STATE_DB_PATH", database),
                patch.object(env_loader, "load_env"),
            ):
                result = config_editor.update_config(
                    {"SUB2API_AUTOMATION_CALLBACK_SECRET": "callback-secret"}
                )

                self.assertEqual(
                    result["updated"],
                    ["SUB2API_AUTOMATION_CALLBACK_SECRET"],
                )
                self.assertEqual(
                    app_state_db.get_named_document(
                        env_loader.RUNTIME_SETTINGS_DOCUMENT_KEY,
                        {},
                    )["SUB2API_AUTOMATION_CALLBACK_SECRET"],
                    "callback-secret",
                )
                fields = {
                    item["key"]: item
                    for item in config_editor.get_config()
                }
                self.assertEqual(
                    fields["SUB2API_AUTOMATION_CALLBACK_SECRET"]["value"],
                    "callback-secret",
                )

    def test_load_env_prefers_persisted_runtime_setting_over_read_only_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "turb.env"
            env_path.write_text("NORDVPN_WG_ENABLED=False\n", encoding="utf-8")

            with (
                patch.object(env_loader, "_ENV_PATH", env_path),
                patch.object(env_loader, "_PROCESS_ENV_KEYS", frozenset()),
                patch(
                    "core.app_state_db.get_named_document",
                    return_value={"NORDVPN_WG_ENABLED": "True"},
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                env_loader._LOADED = False
                env_loader.load_env(override=True)
                self.assertEqual(os.environ.get("NORDVPN_WG_ENABLED"), "True")

    @patch("config.env_loader.read_runtime_settings", return_value={"NORDVPN_WG_ENABLED": "True"})
    @patch("config.env_loader.read_env_file", return_value={"NORDVPN_WG_ENABLED": "False"})
    @patch("config.env_loader.load_env")
    def test_config_editor_reads_sqlite_settings_first(
        self,
        load_env,
        read_env_file,
        read_runtime_settings,
    ):
        with patch.dict(os.environ, {}, clear=True):
            fields = {
                item["key"]: item
                for item in config_editor.get_config()
            }

        self.assertTrue(fields["NORDVPN_WG_ENABLED"]["value"])
        self.assertEqual(fields["NORDVPN_WG_ENABLED"]["storage"], "sqlite")
        load_env.assert_called_once_with(override=True)
        read_env_file.assert_called_once_with()
        read_runtime_settings.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
