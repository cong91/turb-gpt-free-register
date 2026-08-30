# -*- coding: utf-8 -*-
"""Offline tests for pinned wireproxy runtime installation."""
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import wireproxy_runtime as runtime


class _Response:
    status_code = 200

    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]


def _archive(
    executable: bytes = b"fake-wireproxy-executable",
    executable_name: str = "wireproxy.exe",
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        member = tarfile.TarInfo(executable_name)
        member.size = len(executable)
        bundle.addfile(member, io.BytesIO(executable))
    return output.getvalue()


class WireproxyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.install_root = Path(self.temp_dir.name) / "wireproxy"

    def test_resolve_uses_explicit_file(self):
        executable = Path(self.temp_dir.name) / "custom-wireproxy.exe"
        executable.write_bytes(b"custom")

        self.assertEqual(
            runtime.resolve_wireproxy_executable(str(executable)),
            str(executable.resolve()),
        )

    def test_resolve_uses_path_before_download(self):
        found = str(Path(self.temp_dir.name) / "wireproxy.exe")
        with mock.patch.object(runtime.shutil, "which", return_value=found), \
             mock.patch.object(runtime, "_install_pinned_release") as install:
            resolved = runtime.resolve_wireproxy_executable("wireproxy.exe")

        self.assertEqual(resolved, str(Path(found).resolve()))
        install.assert_not_called()

    def test_download_rejects_checksum_mismatch(self):
        archive = _archive()
        with mock.patch.object(runtime.requests, "get", return_value=_Response(archive)), \
             self.assertRaisesRegex(runtime.WireproxyRuntimeError, "SHA-256"):
            runtime._download_archive("https://example.test/archive", "0" * 64)

    def test_extracts_wireproxy_executable_only(self):
        executable = b"wireproxy-binary"
        self.assertEqual(runtime._extract_executable(_archive(executable)), executable)

    def test_installs_pinned_release_and_reuses_verified_manifest(self):
        archive = _archive()
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        executable = b"fake-wireproxy-executable"
        assets = {"Windows": {"amd64": ("wireproxy_windows_amd64.tar.gz", archive_sha256)}}

        with mock.patch.object(runtime, "_INSTALL_ROOT", self.install_root), \
             mock.patch.object(runtime, "_ASSETS", assets), \
             mock.patch.object(runtime.platform, "system", return_value="Windows"), \
             mock.patch.object(runtime, "_architecture", return_value="amd64"), \
             mock.patch.object(runtime.requests, "get", return_value=_Response(archive)) as get:
            first = runtime._install_pinned_release()
            second = runtime._install_pinned_release()

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), executable)
        manifest = json.loads(first.with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], runtime._VERSION)
        self.assertEqual(manifest["archive_sha256"], archive_sha256)
        self.assertEqual(manifest["executable_sha256"], hashlib.sha256(executable).hexdigest())
        get.assert_called_once()

    def test_tampered_installed_binary_is_rejected_without_overwrite(self):
        archive = _archive()
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        assets = {"Windows": {"amd64": ("wireproxy_windows_amd64.tar.gz", archive_sha256)}}
        install_dir = self.install_root / runtime._VERSION / "amd64"
        install_dir.mkdir(parents=True)
        executable = install_dir / "wireproxy.exe"
        executable.write_bytes(b"tampered")
        (install_dir / "manifest.json").write_text(
            json.dumps({
                "version": runtime._VERSION,
                "asset": "wireproxy_windows_amd64.tar.gz",
                "archive_sha256": archive_sha256,
                "executable_sha256": hashlib.sha256(b"original").hexdigest(),
            }),
            encoding="utf-8",
        )

        with mock.patch.object(runtime, "_INSTALL_ROOT", self.install_root), \
             mock.patch.object(runtime, "_ASSETS", assets), \
             mock.patch.object(runtime.platform, "system", return_value="Windows"), \
             mock.patch.object(runtime, "_architecture", return_value="amd64"), \
             mock.patch.object(runtime.requests, "get") as get, \
             self.assertRaisesRegex(runtime.WireproxyRuntimeError, "không khớp manifest"):
            runtime._install_pinned_release()

        self.assertEqual(executable.read_bytes(), b"tampered")
        get.assert_not_called()

    def test_installs_pinned_linux_release_with_native_executable_name(self):
        executable = b"linux-wireproxy-executable"
        archive = _archive(executable=executable, executable_name="wireproxy")
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        assets = {"Linux": {"amd64": ("wireproxy_linux_amd64.tar.gz", archive_sha256)}}

        with mock.patch.object(runtime, "_INSTALL_ROOT", self.install_root), \
             mock.patch.object(runtime, "_ASSETS", assets), \
             mock.patch.object(runtime.platform, "system", return_value="Linux"), \
             mock.patch.object(runtime, "_architecture", return_value="amd64"), \
             mock.patch.object(runtime.requests, "get", return_value=_Response(archive)) as get:
            installed = runtime._install_pinned_release()

        self.assertEqual(installed.name, "wireproxy")
        self.assertEqual(installed.read_bytes(), executable)
        self.assertEqual(installed.parent.name, "linux-amd64")
        get.assert_called_once()

    def test_auto_download_can_be_disabled(self):
        with mock.patch.object(runtime.shutil, "which", return_value=None), \
             mock.patch.object(runtime, "_cfg_attr", return_value=False), \
             self.assertRaisesRegex(runtime.WireproxyRuntimeError, "AUTO_DOWNLOAD=False"):
            runtime.resolve_wireproxy_executable("wireproxy.exe")


if __name__ == "__main__":
    unittest.main()
