# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.roxy_profile_launcher import (
    RoxyLocalLaunch,
    RoxyProfileLauncherError,
    build_command,
    capture_signature,
    find_roxy_chrome,
    launch_offline,
    stop_offline,
)


class RoxyProfileLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "profile"
        (self.profile / "Default").mkdir(parents=True)
        (self.profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        self.exe = Path(self.temp.name) / "RoxyChrome.exe"
        self.exe.write_bytes(b"test")

    def test_build_command_is_loopback_and_user_data_bound(self):
        command = build_command(self.exe, self.profile, 45678, headless=True)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=45678", command)
        self.assertIn(f"--user-data-dir={self.profile}", command)
        self.assertIn("--headless=new", command)
        self.assertNotIn("--no-sandbox", command)

    @patch("core.roxy_profile_launcher._executable_core_version", return_value="150.2.1")
    def test_executable_product_version_is_used(self, product_version):
        self.assertEqual(
            find_roxy_chrome(str(self.exe), core_version="150"),
            self.exe.resolve(),
        )
        product_version.assert_called_once_with(self.exe.resolve())

    def test_configured_executable_is_selected(self):
        self.assertEqual(find_roxy_chrome(str(self.exe)), self.exe.resolve())

    def test_configured_executable_core_mismatch_fails_closed(self):
        with self.assertRaisesRegex(RoxyProfileLauncherError, "match 150"):
            find_roxy_chrome(str(self.exe), core_version="150")
        self.assertEqual(
            find_roxy_chrome(
                str(self.exe), core_version="150", allow_version_mismatch=True
            ),
            self.exe.resolve(),
        )

    @patch("core.roxy_profile_launcher._process_started_at", return_value="2026-08-08T00:00:00+00:00")
    @patch("core.roxy_profile_launcher.capture_signature", return_value=("signature", "unknown"))
    @patch("core.roxy_profile_launcher.wait_for_cdp", return_value=("127.0.0.1:45678", "ws://127.0.0.1/devtools/browser/test"))
    @patch("core.roxy_profile_launcher.subprocess.Popen")
    def test_launch_returns_browser_state_only(self, popen, wait, capture, started):
        process = popen.return_value
        process.pid = 123
        result = launch_offline(self.profile, executable=str(self.exe), port=45678)
        self.assertEqual(result.pid, 123)
        self.assertEqual(result.capability, "browser_state_only")
        self.assertEqual(result.fingerprint_status, "unknown")
        self.assertEqual(result.signature_sha256, "signature")
        popen.assert_called_once()
        wait.assert_called_once_with(45678, timeout=20.0)
        capture.assert_called_once_with(
            "ws://127.0.0.1/devtools/browser/test",
            debugger_address="127.0.0.1:45678",
            official_signature_sha256="",
        )
        started.assert_called_once_with(123)

    @patch("core.roxy_profile_launcher._page_web_socket", return_value="ws://127.0.0.1/page")
    @patch("core.roxy_profile_launcher._cdp_command")
    def test_signature_compares_only_hashed_browser_state(self, command, page_socket):
        responses = [
            {"modelName": "model", "modelVersion": "version", "gpu": {"devices": []}},
            {"result": {"value": {"platform": "Win32", "language": "en-US"}}},
        ]
        command.side_effect = responses + responses
        signature, status = capture_signature(
            "ws://127.0.0.1/browser",
            debugger_address="127.0.0.1:45678",
        )
        self.assertEqual(len(signature), 64)
        self.assertEqual(status, "unknown")
        compared, compared_status = capture_signature(
            "ws://127.0.0.1/browser",
            debugger_address="127.0.0.1:45678",
            official_signature_sha256=signature,
        )
        self.assertEqual(compared, signature)
        self.assertEqual(compared_status, "matched")
        page_socket.assert_called()

    def test_missing_profile_preferences_fail(self):
        (self.profile / "Default" / "Preferences").unlink()
        with self.assertRaises(RoxyProfileLauncherError):
            launch_offline(self.profile, executable=str(self.exe))

    @patch("core.roxy_profile_launcher._tracked_process_matches", return_value=False)
    @patch("core.roxy_profile_launcher.subprocess.Popen")
    def test_stop_rejects_process_identity_mismatch(self, popen, matches):
        launch = RoxyLocalLaunch(
            self.profile,
            self.exe,
            123,
            "127.0.0.1:45678",
            "2026-08-08T00:00:00+00:00",
        )
        with self.assertRaisesRegex(RoxyProfileLauncherError, "identity"):
            stop_offline(launch)
        popen.assert_not_called()
        matches.assert_called_once_with(launch)

    @patch("core.roxy_profile_launcher._tracked_process_matches", return_value=True)
    @patch("core.roxy_profile_launcher.subprocess.Popen")
    def test_stop_rejects_taskkill_failure(self, popen, matches):
        process = popen.return_value
        process.wait.return_value = 5
        launch = RoxyLocalLaunch(
            self.profile,
            self.exe,
            123,
            "127.0.0.1:45678",
            "2026-08-08T00:00:00+00:00",
        )
        with self.assertRaisesRegex(RoxyProfileLauncherError, "exit 5"):
            stop_offline(launch)
        matches.assert_called_once_with(launch)

    @patch(
        "core.roxy_profile_launcher._process_started_at",
        side_effect=RoxyProfileLauncherError("start time failed"),
    )
    @patch("core.roxy_profile_launcher.capture_signature", return_value=("", "unknown"))
    @patch("core.roxy_profile_launcher.wait_for_cdp", return_value=("127.0.0.1:45678", "ws://127.0.0.1/browser"))
    @patch("core.roxy_profile_launcher.subprocess.Popen")
    def test_process_identity_probe_failure_kills_spawned_process(
        self, popen, wait, capture, started
    ):
        process = popen.return_value
        process.pid = 123
        with self.assertRaisesRegex(RoxyProfileLauncherError, "start time failed"):
            launch_offline(self.profile, executable=str(self.exe), port=45678)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    @patch("core.roxy_profile_launcher.wait_for_cdp", side_effect=RoxyProfileLauncherError("timeout"))
    @patch("core.roxy_profile_launcher.subprocess.Popen")
    def test_launch_timeout_kills_spawned_process(self, popen, wait):
        process = popen.return_value
        process.pid = 123
        with self.assertRaises(RoxyProfileLauncherError):
            launch_offline(self.profile, executable=str(self.exe), port=45678)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
