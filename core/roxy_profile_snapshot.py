# -*- coding: utf-8 -*-
"""Safe full-folder snapshots for managed Roxy profiles."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class RoxyProfileSnapshotError(RuntimeError):
    """The profile folder cannot be safely snapshotted or restored."""


_EXCLUDED_NAMES = {
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "LOCK",
}
_MANIFEST_NAME = ".roxy-profile-manifest.json"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class RoxyProfileSnapshot:
    path: Path
    file_count: int
    byte_size: int
    sha256: str
    manifest: tuple[dict, ...]
    manifest_sha256: str = ""


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _safe_relative(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RoxyProfileSnapshotError("Profile path escapes source root") from exc
    if _is_link_or_junction(path) or any(part in {"", ".", ".."} for part in relative.parts):
        raise RoxyProfileSnapshotError("Symlinks and traversal paths are not supported")
    return _safe_zip_path(zipfile.ZipInfo(relative.as_posix())).as_posix()


def _is_zip_link_or_reparse(item: zipfile.ZipInfo) -> bool:
    mode = (item.external_attr >> 16) & 0o170000
    dos_attributes = item.external_attr & 0xFFFF
    return mode == 0o120000 or bool(dos_attributes & 0x0400)


def _safe_zip_path(item: zipfile.ZipInfo) -> PurePosixPath:
    name = item.filename
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.endswith("/")
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RoxyProfileSnapshotError("Snapshot contains unsafe path")
    for part in path.parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise RoxyProfileSnapshotError("Snapshot contains unsafe Windows path")
        if part.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
            raise RoxyProfileSnapshotError("Snapshot contains reserved Windows path")
    return path


def _read_zip_member_limited(
    archive: zipfile.ZipFile,
    item: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> bytes:
    if item.file_size > max_bytes:
        raise RoxyProfileSnapshotError("Snapshot manifest exceeds size limit")
    payload = bytearray()
    with archive.open(item) as source:
        while True:
            remaining = max_bytes + 1 - len(payload)
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise RoxyProfileSnapshotError("Snapshot manifest exceeds size limit")
    return bytes(payload)


def _validate_manifest_entries(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise RoxyProfileSnapshotError("Snapshot manifest entries are invalid")
    validated: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise RoxyProfileSnapshotError("Snapshot manifest entry is invalid")
        path_value = entry.get("path")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if not isinstance(path_value, str):
            raise RoxyProfileSnapshotError("Snapshot manifest path is invalid")
        path = _safe_zip_path(zipfile.ZipInfo(path_value)).as_posix()
        normalized = path.casefold()
        if normalized in seen:
            raise RoxyProfileSnapshotError("Snapshot manifest contains duplicate paths")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256.lower())
        ):
            raise RoxyProfileSnapshotError("Snapshot manifest entry is invalid")
        seen.add(normalized)
        validated.append({"path": path, "size": size, "sha256": sha256.lower()})
    return validated


def _iter_files(root: Path) -> Iterable[tuple[Path, str]]:
    if not root.is_dir() or _is_link_or_junction(root):
        raise RoxyProfileSnapshotError("Profile source must be a real directory")
    for path in sorted(root.rglob("*")):
        if _is_link_or_junction(path):
            raise RoxyProfileSnapshotError("Profile source contains a symlink")
        if path.is_file():
            relative = _safe_relative(root, path)
            if Path(relative).name in _EXCLUDED_NAMES:
                continue
            yield path, relative


def _source_fingerprint(root: Path) -> tuple[tuple[str, int, int], ...]:
    items = []
    for path, relative in _iter_files(root):
        stat = path.stat()
        items.append((relative, stat.st_size, stat.st_mtime_ns))
    return tuple(items)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(manifest: Iterable[dict]) -> str:
    digest = hashlib.sha256()
    for entry in manifest:
        digest.update(
            f"{entry['path']}\0{entry['size']}\0{entry['sha256']}\n".encode()
        )
    return digest.hexdigest()


def _manifest_entry(path: Path, relative: str) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": relative, "size": size, "sha256": digest.hexdigest()}


def create_snapshot(source: str | Path, destination: str | Path, *, max_bytes: int) -> RoxyProfileSnapshot:
    source_input = Path(source)
    if _is_link_or_junction(source_input):
        raise RoxyProfileSnapshotError("Profile source must not be a symlink")
    source_path = source_input.resolve(strict=True)
    destination_input = Path(destination)
    if destination_input.exists() and destination_input.is_symlink():
        raise RoxyProfileSnapshotError("Snapshot destination must not be a symlink")
    destination_path = destination_input.resolve()
    if destination_path == source_path or source_path in destination_path.parents:
        raise RoxyProfileSnapshotError("Snapshot destination must be outside source")
    if not (source_path / "Default" / "Preferences").is_file():
        raise RoxyProfileSnapshotError("Profile must contain Default/Preferences")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".roxy-snapshot-", dir=destination_path.parent))
    manifest: list[dict] = []
    total = 0
    source_before = _source_fingerprint(source_path)
    try:
        with zipfile.ZipFile(temporary / "profile.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path, relative in _iter_files(source_path):
                entry = _manifest_entry(path, relative)
                total += entry["size"]
                if total > max_bytes:
                    raise RoxyProfileSnapshotError("Profile snapshot exceeds configured size limit")
                archive.write(path, relative)
                manifest.append(entry)
            manifest_payload = {
                "file_count": len(manifest),
                "manifest_sha256": _manifest_digest(manifest),
                "entries": manifest,
            }
            archive.writestr(
                _MANIFEST_NAME,
                json.dumps(
                    manifest_payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
            )
        if _source_fingerprint(source_path) != source_before:
            raise RoxyProfileSnapshotError(
                "Profile source changed while the snapshot was being captured"
            )
        package = temporary / "profile.zip"
        final = destination_path
        os.replace(package, final)
        snapshot = RoxyProfileSnapshot(
            path=final,
            file_count=len(manifest),
            byte_size=final.stat().st_size,
            sha256=_file_sha256(final),
            manifest=tuple(manifest),
            manifest_sha256=_manifest_digest(manifest),
        )
        shutil.rmtree(temporary, ignore_errors=True)
        return snapshot
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            destination_path.unlink()
        except FileNotFoundError:
            pass
        raise


def extract_snapshot(archive_path: str | Path, destination: str | Path, *, max_bytes: int) -> RoxyProfileSnapshot:
    archive_input = Path(archive_path)
    if archive_input.is_symlink():
        raise RoxyProfileSnapshotError("Snapshot must not be a symlink")
    archive_path = archive_input.resolve(strict=True)
    destination_input = Path(destination)
    if destination_input.exists() and destination_input.is_symlink():
        raise RoxyProfileSnapshotError("Extraction destination must not be a symlink")
    destination = destination_input.resolve()
    if destination.exists():
        raise RoxyProfileSnapshotError("Extraction destination already exists")
    if not archive_path.is_file() or archive_path.is_symlink():
        raise RoxyProfileSnapshotError("Snapshot must be a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".roxy-restore-", dir=destination.parent))
    manifest: list[dict] = []
    total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest_item = None
            seen_members: set[str] = set()
            for item in archive.infolist():
                path = _safe_zip_path(item)
                normalized = path.as_posix().casefold()
                if normalized in seen_members:
                    raise RoxyProfileSnapshotError("Snapshot contains duplicate paths")
                seen_members.add(normalized)
                if _is_zip_link_or_reparse(item):
                    raise RoxyProfileSnapshotError("Snapshot contains a link or reparse point")
                if path.as_posix() == _MANIFEST_NAME:
                    manifest_item = item
                    continue
                if path.name in _EXCLUDED_NAMES:
                    raise RoxyProfileSnapshotError("Snapshot contains excluded lock data")
                target = (temporary / Path(*path.parts)).resolve()
                if temporary.resolve() not in target.parents:
                    raise RoxyProfileSnapshotError("Snapshot path escapes destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                digest = hashlib.sha256()
                with archive.open(item) as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if total + size > max_bytes:
                            raise RoxyProfileSnapshotError(
                                "Restored profile exceeds configured size limit"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                entry = {
                    "path": path.as_posix(),
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
                total += size
                manifest.append(entry)
            if manifest_item is None:
                raise RoxyProfileSnapshotError("Snapshot manifest is missing")
            try:
                payload = _read_zip_member_limited(
                    archive,
                    manifest_item,
                    max_bytes=min(_MAX_MANIFEST_BYTES, max_bytes),
                )
                declared = json.loads(payload.decode("utf-8"))
                if not isinstance(declared, dict) or set(declared) != {
                    "file_count", "manifest_sha256", "entries",
                }:
                    raise RoxyProfileSnapshotError("Snapshot manifest is invalid")
                entries = _validate_manifest_entries(declared["entries"])
                if (
                    isinstance(declared["file_count"], bool)
                    or declared["file_count"] != len(entries)
                    or declared["manifest_sha256"] != _manifest_digest(entries)
                    or entries != manifest
                ):
                    raise RoxyProfileSnapshotError("Snapshot manifest mismatch")
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                raise RoxyProfileSnapshotError("Snapshot manifest is invalid") from exc
        if not (temporary / "Default" / "Preferences").is_file():
            raise RoxyProfileSnapshotError("Snapshot lacks Default/Preferences")
        os.replace(temporary, destination)
        return RoxyProfileSnapshot(
            path=destination,
            file_count=len(manifest),
            byte_size=sum(item["size"] for item in manifest),
            sha256=_file_sha256(archive_path),
            manifest=tuple(manifest),
            manifest_sha256=_manifest_digest(manifest),
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
