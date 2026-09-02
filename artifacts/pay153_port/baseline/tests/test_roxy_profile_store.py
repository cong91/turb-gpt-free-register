# -*- coding: utf-8 -*-
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from core.roxy_profile_store import (
    RoxyProfileConflict,
    RoxyProfileSchemaError,
    RoxyProfileStore,
)


class RoxyProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = RoxyProfileStore(Path(self.temp_dir.name) / "profiles.sqlite3")

    def _profile(self, suffix="one"):
        return self.store.create_profile(
            local_id=f"local-{suffix}",
            workspace_id="123",
            project_id="456",
            display_name=f"Profile {suffix}",
            owner_marker=f"manager:{suffix}",
        )

    def test_state_transitions_are_durable_and_validated(self):
        profile = self._profile()
        self.assertEqual(profile.state, "LOCAL_ONLY")

        profile = self.store.transition(
            profile.local_id,
            "REMOTE_CREATING",
            expected_state="LOCAL_ONLY",
        )
        profile = self.store.transition(
            profile.local_id,
            "ACTIVE_STOPPED",
            expected_state="REMOTE_CREATING",
            dir_id="dir-one",
            remote_state="active",
        )

        self.assertEqual(profile.state, "ACTIVE_STOPPED")
        self.assertEqual(profile.dir_id, "dir-one")
        with self.assertRaisesRegex(RoxyProfileConflict, "Invalid profile transition"):
            self.store.transition(profile.local_id, "TRASHED")

    def test_operation_idempotency_key_returns_same_operation(self):
        profile = self._profile()

        first, created = self.store.prepare_operation(
            local_id=profile.local_id,
            operation_type="create",
            idempotency_key="same-key",
        )
        second, created_again = self.store.prepare_operation(
            local_id=profile.local_id,
            operation_type="create",
            idempotency_key="same-key",
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["operation_id"], second["operation_id"])

    def test_concurrent_duplicate_owner_marker_creates_one_profile(self):
        barrier = threading.Barrier(2)
        successes = []
        failures = []

        def create():
            barrier.wait(timeout=2)
            try:
                successes.append(self.store.create_profile(
                    workspace_id="123",
                    project_id="456",
                    display_name="Concurrent",
                    owner_marker="same-owner",
                ))
            except RoxyProfileConflict as exc:
                failures.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(self.store.list_profiles()), 1)

    def test_offline_import_is_atomic(self):
        profile, archive = self.store.import_offline_profile(
            local_id="imported",
            workspace_id="123",
            project_id="456",
            display_name="Imported",
            owner_marker="manager:imported",
            archive_id="archive-one",
            format_version="roxy-profile-folder-v2",
            archive_kind="full_folder",
            source_core_version="150",
            path="C:/archives/archive-one.rpa2",
            byte_size=10,
            sha256="a" * 64,
            capabilities={"detached_roxy_offline_open": True},
            verified_at="2026-08-08T00:00:00+00:00",
        )
        self.assertEqual(profile.state, "OFFLINE_STOPPED")
        self.assertEqual(profile.archive_id, archive.archive_id)

        with self.assertRaises(RoxyProfileConflict):
            self.store.import_offline_profile(
                local_id="imported-two",
                workspace_id="123",
                project_id="456",
                display_name="Imported two",
                owner_marker="manager:imported-two",
                archive_id="archive-one",
                format_version="roxy-profile-folder-v2",
                archive_kind="full_folder",
                source_core_version="150",
                path="C:/archives/archive-two.rpa2",
                byte_size=10,
                sha256="b" * 64,
                capabilities={},
                verified_at="2026-08-08T00:00:00+00:00",
            )
        self.assertIsNone(self.store.get_profile("imported-two"))

    def test_launch_metadata_survives_store_reopen(self):
        profile = self._profile()
        self.store.save_launch(
            local_id=profile.local_id,
            backend="local_roxy_chrome",
            executable_path="C:/Roxy/150/RoxyChrome.exe",
            pid=123,
            debugger_address="127.0.0.1:45678",
            staging_path="C:/staging/profile",
            process_started_at="2026-08-08T00:00:00+00:00",
            core_version="150",
            fingerprint_status="unknown",
            signature_sha256="signature",
        )
        reopened = RoxyProfileStore(self.store.path).get_launch(profile.local_id)
        self.assertEqual(reopened["pid"], 123)
        self.assertEqual(reopened["core_version"], "150")
        self.assertEqual(reopened["signature_sha256"], "signature")

    def test_offline_staging_path_survives_store_reopen(self):
        profile = self._profile()
        self.store.set_offline_staging_path(profile.local_id, "C:/staging/profile")
        reopened = RoxyProfileStore(self.store.path).get_profile(profile.local_id)
        self.assertEqual(reopened.offline_staging_path, "C:/staging/profile")

    def test_official_signature_survives_store_reopen(self):
        profile = self._profile()
        signature = "a" * 64
        self.store.save_official_signature(profile.local_id, signature)
        reopened = RoxyProfileStore(self.store.path).get_profile(profile.local_id)
        self.assertEqual(reopened.official_signature_sha256, signature)

    def test_unsupported_catalog_versions_fail_closed(self):
        for version in (2, 999):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "unsupported.sqlite3"
                connection = sqlite3.connect(path)
                connection.execute(f"PRAGMA user_version = {version}")
                connection.close()
                with self.assertRaises(RoxyProfileSchemaError):
                    RoxyProfileStore(path).initialize()

    def test_events_are_append_only(self):
        profile = self._profile()
        self.store.transition(profile.local_id, "REMOTE_CREATING")

        connection = sqlite3.connect(self.store.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM roxy_profile_events")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
