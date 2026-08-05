# -*- coding: utf-8 -*-
"""Tests for NordVPN auto-rotation timing and gate behaviour."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import registration_service


class NordVPNRotationTimingTests(unittest.TestCase):
    """Auto-rotation must close the gate at threshold N, not queue end."""

    def setUp(self):
        registration_service._MAINTENANCE_BARRIER.reset_for_tests()
        registration_service._rotation_pending = False
        registration_service._ACTIVE_JOBS.clear()
        self.addCleanup(
            registration_service._MAINTENANCE_BARRIER.reset_for_tests,
        )
        self.addCleanup(setattr, registration_service, "_rotation_pending", False)
        self.addCleanup(registration_service._ACTIVE_JOBS.clear)

    def test_rotation_triggers_even_when_replacement_workers_remain(self):
        """Gate should close at threshold N, not wait for empty queue."""
        # Two workers are active: job 7 finishes, job 8 still running.
        registration_service._ACTIVE_JOBS.update({7, 8})
        registration_service._rotation_pending = True

        calls = []
        with patch(
            "core.nordvpn_cli.execute_rotation", side_effect=lambda: calls.append(1) or True
        ):
            registration_service._deactivate_job(7)

        # Gate must close even though worker 8 is still active.
        self.assertEqual(len(calls), 1, "rotation callback was not invoked")
        self.assertFalse(
            registration_service._rotation_pending,
            "rotation pending should be cleared after successful rotation",
        )

    def test_failed_rotation_keeps_start_gate_closed(self):
        """A failed IP change must block the next queued registration."""
        registration_service._MAINTENANCE_BARRIER.wait_before_start(
            14,
            lambda: False,
        )
        registration_service._ACTIVE_JOBS.add(14)
        registration_service._rotation_pending = True

        with patch("core.nordvpn_cli.execute_rotation", return_value=False):
            registration_service._deactivate_job(14)

        status = registration_service._MAINTENANCE_BARRIER.status()
        self.assertEqual(status["state"], "awaiting_confirmation")
        self.assertTrue(registration_service._rotation_pending)

    def test_retry_failed_rotation_keeps_gate_closed(self):
        registration_service._MAINTENANCE_BARRIER.start(
            "NordVPN IP rotation (auto)",
            drain_timeout_seconds=30,
        )
        registration_service._rotation_pending = True

        with patch("core.nordvpn_cli.execute_rotation", return_value=False):
            result = registration_service.retry_pending_nordvpn_rotation()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 503)
        self.assertTrue(registration_service._rotation_pending)
        self.assertEqual(
            registration_service.nordvpn_rotation_status()["gate_state"],
            "awaiting_confirmation",
        )

    def test_retry_success_reopens_gate(self):
        registration_service._MAINTENANCE_BARRIER.start(
            "NordVPN IP rotation (auto)",
            drain_timeout_seconds=30,
        )
        registration_service._rotation_pending = True

        with patch("core.nordvpn_cli.execute_rotation", return_value=True):
            result = registration_service.retry_pending_nordvpn_rotation()

        self.assertTrue(result["ok"])
        self.assertFalse(registration_service._rotation_pending)
        self.assertEqual(
            registration_service.nordvpn_rotation_status()["gate_state"],
            "open",
        )

    def test_failed_rotation_appends_specific_reason(self):
        registration_service._ACTIVE_JOBS.add(15)
        registration_service._rotation_pending = True
        with TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "job.log"
            with patch.object(
                registration_service.db,
                "get_job",
                return_value={"id": 15, "log_file": str(log_file)},
            ), patch(
                "core.nordvpn_cli.execute_rotation",
                return_value=False,
            ), patch(
                "core.nordvpn_cli.rotation_status_detail",
                return_value={"error": "Public IP did not change (198.51.100.10)", "detail": None},
            ):
                registration_service._deactivate_job(15)

            text = log_file.read_text(encoding="utf-8")
        self.assertIn("Public IP did not change", text)
        self.assertIn("start gate remains closed", text)

    def test_successful_rotation_appends_public_ip_change(self):
        registration_service._ACTIVE_JOBS.add(16)
        registration_service._rotation_pending = True
        with TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "job.log"
            with patch.object(
                registration_service.db,
                "get_job",
                return_value={"id": 16, "log_file": str(log_file)},
            ), patch(
                "core.nordvpn_cli.execute_rotation",
                return_value=True,
            ), patch(
                "core.nordvpn_cli.rotation_status_detail",
                return_value={
                    "error": None,
                    "detail": "NordVPN rotation succeeded: 198.51.100.10 -> 203.0.113.20 (group=Japan)",
                },
            ):
                registration_service._deactivate_job(16)

            text = log_file.read_text(encoding="utf-8")
        self.assertIn("198.51.100.10 -> 203.0.113.20", text)

    def test_rotation_pending_persists_when_barrier_already_busy(self):
        """When deferred_rotation fails, pending flag stays True for retry."""
        registration_service._ACTIVE_JOBS.add(9)
        registration_service._rotation_pending = True

        # Simulate barrier already in use (e.g. another worker started rotation first).
        with patch.object(
            registration_service._MAINTENANCE_BARRIER,
            "deferred_rotation",
            return_value=False,
        ):
            registration_service._deactivate_job(9)

        self.assertTrue(
            registration_service._rotation_pending,
            "pending flag must survive failed rotation attempt",
        )

    def test_auto_rotation_forces_single_registration_worker(self):
        """System-wide Nord CLI rotation cannot share one IP across workers."""
        with patch("config.nordvpn.NORDVPN_ENABLED", True), patch(
            "config.nordvpn.NORDVPN_AUTO_ROTATE_ENABLED", True
        ), patch("config.nordvpn.NORDVPN_AUTO_ROTATE_INTERVAL", 3), patch(
            "core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False
        ):
            self.assertEqual(registration_service.effective_registration_workers(8), 1)

    def test_per_profile_proxy_preserves_requested_workers(self):
        """Independent Roxy proxies do not require system-wide serialization."""
        with patch("config.nordvpn.NORDVPN_ENABLED", True), patch(
            "config.nordvpn.NORDVPN_AUTO_ROTATE_ENABLED", True
        ), patch("config.nordvpn.NORDVPN_AUTO_ROTATE_INTERVAL", 3), patch(
            "core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True
        ):
            self.assertEqual(registration_service.effective_registration_workers(8), 8)

    def test_zero_rotation_interval_preserves_requested_workers(self):
        """Disabled thresholds must not serialize unrelated registration batches."""
        with patch("config.nordvpn.NORDVPN_ENABLED", True), patch(
            "config.nordvpn.NORDVPN_AUTO_ROTATE_ENABLED", True
        ), patch("config.nordvpn.NORDVPN_AUTO_ROTATE_INTERVAL", 0):
            self.assertEqual(registration_service.effective_registration_workers(8), 8)

    def test_deactivate_job_appends_auto_rotation_outcome(self):
        """The triggering job log must distinguish auto rotation from manual clicks."""
        registration_service._ACTIVE_JOBS.add(12)
        registration_service._rotation_pending = True
        with TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "job.log"
            with patch.object(
                registration_service.db,
                "get_job",
                return_value={"id": 12, "log_file": str(log_file)},
            ), patch(
                "core.nordvpn_cli.execute_rotation", return_value=True
            ):
                registration_service._deactivate_job(12)

            text = log_file.read_text(encoding="utf-8")
        self.assertIn("auto-rotation", text)
        self.assertIn("success", text)


if __name__ == "__main__":
    unittest.main()
