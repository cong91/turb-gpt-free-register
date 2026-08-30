# -*- coding: utf-8 -*-
import hashlib
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from core.roxy_profile_snapshot import (
    RoxyProfileSnapshotError,
    create_snapshot,
    extract_snapshot,
)


class RoxyProfileSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source" / "Default").mkdir(parents=True)
        (self.root / "source" / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        (self.root / "source" / "Local Storage").mkdir()
        (self.root / "source" / "Local Storage" / "marker").write_text("state", encoding="utf-8")
        (self.root / "source" / "Default" / "IndexedDB").mkdir()
        (self.root / "source" / "Default" / "IndexedDB" / "sample.leveldb").write_bytes(b"indexed-db")
        extension = self.root / "source" / "Default" / "Extensions" / "test-extension" / "1.0"
        extension.mkdir(parents=True)
        (extension / "manifest.json").write_text('{"name":"test"}', encoding="utf-8")

    @staticmethod
    def _manifest(entries):
        digest = hashlib.sha256()
        for entry in entries:
            digest.update(
                f"{entry['path']}\0{entry['size']}\0{entry['sha256']}\n".encode()
            )
        return json.dumps({
            "file_count": len(entries),
            "manifest_sha256": digest.hexdigest(),
            "entries": entries,
        })

    def _write_archive(self, members, *, manifest=None):
        archive = self.root / f"fixture-{len(list(self.root.glob('fixture-*.zip')))}.zip"
        entries = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                for name, payload, info in members:
                    if info is None:
                        output.writestr(name, payload)
                    else:
                        output.writestr(info, payload)
                    if name != ".roxy-profile-manifest.json":
                        entries.append({
                            "path": name,
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        })
                output.writestr(
                    ".roxy-profile-manifest.json",
                    manifest if manifest is not None else self._manifest(entries),
                )
        return archive

    def test_snapshot_round_trip(self):
        artifact = create_snapshot(self.root / "source", self.root / "profile.zip", max_bytes=1024 * 1024)
        restored = extract_snapshot(artifact.path, self.root / "restored", max_bytes=1024 * 1024)
        self.assertEqual(restored.file_count, artifact.file_count)
        self.assertEqual((self.root / "restored" / "Default" / "Preferences").read_text(), "{}")
        self.assertEqual((self.root / "restored" / "Local Storage" / "marker").read_text(), "state")
        self.assertEqual(
            (self.root / "restored" / "Default" / "IndexedDB" / "sample.leveldb").read_bytes(),
            b"indexed-db",
        )
        self.assertTrue(
            (self.root / "restored" / "Default" / "Extensions" / "test-extension" / "1.0" / "manifest.json").is_file()
        )

    def test_malicious_zip_paths_are_rejected(self):
        for index, name in enumerate((
            "../escape", "/absolute", "C:/drive", "Default\\Preferences",
            "Default/file:stream", "Default/CON", "Default/trailing. ",
        )):
            with self.subTest(name=name):
                archive = self._write_archive([
                    ("Default/Preferences", b"{}", None),
                    (name, b"bad", None),
                ])
                with self.assertRaises(RoxyProfileSnapshotError):
                    extract_snapshot(
                        archive,
                        self.root / f"unsafe-{index}",
                        max_bytes=1024 * 1024,
                    )

    def test_zip_symlink_and_reparse_entries_are_rejected(self):
        for index, attributes in enumerate(((0o120777 << 16), 0x0400)):
            with self.subTest(attributes=attributes):
                info = zipfile.ZipInfo("Default/link")
                info.external_attr = attributes
                archive = self._write_archive([
                    ("Default/Preferences", b"{}", None),
                    ("Default/link", b"target", info),
                ])
                with self.assertRaisesRegex(RoxyProfileSnapshotError, "link|reparse"):
                    extract_snapshot(
                        archive,
                        self.root / f"link-{index}",
                        max_bytes=1024 * 1024,
                    )

    def test_duplicate_members_and_invalid_manifest_are_rejected(self):
        duplicate = self._write_archive([
            ("Default/Preferences", b"{}", None),
            ("Default/Preferences", b"changed", None),
        ])
        with self.assertRaisesRegex(RoxyProfileSnapshotError, "duplicate"):
            extract_snapshot(duplicate, self.root / "duplicate", max_bytes=1024 * 1024)

        malformed = self._write_archive(
            [("Default/Preferences", b"{}", None)],
            manifest=json.dumps({"file_count": 1, "manifest_sha256": "bad", "entries": "bad"}),
        )
        with self.assertRaisesRegex(RoxyProfileSnapshotError, "manifest"):
            extract_snapshot(malformed, self.root / "malformed", max_bytes=1024 * 1024)

    def test_manifest_is_bounded_independently(self):
        archive = self._write_archive(
            [("Default/Preferences", b"{}", None)],
            manifest=b" " * 2048,
        )
        with self.assertRaisesRegex(RoxyProfileSnapshotError, "manifest exceeds size limit"):
            extract_snapshot(archive, self.root / "manifest-large", max_bytes=1024)

    def test_source_change_during_capture_prevents_publication(self):
        before = (("Default/Preferences", 2, 1),)
        after = (("Default/Preferences", 3, 2),)
        with patch(
            "core.roxy_profile_snapshot._source_fingerprint",
            side_effect=[before, after],
        ):
            with self.assertRaisesRegex(RoxyProfileSnapshotError, "source changed"):
                create_snapshot(
                    self.root / "source",
                    self.root / "changed.zip",
                    max_bytes=1024 * 1024,
                )
        self.assertFalse((self.root / "changed.zip").exists())

    def test_symlink_is_rejected(self):
        link = self.root / "source" / "bad-link"
        try:
            link.symlink_to(self.root / "source" / "Default", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(RoxyProfileSnapshotError):
            create_snapshot(self.root / "source", self.root / "bad.zip", max_bytes=1024 * 1024)

    def test_zip_bomb_is_bounded_while_streaming(self):
        archive = self.root / "large.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("Default/Preferences", "{}")
            output.writestr("large.bin", b"x" * 4096)
            entries = [
                {"path": "Default/Preferences", "size": 2, "sha256": "placeholder"},
                {"path": "large.bin", "size": 4096, "sha256": "placeholder"},
            ]
            output.writestr(".roxy-profile-manifest.json", __import__("json").dumps({"file_count": 2, "manifest_sha256": "placeholder", "entries": entries}))
        with self.assertRaisesRegex(RoxyProfileSnapshotError, "size limit"):
            extract_snapshot(archive, self.root / "large-restored", max_bytes=128)
        self.assertFalse((self.root / "large-restored").exists())

        with self.assertRaises(RoxyProfileSnapshotError):
            create_snapshot(self.root / "source", self.root / "bad.zip", max_bytes=1)


if __name__ == "__main__":
    unittest.main()
