# -*- coding: utf-8 -*-
"""Resolve or install the pinned wireproxy runtime on Windows.

The downloaded archive is pinned to a release and SHA-256 checksum. Runtime
artifacts live under ``data/tools`` (already gitignored) and are never committed.
"""
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path

import requests

_VERSION = "v1.1.3"
_RELEASE_BASE = f"https://github.com/windtf/wireproxy/releases/download/{_VERSION}"
_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
_INSTALL_ROOT = Path(__file__).resolve().parent.parent / "data" / "tools" / "wireproxy"
_ASSETS = {
    "amd64": (
        "wireproxy_windows_amd64.tar.gz",
        "bce041ea9fe0f8a3351301dcbe29cdf6a523bb25cf9c62f17ebb5699a8051d0f",
    ),
    "386": (
        "wireproxy_windows_386.tar.gz",
        "512bd0b724ef50125f00b6d7e978009caa359066ab7f777ae473f734278f326e",
    ),
}
_INSTALL_LOCK = threading.Lock()


class WireproxyRuntimeError(RuntimeError):
    """Raised when the wireproxy executable cannot be resolved safely."""


def _cfg_attr(name: str, default=None):
    from config import nordvpn_wireguard as _cfg

    return getattr(_cfg, name, default)


def _architecture() -> str:
    machine = platform.machine().strip().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "amd64"
    if machine in ("x86", "i386", "i686"):
        return "386"
    raise WireproxyRuntimeError(
        f"wireproxy tự động cài đặt chưa hỗ trợ kiến trúc Windows {machine!r}"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_archive(url: str, expected_sha256: str) -> bytes:
    try:
        with requests.get(url, timeout=(10, 120), stream=True) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise WireproxyRuntimeError(
                        f"wireproxy archive vượt giới hạn {_MAX_DOWNLOAD_BYTES} bytes"
                    )
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise WireproxyRuntimeError(f"không thể tải wireproxy: {exc}") from exc

    archive = b"".join(chunks)
    actual_sha256 = _sha256_bytes(archive)
    if actual_sha256 != expected_sha256:
        raise WireproxyRuntimeError(
            "wireproxy archive SHA-256 không khớp release đã pin "
            f"(expected={expected_sha256}, actual={actual_sha256})"
        )
    return archive


def _extract_executable(archive: bytes) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = next(
                (
                    item
                    for item in bundle.getmembers()
                    if item.isfile() and Path(item.name).name.lower() == "wireproxy.exe"
                ),
                None,
            )
            if member is None:
                raise WireproxyRuntimeError("wireproxy archive không chứa wireproxy.exe")
            if member.size <= 0 or member.size > _MAX_DOWNLOAD_BYTES:
                raise WireproxyRuntimeError(
                    f"wireproxy.exe có kích thước không hợp lệ: {member.size}"
                )
            source = bundle.extractfile(member)
            if source is None:
                raise WireproxyRuntimeError("không thể đọc wireproxy.exe từ archive")
            executable = source.read(_MAX_DOWNLOAD_BYTES + 1)
    except (tarfile.TarError, OSError) as exc:
        raise WireproxyRuntimeError(f"wireproxy archive không hợp lệ: {exc}") from exc
    if len(executable) > _MAX_DOWNLOAD_BYTES:
        raise WireproxyRuntimeError("wireproxy.exe vượt giới hạn kích thước")
    return executable


def _owned_install(install_dir: Path, asset_name: str, archive_sha256: str) -> Path | None:
    executable = install_dir / "wireproxy.exe"
    manifest_path = install_dir / "manifest.json"
    if not executable.exists() and not manifest_path.exists():
        return None
    if not executable.is_file() or not manifest_path.is_file():
        raise WireproxyRuntimeError(
            f"wireproxy runtime path tồn tại nhưng không đầy đủ: {install_dir}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WireproxyRuntimeError(
            f"wireproxy manifest không đọc được: {manifest_path}"
        ) from exc
    expected = {
        "version": _VERSION,
        "asset": asset_name,
        "archive_sha256": archive_sha256,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise WireproxyRuntimeError(
                f"wireproxy runtime hiện có không thuộc release đã pin: {install_dir}"
            )
    executable_sha256 = str(manifest.get("executable_sha256") or "")
    if not executable_sha256 or _sha256_file(executable) != executable_sha256:
        raise WireproxyRuntimeError(
            f"wireproxy.exe hiện có không khớp manifest, từ chối ghi đè: {executable}"
        )
    return executable


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _install_pinned_release() -> Path:
    if platform.system() != "Windows":
        raise WireproxyRuntimeError(
            "wireproxy tự động cài đặt hiện chỉ hỗ trợ Windows; hãy cấu hình đường dẫn binary thủ công"
        )
    architecture = _architecture()
    asset_name, archive_sha256 = _ASSETS[architecture]
    install_dir = _INSTALL_ROOT / _VERSION / architecture

    with _INSTALL_LOCK:
        existing = _owned_install(install_dir, asset_name, archive_sha256)
        if existing is not None:
            return existing

        archive = _download_archive(
            f"{_RELEASE_BASE}/{asset_name}",
            archive_sha256,
        )
        executable = _extract_executable(archive)
        executable_path = install_dir / "wireproxy.exe"
        manifest_path = install_dir / "manifest.json"
        if executable_path.exists() or manifest_path.exists():
            raise WireproxyRuntimeError(
                f"wireproxy runtime path đã tồn tại, từ chối ghi đè: {install_dir}"
            )
        _atomic_write(executable_path, executable)
        manifest = {
            "version": _VERSION,
            "asset": asset_name,
            "archive_sha256": archive_sha256,
            "executable_sha256": _sha256_bytes(executable),
            "source": f"{_RELEASE_BASE}/{asset_name}",
        }
        try:
            _atomic_write(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode("utf-8"),
            )
        except Exception:
            try:
                executable_path.unlink()
            except OSError:
                pass
            raise
        return executable_path


def resolve_wireproxy_executable(configured: str | None = None) -> str:
    """Resolve a configured executable or install the pinned Windows release."""
    value = str(
        configured
        if configured is not None
        else _cfg_attr("NORDVPN_WG_WIREPROXY_EXE", "wireproxy.exe")
    ).strip() or "wireproxy.exe"
    expanded = Path(os.path.expandvars(value)).expanduser()
    explicit_path = expanded.is_absolute() or expanded.parent != Path(".")
    if explicit_path:
        if expanded.is_file():
            return str(expanded.resolve())
        raise WireproxyRuntimeError(f"không tìm thấy wireproxy executable: {expanded}")

    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    if value.lower() not in ("wireproxy", "wireproxy.exe"):
        raise WireproxyRuntimeError(f"không tìm thấy wireproxy executable trong PATH: {value}")
    if not bool(_cfg_attr("NORDVPN_WG_AUTO_DOWNLOAD", True)):
        raise WireproxyRuntimeError(
            "wireproxy chưa được cài đặt và NORDVPN_WG_AUTO_DOWNLOAD=False"
        )
    return str(_install_pinned_release())
