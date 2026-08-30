# -*- coding: utf-8 -*-
"""Use-case orchestration for the independent Roxy profile manager."""
from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from config import roxy_profile_manager as _manager_cfg
from config import roxybrowser as _roxy_cfg
from core.app_state_db import APP_STATE_DB_PATH
from core.roxy_profile_archive import (
    ARCHIVE_FORMAT,
    FOLDER_ARCHIVE_FORMAT,
    RoxyProfileArchiveError,
    RoxyProfileArchiveStore,
)
from core.roxy_profile_launcher import (
    RoxyLocalLaunch,
    RoxyProfileLauncherError,
    capture_signature_from_debugger,
    launch_offline,
    stop_offline,
)
from core.roxy_profile_manager_client import (
    RoxyProfileManagerClient,
    RoxyProfileManagerClientError,
)
from core.roxy_profile_store import (
    ManagedRoxyProfile,
    RoxyProfileConflict,
    RoxyProfileStore,
)


class RoxyProfileManagerError(RuntimeError):
    """Base profile-manager use-case error."""


class RoxyProfileManagerNotFound(RoxyProfileManagerError):
    """A managed profile or archive was not found."""


class RoxyProfileManagerStateError(RoxyProfileManagerError):
    """The operation is invalid for the current profile state."""


class RoxyProfileManagerUpstreamError(RoxyProfileManagerError):
    """RoxyBrowser rejected or could not complete an operation."""


def _store() -> RoxyProfileStore:
    return RoxyProfileStore(APP_STATE_DB_PATH)


def _archive_store(*, full_folder: bool = False) -> RoxyProfileArchiveStore:
    return RoxyProfileArchiveStore(
        _manager_cfg.ROXY_PROFILE_ARCHIVE_DIR,
        _manager_cfg.ROXY_PROFILE_ARCHIVE_KEY,
        max_bytes=(
            _manager_cfg.ROXY_PROFILE_FULL_ARCHIVE_MAX_BYTES
            if full_folder
            else _manager_cfg.ROXY_PROFILE_ARCHIVE_MAX_BYTES
        ),
    )


def _masked_dir_id(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 10:
        return text
    return f"{text[:6]}…{text[-4:]}"


class RoxyProfileManager:
    def __init__(
        self,
        *,
        store: RoxyProfileStore | None = None,
        client: RoxyProfileManagerClient | None = None,
        archive_store: RoxyProfileArchiveStore | None = None,
        snapshot_provider=None,
        signature_probe=None,
        offline_launcher=None,
        offline_stopper=None,
    ):
        self.store = store or _store()
        self.client = client or RoxyProfileManagerClient()
        self.archive_store = archive_store
        self.snapshot_provider = snapshot_provider
        self.signature_probe = signature_probe or capture_signature_from_debugger
        self.offline_launcher = offline_launcher or launch_offline
        self.offline_stopper = offline_stopper or stop_offline

    @property
    def enabled(self) -> bool:
        return bool(_manager_cfg.ROXY_PROFILE_MANAGER_ENABLED)

    def status(self) -> dict[str, Any]:
        profiles = self.store.list_profiles()
        remote_error = ""
        remote_profiles: list[dict[str, Any]] = []
        try:
            remote_profiles = self.client.list_profiles()
        except Exception as exc:
            remote_error = self._safe_error(exc)
        return {
            "enabled": self.enabled,
            "workspace_configured": bool(str(_roxy_cfg.ROXY_WORKSPACE_ID or "").strip()),
            "api_token_configured": bool(str(_roxy_cfg.ROXY_API_TOKEN or "").strip()),
            "archive_key_configured": bool(
                str(_manager_cfg.ROXY_PROFILE_ARCHIVE_KEY or "").strip()
            ),
            "offline_open_supported": bool(
                _manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED
            ),
            "offline_identity_mode": "browser_state_only",
            "restore_required_before_open": not bool(
                _manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED
            ),
            "managed_count": len(profiles),
            "active_remote_count": len(remote_profiles),
            "remote_error": remote_error,
        }

    def _filtered_profiles(
        self,
        *,
        search: str = "",
        state: str = "",
    ) -> list[ManagedRoxyProfile]:
        normalized_search = str(search or "").strip().lower()
        normalized_state = str(state or "").strip().upper()
        profiles = self.store.list_profiles()
        if normalized_search:
            profiles = [
                profile for profile in profiles
                if normalized_search in profile.display_name.lower()
                or normalized_search in profile.local_id.lower()
                or normalized_search in str(profile.dir_id or "").lower()
            ]
        if normalized_state:
            profiles = [profile for profile in profiles if profile.state == normalized_state]
        return profiles

    def count_profiles(self, *, search: str = "", state: str = "") -> int:
        return len(self._filtered_profiles(search=search, state=state))

    def list_profiles(
        self,
        *,
        reconcile: bool = False,
        search: str = "",
        state: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        if reconcile:
            self.reconcile_profiles()
        profiles = self._filtered_profiles(search=search, state=state)
        start = (max(1, int(page)) - 1) * max(1, min(int(page_size), 200))
        end = start + max(1, min(int(page_size), 200))
        return [self._public_profile(item) for item in profiles[start:end]]

    def get_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        result = self._public_profile(profile)
        if profile.dir_id and profile.state in {"ACTIVE_STOPPED", "RUNNING"}:
            try:
                remote = self.client.get_profile(profile.dir_id)
                self.client.require_owned(remote, profile.owner_marker)
                result["remote_config"] = {
                    key: remote[key]
                    for key in ("name", "os", "osVersion", "coreType", "coreVersion", "projectId")
                    if key in remote
                }
            except RoxyProfileManagerClientError as exc:
                result["remote_config_error"] = self._safe_error(exc)
        return result

    def bulk_action(self, local_ids: list[str], action: str) -> dict[str, Any]:
        identifiers = list(dict.fromkeys(str(item or "").strip() for item in local_ids))
        identifiers = [item for item in identifiers if item]
        if not identifiers or len(identifiers) > 100:
            raise RoxyProfileManagerError("Bulk action requires 1-100 profile identities")
        handlers = {
            "remote_open": self.open_managed_profile,
            "remote_close": self.close_managed_profile,
            "metadata_export": self.export_managed_profile,
            "full_export": self.export_full_profile,
            "archive": self.archive_managed_profile,
            "local_close": self.close_offline_profile,
        }
        handler = handlers.get(str(action or "").strip().lower())
        if handler is None:
            raise RoxyProfileManagerError("Unsupported bulk profile action")
        results = []
        for local_id in identifiers:
            try:
                results.append({"local_id": local_id, "ok": True, "result": handler(local_id)})
            except Exception as exc:
                results.append({"local_id": local_id, "ok": False, "error": self._safe_error(exc)})
        return {
            "action": str(action),
            "requested": len(identifiers),
            "succeeded": sum(1 for item in results if item["ok"]),
            "results": results,
        }

    def create_managed_profile(
        self,
        data: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        name = self._validate_name(data.get("name"))
        requested_key = str(idempotency_key or "").strip()
        workspace_id = str(self.client.workspace_id)
        local_id = (
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"roxy-profile-manager:create:{workspace_id}:{requested_key}",
            ).hex
            if requested_key
            else uuid.uuid4().hex
        )
        owner_marker = self._owner_marker(local_id)
        profile = self.store.get_profile(local_id)
        if profile is None:
            try:
                profile = self.store.create_profile(
                    local_id=local_id,
                    workspace_id=workspace_id,
                    project_id=str(self.client.project_id or ""),
                    display_name=name,
                    owner_marker=owner_marker,
                )
            except RoxyProfileConflict:
                profile = self._require_profile(local_id)
        operation, created = self.store.prepare_operation(
            local_id=local_id,
            operation_type="create",
            idempotency_key=requested_key or f"create:{local_id}",
            checkpoint={"phase": "prepared"},
        )
        if not created:
            return self._public_profile(self._require_profile(operation["local_id"]))
        self.store.transition(local_id, "REMOTE_CREATING", expected_state="LOCAL_ONLY")
        payload = self._create_payload(data, name)
        try:
            remote = self.client.create_profile(payload, owner_marker=owner_marker)
        except Exception as exc:
            self._uncertain(local_id, operation["operation_id"], exc, "create")
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        try:
            profile = self.store.transition(
                local_id,
                "ACTIVE_STOPPED",
                expected_state="REMOTE_CREATING",
                dir_id=remote["dirId"],
                remote_state="active",
                operation_id=operation["operation_id"],
            )
            self.store.update_operation(
                operation["operation_id"],
                state="succeeded",
                checkpoint={"phase": "remote_created"},
            )
        except Exception as exc:
            self._uncertain(local_id, operation["operation_id"], exc, "remote_created")
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        return self._public_profile(profile)

    def update_managed_profile(
        self,
        local_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state not in {"ACTIVE_STOPPED", "RUNNING"} or not profile.dir_id:
            raise RoxyProfileManagerStateError(
                "Profile must be active before it can be updated"
            )
        payload: dict[str, Any] = {}
        if "name" in data:
            payload["name"] = self._validate_name(data.get("name"))
        for field in (
            "os", "osVersion", "coreType", "coreVersion", "projectId",
            "proxyInfo", "fingerInfo", "windowPlatformList", "randomFingerprint",
            "syncBookmark", "syncHistory", "syncTab", "syncCookie",
            "syncExtensions", "syncPassword", "syncIndexedDb", "syncLocalStorage",
        ):
            if field in data:
                payload[field] = data[field]
        if "defaultOpenUrl" in data:
            payload["defaultOpenUrl"] = self._default_open_urls(data["defaultOpenUrl"])
        try:
            remote = self.client.update_profile(
                profile.dir_id,
                payload,
                owner_marker=profile.owner_marker,
            )
        except Exception as exc:
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        updated = self.store.update_identity(
            local_id,
            dir_id=profile.dir_id,
            display_name=str(remote.get("name") or profile.display_name),
        )
        return self._public_profile(updated)

    def open_managed_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state in {"TRASHED", "RESTORE_REQUIRED"}:
            raise RoxyProfileManagerStateError(
                "Profile is archived in Roxy Trash; restore it in RoxyBrowser, then reconcile"
            )
        if profile.state != "ACTIVE_STOPPED" or not profile.dir_id:
            raise RoxyProfileManagerStateError(
                f"Profile cannot be opened from state {profile.state}"
            )
        try:
            remote = self.client.get_profile(profile.dir_id)
            self.client.require_owned(remote, profile.owner_marker)
            opened = self.client.open_profile(profile.dir_id)
        except Exception as exc:
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        try:
            updated = self.store.transition(
                local_id,
                "RUNNING",
                expected_state="ACTIVE_STOPPED",
                remote_state="running",
            )
        except Exception as exc:
            try:
                self.store.transition(
                    local_id,
                    "NEEDS_RECONCILIATION",
                    expected_state="ACTIVE_STOPPED",
                    remote_state="running",
                    last_error=self._safe_error(exc),
                )
            except RoxyProfileConflict:
                pass
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        try:
            signature_sha256, _ = self.signature_probe(opened.debugger_address)
            if signature_sha256:
                self.store.save_official_signature(local_id, signature_sha256)
                updated = self._require_profile(local_id)
        except Exception:
            pass
        return {
            "profile": self._public_profile(updated),
            "connection": opened.to_dict(),
        }

    def close_managed_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state != "RUNNING" or not profile.dir_id:
            raise RoxyProfileManagerStateError("Profile is not running")
        try:
            remote = self.client.get_profile(profile.dir_id)
            self.client.require_owned(remote, profile.owner_marker)
            self.client.close_profile(profile.dir_id)
        except Exception as exc:
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        try:
            updated = self.store.transition(
                local_id,
                "ACTIVE_STOPPED",
                expected_state="RUNNING",
                remote_state="active",
            )
        except Exception as exc:
            try:
                self.store.transition(
                    local_id,
                    "NEEDS_RECONCILIATION",
                    expected_state="RUNNING",
                    remote_state="active",
                    last_error=self._safe_error(exc),
                )
            except RoxyProfileConflict:
                pass
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        return self._public_profile(updated)

    def export_managed_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state != "ACTIVE_STOPPED":
            raise RoxyProfileManagerStateError(
                "Metadata export requires an active stopped Roxy profile"
            )
        if not profile.dir_id:
            raise RoxyProfileManagerStateError("Profile has no active Roxy dirId")
        try:
            detail = self.client.get_profile(profile.dir_id)
            self.client.require_owned(detail, profile.owner_marker)
            archive_store = self.archive_store or _archive_store()
            artifact = archive_store.create(
                local_id=profile.local_id,
                workspace_id=profile.workspace_id,
                dir_id=profile.dir_id,
                profile_metadata=detail,
            )
        except (RoxyProfileArchiveError, RoxyProfileManagerClientError) as exc:
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        try:
            record = self.store.save_archive(
                local_id=profile.local_id,
                archive_id=artifact.archive_id,
                format_version=ARCHIVE_FORMAT,
                archive_kind="metadata",
                source_core_version=str(detail.get("coreVersion") or ""),
                path=str(artifact.path),
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                capabilities=artifact.capabilities,
                verified_at=artifact.verified_at,
            )
        except Exception:
            artifact.path.unlink(missing_ok=True)
            raise
        return {
            "profile": self._public_profile(profile),
            "archive": self._public_archive(record),
        }

    def export_full_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state not in {"ACTIVE_STOPPED", "ARCHIVE_COMMITTED"}:
            raise RoxyProfileManagerStateError(
                "Profile must be stopped before full-folder export"
            )
        if not profile.dir_id:
            raise RoxyProfileManagerStateError("Profile has no active Roxy dirId")
        if self.client.connection_info([profile.dir_id]):
            raise RoxyProfileManagerStateError("Roxy still reports the profile as running")
        source = Path(_manager_cfg.ROXY_PROFILE_CACHE_ROOT) / profile.dir_id
        if not source.is_dir():
            raise RoxyProfileManagerStateError(
                "Roxy local cache is unavailable; open and close the profile before export"
            )
        original_state = profile.state
        operation, created = self.store.prepare_operation(
            local_id=local_id,
            operation_type="full_export",
            idempotency_key=f"full-export:{local_id}:{uuid.uuid4().hex}",
            checkpoint={"phase": "prepared", "source_state": original_state},
        )
        if not created:
            raise RoxyProfileManagerStateError("Full export operation already exists")
        self.store.transition(
            local_id,
            "SNAPSHOTTING",
            expected_state=original_state,
            operation_id=operation["operation_id"],
        )
        artifact = None
        archive_saved = False
        try:
            detail = self.client.get_profile(profile.dir_id)
            self.client.require_owned(detail, profile.owner_marker)
            core_version = str(detail.get("coreVersion") or "")
            archive_store = self.archive_store or _archive_store(full_folder=True)
            snapshot_provider = self.snapshot_provider or archive_store.create_folder
            artifact = snapshot_provider(
                local_id=profile.local_id,
                workspace_id=profile.workspace_id,
                dir_id=profile.dir_id,
                profile_directory=source,
                core_version=core_version,
                profile_metadata=detail,
                official_signature_sha256=profile.official_signature_sha256,
            )
            if self.client.connection_info([profile.dir_id]):
                raise RoxyProfileManagerStateError(
                    "Roxy reopened the profile while its local cache was being captured"
                )
            archive_store.verify_folder(artifact.path, artifact.sha256)
            record = self.store.save_archive(
                local_id=profile.local_id,
                archive_id=artifact.archive_id,
                format_version=FOLDER_ARCHIVE_FORMAT,
                archive_kind="full_folder",
                source_core_version=core_version,
                path=str(artifact.path),
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                capabilities=artifact.capabilities,
                verified_at=artifact.verified_at,
            )
            archive_saved = True
            self.store.update_operation(
                operation["operation_id"],
                state="running",
                checkpoint={"phase": "archive_saved", "archive_id": artifact.archive_id},
            )
            updated = self.store.transition(
                local_id,
                "ARCHIVE_COMMITTED",
                expected_state="SNAPSHOTTING",
                operation_id=operation["operation_id"],
            )
            self.store.update_operation(
                operation["operation_id"],
                state="succeeded",
                checkpoint={"phase": "archive_committed", "archive_id": artifact.archive_id},
            )
            return {
                "profile": self._public_profile(updated),
                "archive": self._public_archive(record),
            }
        except Exception as exc:
            if artifact is not None and not archive_saved:
                try:
                    artifact.path.unlink()
                except OSError:
                    pass
            if archive_saved:
                try:
                    self.store.transition(
                        local_id,
                        "NEEDS_RECONCILIATION",
                        expected_state="SNAPSHOTTING",
                        last_error=self._safe_error(exc),
                        operation_id=operation["operation_id"],
                    )
                    self.store.update_operation(
                        operation["operation_id"],
                        state="uncertain",
                        checkpoint={"phase": "archive_saved"},
                        error=self._safe_error(exc),
                    )
                except RoxyProfileConflict:
                    pass
            else:
                try:
                    self.store.transition(
                        local_id,
                        original_state,
                        expected_state="SNAPSHOTTING",
                        last_error=self._safe_error(exc),
                        operation_id=operation["operation_id"],
                    )
                    self.store.update_operation(
                        operation["operation_id"],
                        state="failed",
                        checkpoint={"phase": "snapshot_failed"},
                        error=self._safe_error(exc),
                    )
                except RoxyProfileConflict:
                    pass
            if isinstance(exc, (RoxyProfileArchiveError, RoxyProfileManagerClientError)):
                raise RoxyProfileManagerError(self._safe_error(exc)) from exc
            raise

    def import_full_profile(
        self,
        archive_path: str | Path,
        *,
        display_name: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        name = self._validate_name(display_name)
        archive_store = self.archive_store or _archive_store(full_folder=True)
        source_local_id = ""
        artifact = None
        try:
            artifact = archive_store.import_folder(archive_path)
            header = archive_store.read_folder_header(artifact.path)
            if header.get("format") != FOLDER_ARCHIVE_FORMAT:
                raise RoxyProfileManagerStateError("Only full-folder archive v2 can be imported")
            source_local_id = str(header.get("local_id") or "").strip()
            if (
                not source_local_id
                or len(source_local_id) > 128
                or not source_local_id.replace("-", "").replace("_", "").isalnum()
            ):
                raise RoxyProfileManagerStateError("Imported archive local identity is invalid")
            if self.store.get_profile(source_local_id) is not None:
                artifact.path.unlink(missing_ok=True)
                raise RoxyProfileManagerStateError("A profile with this archive identity already exists")
        except (RoxyProfileArchiveError, RoxyProfileManagerStateError) as exc:
            if artifact is not None and isinstance(exc, RoxyProfileManagerStateError):
                artifact.path.unlink(missing_ok=True)
            if isinstance(exc, RoxyProfileManagerStateError):
                raise
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        owner_marker = self._owner_marker(source_local_id)
        try:
            updated, record = self.store.import_offline_profile(
                local_id=source_local_id,
                workspace_id=str(self.client.workspace_id),
                project_id=str(self.client.project_id or ""),
                display_name=name,
                owner_marker=owner_marker,
                archive_id=artifact.archive_id,
                format_version=FOLDER_ARCHIVE_FORMAT,
                archive_kind="full_folder",
                source_core_version=str(header.get("source", {}).get("core_version") or ""),
                path=str(artifact.path),
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                capabilities=artifact.capabilities,
                verified_at=artifact.verified_at,
            )
        except Exception:
            artifact.path.unlink(missing_ok=True)
            raise
        return {
            "profile": self._public_profile(updated),
            "archive": self._public_archive(record),
        }

    def open_offline_profile(self, local_id: str) -> dict[str, Any]:
        if not _manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED:
            raise RoxyProfileManagerStateError("Offline profile opening is disabled")
        profile = self._require_profile(local_id)
        if profile.state not in {
            "TRASHED", "RESTORE_REQUIRED", "OFFLINE_STOPPED", "OFFLINE_UNVERIFIED",
        }:
            raise RoxyProfileManagerStateError(
                f"Profile cannot be opened locally from state {profile.state}"
            )
        if not profile.archive_id:
            raise RoxyProfileManagerStateError("Full-folder archive is required")
        record = self.store.get_archive(profile.archive_id)
        if record is None or record.format_version != FOLDER_ARCHIVE_FORMAT:
            raise RoxyProfileManagerStateError("Full-folder archive v2 is required")
        if self.store.get_launch(local_id):
            raise RoxyProfileManagerStateError("Offline profile already has a launch record")
        if profile.offline_staging_path and Path(profile.offline_staging_path).exists():
            raise RoxyProfileManagerStateError(
                "Offline staging requires checkpoint recovery before another local open"
            )
        staging = Path(_manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR) / local_id
        self.store.transition(
            local_id,
            "OFFLINE_STAGING",
            expected_state=profile.state,
        )
        launch = None
        launch_saved = False
        core_version = ""
        try:
            self.store.set_offline_staging_path(local_id, str(staging))
            archive_store = self.archive_store or _archive_store(full_folder=True)
            result = archive_store.extract_folder(record.path, staging)
            header = result["header"]
            core_version = str(header.get("source", {}).get("core_version") or "")
            launch = self.offline_launcher(
                staging,
                executable=_manager_cfg.ROXY_PROFILE_ROXY_CHROME_PATH,
                core_version=core_version,
                official_signature_sha256=str(
                    header.get("official_signature_sha256") or ""
                ),
                timeout=float(_manager_cfg.ROXY_PROFILE_OFFLINE_TIMEOUT),
                allow_version_mismatch=bool(
                    _manager_cfg.ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH
                ),
            )
            self.store.save_launch(
                local_id=local_id,
                backend="local_roxy_chrome",
                pid=launch.pid,
                debugger_address=launch.debugger_address,
                staging_path=str(staging),
                executable_path=str(launch.executable),
                process_started_at=launch.process_started_at,
                core_version=core_version,
                fingerprint_status=launch.fingerprint_status,
                signature_sha256=launch.signature_sha256,
            )
            launch_saved = True
            updated = self.store.transition(
                local_id,
                "OFFLINE_RUNNING",
                expected_state="OFFLINE_STAGING",
            )
            return {
                "profile": self._public_profile(updated),
                "connection": {
                    "debugger_address": launch.debugger_address,
                    "pid": launch.pid,
                    "capability": launch.capability,
                    "fingerprint_status": launch.fingerprint_status,
                    "signature_sha256": launch.signature_sha256,
                },
            }
        except Exception as exc:
            failure = exc
            cleanup_staging = launch is None
            if launch is not None:
                try:
                    self.offline_stopper(launch)
                except Exception as stop_exc:
                    failure = RoxyProfileManagerError(
                        f"{self._safe_error(exc)}; launched process cleanup failed: "
                        f"{self._safe_error(stop_exc)}"
                    )
                    if not launch_saved:
                        try:
                            self.store.save_launch(
                                local_id=local_id,
                                backend="local_roxy_chrome",
                                pid=launch.pid,
                                debugger_address=launch.debugger_address,
                                staging_path=str(staging),
                                executable_path=str(launch.executable),
                                process_started_at=launch.process_started_at,
                                core_version=core_version,
                                fingerprint_status=launch.fingerprint_status,
                                signature_sha256=launch.signature_sha256,
                            )
                        except Exception:
                            pass
                else:
                    self.store.clear_launch(local_id)
                    cleanup_staging = True
            if cleanup_staging:
                shutil.rmtree(staging, ignore_errors=True)
                self.store.set_offline_staging_path(local_id)
            try:
                self.store.transition(
                    local_id,
                    "OFFLINE_UNVERIFIED",
                    expected_state="OFFLINE_STAGING",
                    last_error=self._safe_error(failure),
                )
            except RoxyProfileConflict:
                pass
            if isinstance(failure, RoxyProfileManagerError):
                raise failure from exc
            if isinstance(failure, (RoxyProfileArchiveError, RoxyProfileLauncherError)):
                raise RoxyProfileManagerError(self._safe_error(failure)) from failure
            raise failure

    def close_offline_profile(self, local_id: str) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state not in {"OFFLINE_RUNNING", "OFFLINE_UNVERIFIED"}:
            raise RoxyProfileManagerStateError("Offline profile is not running")
        launch_data = self.store.get_launch(local_id)
        if profile.state == "OFFLINE_RUNNING" and not launch_data:
            raise RoxyProfileManagerStateError("Offline launch record is missing")
        launch = None
        if launch_data:
            launch = RoxyLocalLaunch(
                profile_path=Path(launch_data["staging_path"]),
                executable=Path(launch_data["executable_path"]),
                pid=int(launch_data["pid"]),
                debugger_address=str(launch_data["debugger_address"]),
                process_started_at=str(launch_data["process_started_at"]),
                fingerprint_status=str(launch_data["fingerprint_status"]),
                signature_sha256=str(launch_data["signature_sha256"]),
            )
        staging = (
            launch.profile_path
            if launch is not None
            else Path(
                profile.offline_staging_path
                or (Path(_manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR) / local_id)
            )
        )
        if not staging.is_dir():
            raise RoxyProfileManagerStateError("Offline staging directory is missing")
        process_stopped = launch is None
        artifact = None
        archive_saved = False
        try:
            if launch is not None:
                self.offline_stopper(launch)
                process_stopped = True
                self.store.clear_launch(local_id)
            self.store.transition(
                local_id,
                "SNAPSHOTTING",
                expected_state=profile.state,
            )
            archive_store = self.archive_store or _archive_store(full_folder=True)
            artifact = archive_store.create_folder(
                local_id=profile.local_id,
                workspace_id=profile.workspace_id,
                dir_id=profile.dir_id or profile.local_id,
                profile_directory=staging,
                core_version=str((launch_data or {}).get("core_version") or ""),
            )
            archive_store.verify_folder(artifact.path, artifact.sha256)
            record = self.store.save_archive(
                local_id=profile.local_id,
                archive_id=artifact.archive_id,
                format_version=FOLDER_ARCHIVE_FORMAT,
                archive_kind="full_folder",
                source_core_version=str((launch_data or {}).get("core_version") or ""),
                path=str(artifact.path),
                byte_size=artifact.byte_size,
                sha256=artifact.sha256,
                capabilities=artifact.capabilities,
                verified_at=artifact.verified_at,
            )
            archive_saved = True
            shutil.rmtree(staging, ignore_errors=False)
            self.store.set_offline_staging_path(local_id)
            updated = self.store.transition(
                local_id,
                "OFFLINE_STOPPED",
                expected_state="SNAPSHOTTING",
            )
            return {
                "profile": self._public_profile(updated),
                "archive": self._public_archive(record),
            }
        except Exception as exc:
            if artifact is not None and not archive_saved:
                artifact.path.unlink(missing_ok=True)
            if process_stopped:
                self.store.clear_launch(local_id)
            current = self.store.get_profile(local_id)
            expected = current.state if current is not None else None
            if expected in {"SNAPSHOTTING", "OFFLINE_RUNNING", "OFFLINE_UNVERIFIED"}:
                try:
                    self.store.transition(
                        local_id,
                        "OFFLINE_UNVERIFIED",
                        expected_state=expected,
                        last_error=self._safe_error(exc),
                    )
                except RoxyProfileConflict:
                    pass
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc

    def archive_managed_profile(
        self,
        local_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        profile = self._require_profile(local_id)
        if profile.state == "RUNNING":
            raise RoxyProfileManagerStateError(
                "Close the profile before archiving it"
            )
        if profile.state == "ACTIVE_STOPPED":
            self.export_full_profile(local_id)
            profile = self._require_profile(local_id)
        if profile.state != "ARCHIVE_COMMITTED" or not profile.dir_id:
            raise RoxyProfileManagerStateError(
                "A verified full-folder archive is required before soft delete"
            )
        archive = self.store.get_archive(profile.archive_id or "")
        if archive is None or archive.format_version != FOLDER_ARCHIVE_FORMAT:
            raise RoxyProfileManagerStateError(
                "A verified full-folder archive v2 is required before soft delete"
            )
        archive_store = self.archive_store or _archive_store(full_folder=True)
        archive_store.verify_folder(archive.path, archive.sha256)
        if self.client.connection_info([profile.dir_id]):
            raise RoxyProfileManagerStateError(
                "Roxy still reports the profile as running"
            )
        operation, created = self.store.prepare_operation(
            local_id=local_id,
            operation_type="archive",
            idempotency_key=str(idempotency_key or f"archive:{local_id}:{profile.archive_id}"),
            checkpoint={"phase": "archive_verified"},
        )
        if not created and operation["state"] == "succeeded":
            return self.get_profile(local_id)
        self.store.transition(
            local_id,
            "SOFT_DELETE_PENDING",
            expected_state="ARCHIVE_COMMITTED",
            operation_id=operation["operation_id"],
        )
        try:
            self.client.soft_delete_profile(
                profile.dir_id,
                owner_marker=profile.owner_marker,
            )
        except Exception as exc:
            self._uncertain(local_id, operation["operation_id"], exc, "archive")
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        try:
            updated = self.store.transition(
                local_id,
                "TRASHED",
                expected_state="SOFT_DELETE_PENDING",
                remote_state="trashed",
                operation_id=operation["operation_id"],
            )
            self.store.update_operation(
                operation["operation_id"],
                state="succeeded",
                checkpoint={"phase": "soft_deleted"},
            )
        except Exception as exc:
            self._uncertain(local_id, operation["operation_id"], exc, "soft_deleted")
            raise RoxyProfileManagerError(self._safe_error(exc)) from exc
        return self._public_profile(updated)

    def reconcile_profiles(self) -> dict[str, Any]:
        try:
            remote = self.client.list_profiles()
            running = {
                str(item.get("dirId") or "")
                for item in self.client.connection_info()
                if item.get("dirId")
            }
        except Exception as exc:
            raise RoxyProfileManagerUpstreamError(self._safe_error(exc)) from exc
        remote_by_id = {
            str(item.get("dirId") or ""): item
            for item in remote
            if item.get("dirId")
        }
        updated = 0
        preserved_states = {
            "SNAPSHOTTING", "ARCHIVE_COMMITTED", "SOFT_DELETE_PENDING",
            "OFFLINE_STAGING", "OFFLINE_RUNNING", "OFFLINE_STOPPED",
            "OFFLINE_UNVERIFIED",
        }
        for profile in self.store.list_profiles():
            if not profile.dir_id or profile.state in preserved_states:
                continue
            item = remote_by_id.get(profile.dir_id)
            if item:
                try:
                    self.client.require_owned(item, profile.owner_marker)
                except RoxyProfileManagerClientError:
                    continue
                target = "RUNNING" if profile.dir_id in running else "ACTIVE_STOPPED"
                if profile.state != target:
                    self.store.transition(
                        profile.local_id,
                        target,
                        remote_state="running" if target == "RUNNING" else "active",
                    )
                    updated += 1
            elif profile.state not in {
                "TRASHED", "RESTORE_REQUIRED", "OFFLINE_RUNNING",
                "OFFLINE_STOPPED", "OFFLINE_UNVERIFIED",
            }:
                self.store.transition(
                    profile.local_id,
                    "NEEDS_RECONCILIATION",
                    remote_state="missing",
                    last_error="Remote profile is absent from active Roxy inventory",
                )
                updated += 1
        return {"updated": updated, "active_remote_count": len(remote_by_id)}

    def archive_path(self, local_id: str) -> Path:
        profile = self._require_profile(local_id)
        if not profile.archive_id:
            raise RoxyProfileManagerNotFound("Profile has no archive")
        record = self.store.get_archive(profile.archive_id)
        if record is None:
            raise RoxyProfileManagerNotFound("Archive catalog entry does not exist")
        archive_store = self.archive_store or _archive_store(
            full_folder=record.format_version == FOLDER_ARCHIVE_FORMAT
        )
        if record.format_version == FOLDER_ARCHIVE_FORMAT:
            archive_store.verify_folder(record.path, record.sha256)
        else:
            archive_store.verify(record.path, record.sha256)
        return Path(record.path)

    def _require_profile(self, local_id: str) -> ManagedRoxyProfile:
        profile = self.store.get_profile(str(local_id))
        if profile is None:
            raise RoxyProfileManagerNotFound("Managed profile does not exist")
        return profile

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RoxyProfileManagerStateError("Roxy profile manager is disabled")

    @staticmethod
    def _validate_name(value: Any) -> str:
        name = str(value or "").strip()
        if not 1 <= len(name) <= 100:
            raise RoxyProfileManagerError("Profile name must be 1-100 characters")
        return name

    @staticmethod
    def _create_payload(data: dict[str, Any], name: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        for field in (
            "os", "osVersion", "coreType", "coreVersion", "projectId",
            "proxyInfo", "fingerInfo", "windowPlatformList", "randomFingerprint",
            "syncBookmark", "syncHistory", "syncTab", "syncCookie",
            "syncExtensions", "syncPassword", "syncIndexedDb", "syncLocalStorage",
        ):
            if field in data:
                payload[field] = data[field]
        if "defaultOpenUrl" in data:
            payload["defaultOpenUrl"] = RoxyProfileManager._default_open_urls(
                data["defaultOpenUrl"]
            )
        payload.setdefault("os", "Windows")
        return payload

    @staticmethod
    def _default_open_urls(value: Any) -> list[str]:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            raise RoxyProfileManagerError("defaultOpenUrl must be a URL list")
        urls = [str(item or "").strip() for item in values]
        if any(not item for item in urls) or len(urls) > 20:
            raise RoxyProfileManagerError("defaultOpenUrl contains invalid URLs")
        return urls

    @staticmethod
    def _owner_marker(local_id: str) -> str:
        prefix = str(
            _manager_cfg.ROXY_PROFILE_MANAGER_OWNER_PREFIX or "zcode-profile-manager"
        ).strip()
        digest = hashlib.sha256(str(local_id).encode("utf-8")).hexdigest()[:16]
        return f"{prefix}:{digest}"

    def _uncertain(
        self,
        local_id: str,
        operation_id: str,
        exc: Exception,
        phase: str,
    ) -> None:
        error = self._safe_error(exc)
        try:
            self.store.transition(
                local_id,
                "NEEDS_RECONCILIATION",
                last_error=error,
                operation_id=operation_id,
                payload={"phase": phase},
            )
            self.store.update_operation(
                operation_id,
                state="uncertain",
                checkpoint={"phase": phase},
                error=error,
            )
        except RoxyProfileConflict:
            pass

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        text = str(exc or "operation failed")
        for secret in (
            str(getattr(_roxy_cfg, "ROXY_API_TOKEN", "") or ""),
            str(getattr(_manager_cfg, "ROXY_PROFILE_ARCHIVE_KEY", "") or ""),
        ):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text[:500]

    def _public_profile(self, profile: ManagedRoxyProfile) -> dict[str, Any]:
        archive = self.store.get_archive(profile.archive_id) if profile.archive_id else None
        launch = self.store.get_launch(profile.local_id)
        archive_capabilities = archive.capabilities if archive else {}
        return {
            "local_id": profile.local_id,
            "dir_id": _masked_dir_id(profile.dir_id),
            "name": profile.display_name,
            "state": profile.state,
            "remote_state": profile.remote_state,
            "archive": self._public_archive(archive) if archive else None,
            "last_error": profile.last_error,
            "updated_at": profile.updated_at,
            "launch": {
                "backend": launch["backend"],
                "pid": launch["pid"],
                "debugger_address": launch["debugger_address"],
                "fingerprint_status": launch["fingerprint_status"],
                "signature_sha256": launch["signature_sha256"],
            } if launch else None,
            "capabilities": {
                "metadata_archive": bool(archive and archive.format_version == ARCHIVE_FORMAT),
                "offline_archive": bool(archive and archive.format_version == FOLDER_ARCHIVE_FORMAT),
                "offline_open": bool(
                    _manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED
                    and archive_capabilities.get("detached_roxy_offline_open")
                ),
                "offline_identity_mode": "browser_state_only",
                "fingerprint_equivalent": False,
                "offline_recovery_staging": bool(profile.offline_staging_path),
                "restore_in_roxy_required": not bool(
                    archive_capabilities.get("detached_roxy_offline_open")
                ),
            },
        }

    @staticmethod
    def _public_archive(record) -> dict[str, Any]:
        data = record.to_dict()
        return {
            "archive_id": data["archive_id"],
            "format_version": data["format_version"],
            "archive_kind": data["archive_kind"],
            "source_core_version": data["source_core_version"],
            "byte_size": data["byte_size"],
            "sha256": data["sha256"],
            "encrypted": data["encrypted"],
            "capabilities": data["capabilities"],
            "created_at": data["created_at"],
            "verified_at": data["verified_at"],
        }
