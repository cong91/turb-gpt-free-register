import os
import tempfile
import unittest
from pathlib import Path

from core.nordvpn_wireguard_store import NordVPNWireGuardLeaseStore


class NordVPNWireGuardLeaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = NordVPNWireGuardLeaseStore(Path(self._tmp.name) / "leases.sqlite3")

    def _claim(self, **overrides):
        values = {
            "owner_id": "job-1",
            "source_label": "vn55.nordvpn.com",
            "local_port": 35100,
            "proxy_url": "socks5://127.0.0.1:35100",
            "conf_path": "C:/wg/vn55.conf",
            "owner_pid": os.getpid(),
            "owner_thread_id": 1,
            "acquired_at": "2026-08-29T12:00:00+00:00",
        }
        values.update(overrides)
        return self.store.try_claim(**values)

    def test_source_port_owner_and_profile_are_unique(self):
        first = self._claim()

        self.assertIsNotNone(first)
        self.assertIsNone(self._claim(owner_id="job-2"))
        self.assertIsNone(
            self._claim(
                owner_id="job-5",
                source_label="VN55.nordvpn.com.conf",
                local_port=35102,
            )
        )
        self.assertIsNone(self._claim(owner_id="job-3", source_label="vn56.nordvpn.com"))
        self.assertIsNone(self._claim(owner_id="job-4", source_label="vn57.nordvpn.com", local_port=35100))

        self.assertEqual(self.store.get_owner_lease("job-1")["source_label"], "vn55.nordvpn.com")

    def test_profile_binding_and_release(self):
        lease_id = self._claim()

        self.assertTrue(self.store.update_lease(lease_id, profile_id="roxy-1"))
        second_lease_id = self._claim(
            owner_id="job-2", source_label="vn56.nordvpn.com", local_port=35101
        )
        self.assertFalse(self.store.update_lease(second_lease_id, profile_id="roxy-1"))
        self.assertTrue(self.store.release(lease_id))
        self.assertTrue(self.store.release(second_lease_id))
        self.assertEqual(self.store.list_active(), [])

    def test_cleanup_stale_returns_and_removes_dead_owner(self):
        lease_id = self._claim(owner_pid=999999)

        stale = self.store.cleanup_stale(lambda pid: pid == os.getpid())

        self.assertEqual([row["lease_id"] for row in stale], [lease_id])
        self.assertEqual(self.store.list_active(), [])


if __name__ == "__main__":
    unittest.main()
