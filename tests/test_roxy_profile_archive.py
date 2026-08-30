# -*- coding: utf-8 -*-
import base64
import tempfile
import unittest
from pathlib import Path

from core.roxy_profile_archive import (
    ARCHIVE_FORMAT,
    RoxyProfileArchiveError,
    RoxyProfileArchiveStore,
)


class RoxyProfileArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        self.store = RoxyProfileArchiveStore(
            Path(self.temp_dir.name) / "archives",
            key,
        )

    def test_encrypted_archive_round_trip_declares_offline_limit(self):
        artifact = self.store.create(
            local_id="local-one",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_metadata={"name": "Profile", "remark": "safe metadata"},
        )

        self.assertTrue(artifact.path.exists())
        self.assertEqual(self.store.read(artifact.path)["format"], ARCHIVE_FORMAT)
        payload = self.store.read(artifact.path)
        self.assertFalse(payload["capabilities"]["detached_roxy_offline_open"])
        self.assertTrue(payload["capabilities"]["restore_in_roxy_required_before_open"])
        self.assertNotIn("workspace-one", artifact.path.read_text(encoding="utf-8"))

    def test_wrong_key_and_corruption_fail_closed(self):
        artifact = self.store.create(
            local_id="local-one",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_metadata={"name": "Profile"},
        )
        wrong = RoxyProfileArchiveStore(
            self.store.directory,
            base64.urlsafe_b64encode(b"x" * 32).decode("ascii"),
        )
        with self.assertRaises(RoxyProfileArchiveError):
            wrong.read(artifact.path)

        original = artifact.path.read_bytes()
        artifact.path.write_bytes(original[:-2] + b"xx")
        with self.assertRaises(RoxyProfileArchiveError):
            self.store.read(artifact.path)

    def test_checksum_and_path_traversal_are_rejected(self):
        artifact = self.store.create(
            local_id="local-one",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_metadata={"name": "Profile"},
        )
        with self.assertRaises(RoxyProfileArchiveError):
            self.store.verify(artifact.path, "0" * 64)
        with self.assertRaises(RoxyProfileArchiveError):
            self.store.read(Path(self.temp_dir.name) / "outside.rpa")

    def test_folder_archive_round_trip_is_encrypted(self):
        source = Path(self.temp_dir.name) / "source-profile"
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        (source / "Local Storage").mkdir()
        (source / "Local Storage" / "marker").write_text("secret-state", encoding="utf-8")

        artifact = self.store.create_folder(
            local_id="local-one",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_directory=source,
            core_version="150",
        )

        self.assertTrue(artifact.path.exists())
        self.assertNotIn(b"secret-state", artifact.path.read_bytes())
        restored = Path(self.temp_dir.name) / "restored-profile"
        result = self.store.extract_folder(artifact.path, restored)
        self.assertEqual(result["header"]["source"]["core_version"], "150")
        self.assertTrue(result["header"]["profile_metadata_sha256"])
        self.assertIn("official_signature_sha256", result["header"])
        self.assertEqual(
            (restored / "Local Storage" / "marker").read_text(encoding="utf-8"),
            "secret-state",
        )

    def test_folder_archive_import_copies_and_verifies_artifact(self):
        source = Path(self.temp_dir.name) / "source-profile"
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        artifact = self.store.create_folder(
            local_id="local-import",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_directory=source,
            core_version="150",
        )
        uploaded = Path(self.temp_dir.name) / "uploaded.rpa2"
        uploaded.write_bytes(artifact.path.read_bytes())
        target = RoxyProfileArchiveStore(
            Path(self.temp_dir.name) / "imported-archives",
            base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        )
        imported = target.import_folder(uploaded, expected_local_id="local-import")
        self.assertEqual(imported.archive_id, artifact.archive_id)
        self.assertNotEqual(imported.path, uploaded)
        self.assertTrue(imported.path.is_file())

    def test_folder_archive_tampering_fails_closed(self):
        source = Path(self.temp_dir.name) / "tampered-source-profile"
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        artifact = self.store.create_folder(
            local_id="local-one",
            workspace_id="workspace-one",
            dir_id="dir-one",
            profile_directory=source,
        )
        encoded = bytearray(artifact.path.read_bytes())
        encoded[-20] ^= 1
        artifact.path.write_bytes(encoded)

        with self.assertRaises(RoxyProfileArchiveError):
            self.store.extract_folder(
                artifact.path,
                Path(self.temp_dir.name) / "tampered-restored",
            )

    def test_missing_key_fails_closed(self):
        with self.assertRaises(RoxyProfileArchiveError):
            RoxyProfileArchiveStore(Path(self.temp_dir.name), "")


if __name__ == "__main__":
    unittest.main()
