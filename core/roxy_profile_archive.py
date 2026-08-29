# -*- coding: utf-8 -*-
"""Encrypted metadata archives for managed Roxy profiles."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.roxy_profile_snapshot import create_snapshot, extract_snapshot

ARCHIVE_FORMAT = "roxy-profile-archive-v1"
FOLDER_ARCHIVE_FORMAT = "roxy-profile-folder-v2"
_AAD = ARCHIVE_FORMAT.encode("ascii")
_FOLDER_MAGIC = b"RPA2"
_FOLDER_AAD = FOLDER_ARCHIVE_FORMAT.encode("ascii")

ARCHIVE_CAPABILITIES = {
    "profile_metadata": True,
    "cookies_if_returned_by_roxy_detail": True,
    "proxy_if_returned_by_roxy_detail": True,
    "fingerprint_if_returned_by_roxy_detail": True,
    "browser_cache": False,
    "extension_binaries": False,
    "indexed_db_complete": False,
    "local_storage_complete": False,
    "portable_password_database": False,
    "detached_roxy_offline_open": False,
    "restore_in_roxy_required_before_open": True,
}
FOLDER_ARCHIVE_CAPABILITIES = {
    "profile_metadata": True,
    "browser_cache": True,
    "extension_binaries": True,
    "indexed_db_complete": True,
    "local_storage_complete": True,
    "portable_password_database": False,
    "detached_roxy_offline_open": True,
    "identity_mode": "browser_state_only",
    "fingerprint_equivalent": False,
}


class RoxyProfileArchiveError(RuntimeError):
    """The archive could not be safely created or verified."""


@dataclass(frozen=True)
class RoxyProfileArchiveArtifact:
    archive_id: str
    path: Path
    byte_size: int
    sha256: str
    verified_at: str
    capabilities: dict[str, Any]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_archive_key(value: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise RoxyProfileArchiveError(
            "ROXY_PROFILE_ARCHIVE_KEY is required for encrypted export"
        )
    try:
        padded = raw + "=" * (-len(raw) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RoxyProfileArchiveError(
            "ROXY_PROFILE_ARCHIVE_KEY must be URL-safe base64"
        ) from exc
    if len(key) != 32:
        raise RoxyProfileArchiveError(
            "ROXY_PROFILE_ARCHIVE_KEY must decode to exactly 32 bytes"
        )
    return key


class RoxyProfileArchiveStore:
    def __init__(
        self,
        directory: str | Path,
        key: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
    ):
        self.directory = Path(directory)
        self.key = decode_archive_key(key)
        self.max_bytes = max(1024, int(max_bytes))

    @staticmethod
    def _hash_identifier(value: str) -> str:
        return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def create(
        self,
        *,
        local_id: str,
        workspace_id: str,
        dir_id: str,
        profile_metadata: dict[str, Any],
        roxy_version: str = "",
    ) -> RoxyProfileArchiveArtifact:
        if not isinstance(profile_metadata, dict):
            raise RoxyProfileArchiveError("Profile metadata must be an object")
        archive_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "format": ARCHIVE_FORMAT,
            "archive_id": archive_id,
            "local_id": str(local_id),
            "created_at": created_at,
            "source": {
                "workspace_hash": self._hash_identifier(workspace_id),
                "dir_id_hash": self._hash_identifier(dir_id),
                "roxy_version": str(roxy_version or ""),
            },
            "capabilities": dict(ARCHIVE_CAPABILITIES),
            "profile": profile_metadata,
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(plaintext) > self.max_bytes:
            raise RoxyProfileArchiveError("Profile archive exceeds configured size limit")
        plaintext_sha256 = hashlib.sha256(plaintext).hexdigest()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(nonce, plaintext, _AAD)
        envelope = {
            "format": ARCHIVE_FORMAT,
            "archive_id": archive_id,
            "cipher": "AES-256-GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            "plaintext_sha256": plaintext_sha256,
        }
        encoded = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise RoxyProfileArchiveError("Encrypted archive exceeds configured size limit")

        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise RoxyProfileArchiveError("Archive directory must not be a symlink")
        destination = self.directory / f"{archive_id}.rpa"
        temporary = self.directory / f".{archive_id}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            verified = self.read(destination)
            if verified.get("archive_id") != archive_id:
                raise RoxyProfileArchiveError("Archive identity verification failed")
        except Exception:
            for candidate in (temporary, destination):
                try:
                    candidate.unlink()
                except OSError:
                    pass
            raise

        verified_at = datetime.now(timezone.utc).isoformat()
        return RoxyProfileArchiveArtifact(
            archive_id=archive_id,
            path=destination,
            byte_size=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
            verified_at=verified_at,
            capabilities=dict(ARCHIVE_CAPABILITIES),
        )

    def create_folder(
        self,
        *,
        local_id: str,
        workspace_id: str,
        dir_id: str,
        profile_directory: str | Path,
        core_version: str = "",
        profile_metadata: dict[str, Any] | None = None,
        official_signature_sha256: str = "",
    ) -> RoxyProfileArchiveArtifact:
        archive_id = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise RoxyProfileArchiveError("Archive directory must not be a symlink")
        zip_path = self.directory / f".{archive_id}.zip"
        temporary = self.directory / f".{archive_id}.tmp"
        destination = self.directory / f"{archive_id}.rpa2"
        published = False
        try:
            snapshot = create_snapshot(
                profile_directory,
                zip_path,
                max_bytes=self.max_bytes,
            )
            metadata_sha256 = hashlib.sha256(
                json.dumps(
                    profile_metadata or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            header = {
                "format": FOLDER_ARCHIVE_FORMAT,
                "archive_id": archive_id,
                "local_id": str(local_id),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": {
                    "workspace_hash": self._hash_identifier(workspace_id),
                    "dir_id_hash": self._hash_identifier(dir_id),
                    "core_version": str(core_version or ""),
                },
                "plaintext_sha256": snapshot.sha256,
                "file_count": snapshot.file_count,
                "manifest_sha256": snapshot.manifest_sha256,
                "profile_metadata_sha256": metadata_sha256,
                "official_signature_sha256": str(official_signature_sha256 or ""),
                "capabilities": dict(FOLDER_ARCHIVE_CAPABILITIES),
            }
            encoded_header = json.dumps(
                header, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            nonce = os.urandom(12)
            encryptor = Cipher(
                algorithms.AES(self.key), modes.GCM(nonce)
            ).encryptor()
            encryptor.authenticate_additional_data(_FOLDER_AAD + encoded_header)
            with temporary.open("xb") as output, zip_path.open("rb") as source:
                output.write(_FOLDER_MAGIC)
                output.write(struct.pack(">I", len(encoded_header)))
                output.write(encoded_header)
                output.write(nonce)
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(encryptor.update(chunk))
                output.write(encryptor.finalize())
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            verified = self.read_folder_header(destination)
            if verified.get("archive_id") != archive_id:
                raise RoxyProfileArchiveError("Folder archive identity verification failed")
            encoded_sha256 = _file_sha256(destination)
            published = True
            return RoxyProfileArchiveArtifact(
                archive_id=archive_id,
                path=destination,
                byte_size=destination.stat().st_size,
                sha256=encoded_sha256,
                verified_at=datetime.now(timezone.utc).isoformat(),
                capabilities=dict(FOLDER_ARCHIVE_CAPABILITIES),
            )
        except RoxyProfileArchiveError:
            raise
        except Exception as exc:
            raise RoxyProfileArchiveError("Unable to create folder archive") from exc
        finally:
            for candidate in (zip_path, temporary):
                try:
                    candidate.unlink()
                except OSError:
                    pass
            if destination.exists() and not published:
                destination.unlink(missing_ok=True)

    def import_folder(
        self,
        source: str | Path,
        *,
        expected_local_id: str | None = None,
    ) -> RoxyProfileArchiveArtifact:
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise RoxyProfileArchiveError("Imported folder archive must be a regular file")
        if source_path.stat().st_size > self.max_bytes:
            raise RoxyProfileArchiveError("Imported folder archive exceeds size limit")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise RoxyProfileArchiveError("Archive directory must not be a symlink")
        temporary = self.directory / f".import-{uuid.uuid4().hex}.tmp"
        destination = None
        try:
            with source_path.open("rb") as source_handle, temporary.open("xb") as output:
                shutil.copyfileobj(source_handle, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            header = self.read_folder_header(temporary)
            if expected_local_id and str(header.get("local_id")) != str(expected_local_id):
                raise RoxyProfileArchiveError("Imported archive belongs to another local profile")
            archive_id = str(header.get("archive_id") or "")
            if not archive_id or any(char not in "0123456789abcdef" for char in archive_id.lower()):
                raise RoxyProfileArchiveError("Imported archive identity is invalid")
            destination = self.directory / f"{archive_id}.rpa2"
            if destination.exists():
                raise RoxyProfileArchiveError("Archive identity already exists")
            os.replace(temporary, destination)
            verified = self.verify_folder(destination)
            return RoxyProfileArchiveArtifact(
                archive_id=archive_id,
                path=destination,
                byte_size=destination.stat().st_size,
                sha256=_file_sha256(destination),
                verified_at=datetime.now(timezone.utc).isoformat(),
                capabilities=dict(verified.get("capabilities") or FOLDER_ARCHIVE_CAPABILITIES),
            )
        except RoxyProfileArchiveError:
            raise
        except Exception as exc:
            raise RoxyProfileArchiveError("Unable to import folder archive") from exc
        finally:
            temporary.unlink(missing_ok=True)
            if destination is not None and destination.exists():
                try:
                    self.verify_folder(destination)
                except RoxyProfileArchiveError:
                    destination.unlink(missing_ok=True)

    def read_folder_header(self, path: str | Path) -> dict[str, Any]:
        archive_path = Path(path)
        try:
            resolved_directory = self.directory.resolve(strict=True)
            resolved_path = archive_path.resolve(strict=True)
        except OSError as exc:
            raise RoxyProfileArchiveError("Folder archive does not exist") from exc
        if resolved_directory not in resolved_path.parents:
            raise RoxyProfileArchiveError("Folder archive escapes configured directory")
        if archive_path.is_symlink() or not archive_path.is_file():
            raise RoxyProfileArchiveError("Folder archive must be a regular file")
        with archive_path.open("rb") as handle:
            if handle.read(4) != _FOLDER_MAGIC:
                raise RoxyProfileArchiveError("Unsupported folder archive format")
            raw_length = handle.read(4)
            if len(raw_length) != 4:
                raise RoxyProfileArchiveError("Folder archive header is truncated")
            header_length = struct.unpack(">I", raw_length)[0]
            if header_length < 2 or header_length > 64 * 1024:
                raise RoxyProfileArchiveError("Folder archive header is invalid")
            try:
                header = json.loads(handle.read(header_length).decode("utf-8"))
            except Exception as exc:
                raise RoxyProfileArchiveError("Folder archive header is invalid") from exc
        if header.get("format") != FOLDER_ARCHIVE_FORMAT:
            raise RoxyProfileArchiveError("Unsupported folder archive format")
        return header

    def extract_folder(self, path: str | Path, destination: str | Path) -> dict[str, Any]:
        archive_path = Path(path)
        header = self.read_folder_header(archive_path)
        temporary_zip = self.directory / f".{header['archive_id']}.{uuid.uuid4().hex}.restore.zip"
        try:
            with archive_path.open("rb") as source:
                source.read(4)
                header_length = struct.unpack(">I", source.read(4))[0]
                encoded_header = source.read(header_length)
                nonce = source.read(12)
                ciphertext_size = archive_path.stat().st_size - 4 - 4 - header_length - 12 - 16
                if ciphertext_size < 0:
                    raise RoxyProfileArchiveError("Folder archive is truncated")
                source.seek(-16, os.SEEK_END)
                tag = source.read(16)
                source.seek(4 + 4 + header_length + 12)
                decryptor = Cipher(
                    algorithms.AES(self.key), modes.GCM(nonce, tag)
                ).decryptor()
                decryptor.authenticate_additional_data(_FOLDER_AAD + encoded_header)
                remaining = ciphertext_size
                with temporary_zip.open("xb") as output:
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RoxyProfileArchiveError("Folder archive is truncated")
                        remaining -= len(chunk)
                        output.write(decryptor.update(chunk))
                    output.write(decryptor.finalize())
                    output.flush()
                    os.fsync(output.fileno())
            actual = _file_sha256(temporary_zip)
            if actual != header.get("plaintext_sha256"):
                raise RoxyProfileArchiveError("Folder archive plaintext checksum mismatch")
            snapshot = extract_snapshot(
                temporary_zip,
                destination,
                max_bytes=self.max_bytes,
            )
            if (
                snapshot.file_count != int(header.get("file_count", -1))
                or snapshot.manifest_sha256 != str(header.get("manifest_sha256") or "")
            ):
                raise RoxyProfileArchiveError("Folder archive manifest mismatch")
            return {"header": header, "snapshot": snapshot}
        except RoxyProfileArchiveError:
            raise
        except Exception as exc:
            raise RoxyProfileArchiveError("Folder archive authentication failed") from exc
        finally:
            try:
                temporary_zip.unlink()
            except OSError:
                pass

    def read(self, path: str | Path) -> dict[str, Any]:
        archive_path = Path(path)
        try:
            resolved_directory = self.directory.resolve(strict=True)
            resolved_path = archive_path.resolve(strict=True)
        except OSError as exc:
            raise RoxyProfileArchiveError("Archive file does not exist") from exc
        if resolved_directory not in resolved_path.parents:
            raise RoxyProfileArchiveError("Archive path escapes configured directory")
        if archive_path.is_symlink() or not archive_path.is_file():
            raise RoxyProfileArchiveError("Archive path must be a regular file")
        encoded = archive_path.read_bytes()
        if len(encoded) > self.max_bytes:
            raise RoxyProfileArchiveError("Archive exceeds configured size limit")
        try:
            envelope = json.loads(encoded.decode("utf-8"))
            if envelope.get("format") != ARCHIVE_FORMAT:
                raise RoxyProfileArchiveError("Unsupported archive format")
            nonce = base64.urlsafe_b64decode(envelope["nonce"])
            ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
            plaintext = AESGCM(self.key).decrypt(nonce, ciphertext, _AAD)
            if hashlib.sha256(plaintext).hexdigest() != envelope["plaintext_sha256"]:
                raise RoxyProfileArchiveError("Archive plaintext checksum mismatch")
            payload = json.loads(plaintext.decode("utf-8"))
        except RoxyProfileArchiveError:
            raise
        except Exception as exc:
            raise RoxyProfileArchiveError("Archive authentication failed") from exc
        if payload.get("format") != ARCHIVE_FORMAT:
            raise RoxyProfileArchiveError("Archive payload format mismatch")
        return payload

    def verify_folder(self, path: str | Path, sha256: str | None = None) -> dict[str, Any]:
        archive_path = Path(path)
        if sha256 is not None:
            actual = _file_sha256(archive_path)
            if actual != str(sha256):
                raise RoxyProfileArchiveError("Folder archive file checksum mismatch")
        self.read_folder_header(archive_path)
        with tempfile.TemporaryDirectory(prefix=".roxy-verify-", dir=self.directory) as temp_dir:
            result = self.extract_folder(archive_path, Path(temp_dir) / "profile")
        return result["header"]

    def verify(self, path: str | Path, sha256: str) -> dict[str, Any]:
        archive_path = Path(path)
        actual = _file_sha256(archive_path)
        if actual != str(sha256):
            raise RoxyProfileArchiveError("Archive file checksum mismatch")
        return self.read(archive_path)
