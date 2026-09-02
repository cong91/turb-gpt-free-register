import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.roxy_profile_archive import FOLDER_ARCHIVE_FORMAT, RoxyProfileArchiveStore
from core.roxy_profile_launcher import RoxyLocalLaunch
from core.roxy_profile_manager import RoxyProfileManager, RoxyProfileManagerStateError
from core.roxy_profile_manager_client import RoxyProfileOpenResult
from core.roxy_profile_store import RoxyProfileStore


class _ManagerClient:
    workspace_id = "workspace"
    project_id = "project"

    def __init__(self, directory: Path):
        self.directory = directory
        self.deleted = []

    def create_profile(self, payload, *, owner_marker):
        return {"dirId": "dir-1", "name": payload["name"]}

    def get_profile(self, dir_id):
        return {"dirId": dir_id, "name": "Managed", "windowRemark": "[managed:owner]", "coreVersion": "150"}

    def require_owned(self, profile, owner_marker):
        return None

    def open_profile(self, dir_id):
        return RoxyProfileOpenResult(str(dir_id), "127.0.0.1:45678", 123)

    def close_profile(self, dir_id):
        return None

    def connection_info(self, dir_ids=None):
        return []

    def soft_delete_profile(self, dir_id, *, owner_marker):
        self.deleted.append((dir_id, owner_marker))

    def list_profiles(self):
        return []


class RoxyProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RoxyProfileStore(self.root / "profiles.sqlite3")
        self.key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        self.archive = RoxyProfileArchiveStore(self.root / "archives", self.key, max_bytes=10 * 1024 * 1024)
        self.client = _ManagerClient(self.root)
        self.source = self.root / "cache" / "dir-1"
        (self.source / "Default").mkdir(parents=True)
        (self.source / "Default" / "Preferences").write_text("{}", encoding="utf-8")

    def _manager(self, *, launcher=None, stopper=None, signature_probe=None):
        return RoxyProfileManager(
            store=self.store,
            client=self.client,
            archive_store=self.archive,
            signature_probe=signature_probe,
            offline_launcher=launcher,
            offline_stopper=stopper,
        )

    def _active_profile(self, local_id="local-1"):
        profile = self.store.create_profile(
            local_id=local_id,
            workspace_id="workspace",
            project_id="project",
            display_name="Managed",
            owner_marker="owner",
        )
        self.store.transition(profile.local_id, "REMOTE_CREATING", expected_state="LOCAL_ONLY")
        return self.store.transition(
            profile.local_id,
            "ACTIVE_STOPPED",
            expected_state="REMOTE_CREATING",
            dir_id="dir-1",
            remote_state="active",
        )

    def test_remote_open_persists_official_signature_for_full_export(self):
        self._active_profile()
        signature = "a" * 64
        manager = self._manager(signature_probe=Mock(return_value=(signature, "unknown")))
        opened = manager.open_managed_profile("local-1")
        self.assertEqual(opened["profile"]["state"], "RUNNING")
        self.assertEqual(
            self.store.get_profile("local-1").official_signature_sha256,
            signature,
        )
        manager.close_managed_profile("local-1")
        original_create = self.archive.create_folder
        self.archive.create_folder = Mock(wraps=original_create)
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            manager.export_full_profile("local-1")
        self.assertEqual(
            self.archive.create_folder.call_args.kwargs["official_signature_sha256"],
            signature,
        )

    def test_remote_open_ignores_signature_probe_failure(self):
        self._active_profile()
        manager = self._manager(
            signature_probe=Mock(side_effect=RuntimeError("probe failed"))
        )
        opened = manager.open_managed_profile("local-1")
        self.assertEqual(opened["profile"]["state"], "RUNNING")
        self.assertEqual(
            self.store.get_profile("local-1").official_signature_sha256,
            "",
        )

    def test_create_payload_normalizes_default_open_url_list(self):
        payload = self._manager()._create_payload(
            {"name": "Managed", "defaultOpenUrl": "about:blank"},
            "Managed",
        )
        self.assertEqual(payload["defaultOpenUrl"], ["about:blank"])
        with self.assertRaisesRegex(Exception, "URL list"):
            self._manager()._create_payload(
                {"name": "Managed", "defaultOpenUrl": {"bad": True}},
                "Managed",
            )

    def test_create_idempotency_key_reuses_profile_and_remote_create(self):
        manager = self._manager()
        self.client.create_profile = Mock(
            return_value={"dirId": "dir-created", "name": "Managed"}
        )
        first = manager.create_managed_profile(
            {"name": "Managed"}, idempotency_key="same-create"
        )
        second = manager.create_managed_profile(
            {"name": "Managed"}, idempotency_key="same-create"
        )
        self.assertEqual(first["local_id"], second["local_id"])
        self.assertEqual(len(self.store.list_profiles()), 1)
        self.client.create_profile.assert_called_once()

    def test_remote_open_commit_failure_requires_reconciliation(self):
        self._active_profile()
        manager = self._manager(signature_probe=Mock(return_value=("", "unknown")))
        original_transition = self.store.transition

        def transition(local_id, new_state, **kwargs):
            if new_state == "RUNNING":
                raise RuntimeError("db commit failed")
            return original_transition(local_id, new_state, **kwargs)

        with patch.object(self.store, "transition", side_effect=transition):  # noqa: SIM117
            with self.assertRaisesRegex(Exception, "db commit failed"):
                manager.open_managed_profile("local-1")
        self.assertEqual(
            self.store.get_profile("local-1").state,
            "NEEDS_RECONCILIATION",
        )
        self.assertEqual(self.store.get_profile("local-1").remote_state, "running")

    def test_remote_close_checks_ownership_before_mutation(self):
        self._active_profile()
        self.store.transition("local-1", "RUNNING", expected_state="ACTIVE_STOPPED")
        self.client.close_profile = Mock()
        self.client.require_owned = Mock(side_effect=RuntimeError("not owned"))
        with self.assertRaisesRegex(Exception, "not owned"):
            self._manager().close_managed_profile("local-1")
        self.client.close_profile.assert_not_called()

    def test_metadata_export_does_not_change_remote_lifecycle(self):
        self._active_profile()
        exported = self._manager().export_managed_profile("local-1")
        self.assertEqual(exported["archive"]["format_version"], "roxy-profile-archive-v1")
        self.assertEqual(self.store.get_profile("local-1").state, "ACTIVE_STOPPED")

    def test_metadata_export_cannot_replace_committed_full_archive(self):
        self._active_profile()
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            exported = self._manager().export_full_profile("local-1")
        archive_id = exported["archive"]["archive_id"]
        with self.assertRaises(RoxyProfileManagerStateError):
            self._manager().export_managed_profile("local-1")
        self.assertEqual(self.store.get_profile("local-1").archive_id, archive_id)

    def test_reconcile_preserves_committed_full_archive_state(self):
        self._active_profile()
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            self._manager().export_full_profile("local-1")
        self.client.list_profiles = Mock(return_value=[{
            "dirId": "dir-1",
            "windowRemark": "[managed:owner]",
        }])
        self._manager().reconcile_profiles()
        self.assertEqual(self.store.get_profile("local-1").state, "ARCHIVE_COMMITTED")

    def test_full_export_then_archive_requires_folder_artifact(self):
        self._active_profile()
        manager = self._manager()
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")), \
             patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_ARCHIVE_KEY", self.key):
            exported = manager.export_full_profile("local-1")
            self.assertEqual(exported["archive"]["format_version"], FOLDER_ARCHIVE_FORMAT)
            archived = manager.archive_managed_profile("local-1")
        self.assertEqual(archived["state"], "TRASHED")
        self.assertEqual(len(self.client.deleted), 1)

    def test_offline_open_close_checkpoints_modified_staging(self):
        self._active_profile()
        executable = self.root / "150" / "RoxyChrome.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"test")
        launches = []

        def launcher(staging, **kwargs):
            staging_path = Path(staging)
            (staging_path / "Local Storage").mkdir(exist_ok=True)
            (staging_path / "Local Storage" / "changed").write_text(
                "local-change", encoding="utf-8"
            )
            launch = RoxyLocalLaunch(
                staging_path,
                executable,
                123,
                "127.0.0.1:45678",
                "2026-08-08T00:00:00+00:00",
                fingerprint_status="unknown",
                signature_sha256="signature",
            )
            launches.append(launch)
            return launch

        stopper = Mock()
        manager = self._manager(launcher=launcher, stopper=stopper)
        staging_root = self.root / "staging"
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")), \
             patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR", str(staging_root)), \
             patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED", True), \
             patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH", False):
            manager.export_full_profile("local-1")
            manager.archive_managed_profile("local-1")
            opened = manager.open_offline_profile("local-1")
            self.assertEqual(opened["profile"]["state"], "OFFLINE_RUNNING")
            self.assertEqual(opened["connection"]["capability"], "browser_state_only")
            closed = manager.close_offline_profile("local-1")
        self.assertEqual(closed["profile"]["state"], "OFFLINE_STOPPED")
        self.assertFalse((staging_root / "local-1").exists())
        self.assertIsNone(self.store.get_launch("local-1"))
        stopper.assert_called_once_with(launches[0])
        restored = self.root / "checkpoint-restored"
        self.archive.extract_folder(self.store.get_archive(self.store.get_profile("local-1").archive_id).path, restored)
        self.assertEqual(
            (restored / "Local Storage" / "changed").read_text(encoding="utf-8"),
            "local-change",
        )

    def test_checkpoint_failure_keeps_staging_unverified(self):
        self._active_profile("local-fail")
        self.store.update_identity("local-fail", dir_id="dir-1")
        manager = self._manager()
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            manager.export_full_profile("local-fail")
        self.store.transition("local-fail", "OFFLINE_STAGING", expected_state="ARCHIVE_COMMITTED")
        self.store.transition("local-fail", "OFFLINE_RUNNING", expected_state="OFFLINE_STAGING")
        staging = self.root / "staging-fail"
        (staging / "Default").mkdir(parents=True)
        (staging / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        self.store.save_launch(
            local_id="local-fail",
            backend="local_roxy_chrome",
            executable_path=str(self.root / "150" / "RoxyChrome.exe"),
            pid=123,
            debugger_address="127.0.0.1:45678",
            staging_path=str(staging),
            process_started_at="2026-08-08T00:00:00+00:00",
            core_version="150",
        )
        manager = self._manager(stopper=Mock(side_effect=RuntimeError("stop failed")))
        with self.assertRaisesRegex(Exception, "stop failed"):
            manager.close_offline_profile("local-fail")
        self.assertEqual(self.store.get_profile("local-fail").state, "OFFLINE_UNVERIFIED")
        self.assertTrue(staging.exists())
        self.assertIsNotNone(self.store.get_launch("local-fail"))

    def test_open_commit_failure_stops_launched_process(self):
        self._active_profile("local-open-fail")
        self.store.update_identity("local-open-fail", dir_id="dir-1")
        executable = self.root / "150" / "RoxyChrome.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"test")
        launch = RoxyLocalLaunch(
            self.root / "unused",
            executable,
            123,
            "127.0.0.1:45678",
            "2026-08-08T00:00:00+00:00",
        )
        stopper = Mock()
        manager = self._manager(launcher=Mock(return_value=launch), stopper=stopper)
        staging_root = self.root / "open-fail-staging"
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            manager.export_full_profile("local-open-fail")
            manager.archive_managed_profile("local-open-fail")
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR", str(staging_root)), \
             patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED", True), \
             patch.object(self.store, "save_launch", side_effect=RuntimeError("db failed")):  # noqa: SIM117
            with self.assertRaisesRegex(RuntimeError, "db failed"):
                manager.open_offline_profile("local-open-fail")
        stopper.assert_called_once_with(launch)
        self.assertFalse((staging_root / "local-open-fail").exists())
        self.assertIsNone(self.store.get_launch("local-open-fail"))
        self.assertEqual(
            self.store.get_profile("local-open-fail").state,
            "OFFLINE_UNVERIFIED",
        )

    def test_checkpoint_verify_failure_keeps_recoverable_staging(self):
        self._active_profile("local-verify-fail")
        self.store.update_identity("local-verify-fail", dir_id="dir-1")
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_CACHE_ROOT", str(self.root / "cache")):
            self._manager().export_full_profile("local-verify-fail")
        self.store.transition(
            "local-verify-fail", "OFFLINE_STAGING", expected_state="ARCHIVE_COMMITTED"
        )
        self.store.transition(
            "local-verify-fail", "OFFLINE_RUNNING", expected_state="OFFLINE_STAGING"
        )
        staging_root = self.root / "verify-fail-staging"
        staging = staging_root / "local-verify-fail"
        (staging / "Default").mkdir(parents=True)
        (staging / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        self.store.save_launch(
            local_id="local-verify-fail",
            backend="local_roxy_chrome",
            executable_path=str(self.root / "150" / "RoxyChrome.exe"),
            pid=123,
            debugger_address="127.0.0.1:45678",
            staging_path=str(staging),
            process_started_at="2026-08-08T00:00:00+00:00",
            core_version="150",
        )
        stopper = Mock()
        manager = self._manager(stopper=stopper)
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR", str(staging_root)), \
             patch.object(self.archive, "verify_folder", side_effect=RuntimeError("verify failed")):  # noqa: SIM117
            with self.assertRaisesRegex(Exception, "verify failed"):
                manager.close_offline_profile("local-verify-fail")
        stopper.assert_called_once()
        self.assertEqual(
            self.store.get_profile("local-verify-fail").state,
            "OFFLINE_UNVERIFIED",
        )
        self.assertTrue(staging.exists())
        self.assertIsNone(self.store.get_launch("local-verify-fail"))
        self.assertEqual(
            self.store.get_profile("local-verify-fail").offline_staging_path,
            str(staging),
        )

        reopened_store = RoxyProfileStore(self.store.path)
        restarted = RoxyProfileManager(
            store=reopened_store,
            client=self.client,
            archive_store=self.archive,
            offline_stopper=stopper,
        )
        changed_root = self.root / "different-config-root"
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_STAGING_DIR", str(changed_root)):
            recovered = restarted.close_offline_profile("local-verify-fail")
        self.assertEqual(recovered["profile"]["state"], "OFFLINE_STOPPED")
        self.assertFalse(staging.exists())
        self.assertEqual(
            reopened_store.get_profile("local-verify-fail").offline_staging_path,
            "",
        )

    def test_bulk_action_returns_per_profile_results(self):
        manager = self._manager()
        manager.close_managed_profile = Mock(side_effect=[{"state": "ACTIVE_STOPPED"}, RuntimeError("conflict")])
        result = manager.bulk_action(["one", "one", "two"], "remote_close")
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertFalse(result["results"][1]["ok"])

    def test_public_status_redacts_archive_key(self):
        manager = self._manager()
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_ARCHIVE_KEY", self.key):
            payload = manager.status()
        self.assertNotIn(self.key, repr(payload))

    def test_status_is_local_only_by_default(self):
        manager = self._manager()
        self.client.list_profiles = Mock(side_effect=AssertionError("Roxy must not be probed"))

        payload = manager.status()

        self.client.list_profiles.assert_not_called()
        self.assertEqual(payload["active_remote_count"], 0)
        self.assertEqual(payload["remote_error"], "")

    def test_status_can_probe_remote_when_requested(self):
        manager = self._manager()
        self.client.list_profiles = Mock(return_value=[{"dirId": "dir-1"}])

        payload = manager.status(include_remote=True)

        self.client.list_profiles.assert_called_once_with()
        self.assertEqual(payload["active_remote_count"], 1)

    def test_offline_open_can_be_disabled_by_config(self):
        profile = self.store.create_profile(
            local_id="local-2",
            workspace_id="workspace",
            project_id="project",
            display_name="Local",
            owner_marker="owner-2",
        )
        with patch("core.roxy_profile_manager._manager_cfg.ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED", False):  # noqa: SIM117
            with self.assertRaises(RoxyProfileManagerStateError):
                self._manager().open_offline_profile(profile.local_id)


if __name__ == "__main__":
    unittest.main()
