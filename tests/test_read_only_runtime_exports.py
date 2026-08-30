# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import app_state_db, db


class ReadOnlyRuntimeExportTests(unittest.TestCase):
    def _runtime_symlink(self, root: Path, runtime: Path, filename: str) -> tuple[Path, Path]:
        project_export = root / filename
        runtime_export = runtime / filename
        try:
            project_export.symlink_to(runtime_export)
        except OSError as exc:
            self.skipTest(f"symlink support is unavailable: {exc}")
        return project_export, runtime_export

    def test_project_runtime_document_uses_lexical_path_without_resolving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            logical_export = MagicMock(spec=Path)
            logical_export.absolute.return_value = root / "accounts.json"

            with patch.object(db, "_PROJECT_ROOT", root):
                self.assertTrue(db._is_project_runtime_document(logical_export))

            logical_export.resolve.assert_not_called()

    def test_compatibility_export_writes_to_resolved_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_export = Path(tmp) / "runtime" / "accounts.json"
            logical_export = MagicMock(spec=Path)
            logical_export.resolve.return_value = runtime_export
            payload = '[{"id": 1}]'

            app_state_db._write_compatibility_export(logical_export, payload)

            logical_export.resolve.assert_called_once_with()
            self.assertEqual(runtime_export.read_text(encoding="utf-8"), payload)

    def test_json_export_uses_sqlite_and_keeps_runtime_volume_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            runtime = Path(tmp) / "runtime"
            root.mkdir()
            runtime.mkdir()
            export, runtime_export = self._runtime_symlink(root, runtime, "accounts.json")
            payload = [{"id": 1, "email": "user@example.test"}]

            with (
                patch.object(db, "_PROJECT_ROOT", root),
                patch.object(db, "_DATA_DIR", root),
                patch.object(db, "_LOG_DIR", runtime / "logs"),
                patch.object(app_state_db, "APP_STATE_DB_PATH", runtime / "turb.sqlite3"),
            ):
                db._write_json(export, payload)
                self.assertEqual(app_state_db.get_document(export, []), payload)

            self.assertTrue(export.is_symlink())
            self.assertEqual(
                json.loads(runtime_export.read_text(encoding="utf-8")),
                payload,
            )

    def test_static_viewer_keeps_runtime_volume_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            runtime = Path(tmp) / "runtime"
            root.mkdir()
            runtime.mkdir()
            viewer, runtime_viewer = self._runtime_symlink(root, runtime, "accounts_viewer.html")

            with patch.object(db, "_VIEWER_HTML", viewer):
                rendered = db._render_static_viewer(outlook_rows=[], account_rows=[])

            self.assertEqual(rendered, viewer)
            self.assertTrue(viewer.is_symlink())
            self.assertIn("账号查看器", runtime_viewer.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
