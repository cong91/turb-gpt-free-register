"""Tests for core.nordvpn_cli — NordVPN CLI wrapper."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import core.nordvpn_cli as _mod


class NordVPNCliTests(unittest.TestCase):
    """Unit tests for NordVPN CLI wrapper functions."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_cfg(**overrides) -> MagicMock:
        """Return a mock config.nordvpn module with safe defaults."""
        defaults: dict = {
            "NORDVPN_ENABLED": True,
            "NORDVPN_INSTALL_DIR": r"C:\Program Files\NordVPN",
            "NORDVPN_CLI_TIMEOUT": 30,
            "NORDVPN_SERVICE_HOST": "127.0.0.1",
            "NORDVPN_SERVICE_PORT": 9247,
            "NORDVPN_POST_CONNECT_DELAY": 0.0,
            "NORDVPN_COUNTRY_GROUPS": "",
        }
        defaults.update(overrides)
        mock = MagicMock()
        mock.configure_mock(**defaults)
        return mock

    # ------------------------------------------------------------------
    # _nordvpn_exe
    # ------------------------------------------------------------------

    def test_nordvpn_exe_raises_when_install_dir_missing(self):
        cfg = self._mock_cfg(NORDVPN_INSTALL_DIR=r"C:\DoesNotExist")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            with self.assertRaises(_mod.NordVPNError) as ctx:
                _mod._nordvpn_exe()
            self.assertIn("安装目录不存在", str(ctx.exception))

    @patch.object(Path, "is_dir", return_value=True)
    @patch.object(Path, "is_file", return_value=False)
    def test_nordvpn_exe_raises_when_exe_missing(self, _is_file, _is_dir):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            with self.assertRaises(_mod.NordVPNError) as ctx:
                _mod._nordvpn_exe()
            self.assertIn("找不到 NordVPN.exe", str(ctx.exception))

    # ------------------------------------------------------------------
    # _run_nordvpn
    # ------------------------------------------------------------------

    @patch.object(Path, "is_dir", return_value=True)
    @patch.object(Path, "is_file", return_value=True)
    def test_run_nordvpn_returns_on_success(self, _is_file, _is_dir):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):  # noqa: SIM117
            with patch.object(subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="connected", stderr=""
                )
                result = _mod._run_nordvpn("-c")
                self.assertEqual(result.returncode, 0)

    @patch.object(Path, "is_dir", return_value=True)
    @patch.object(Path, "is_file", return_value=True)
    def test_run_nordvpn_raises_on_nonzero_exit(self, _is_file, _is_dir):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):  # noqa: SIM117
            with patch.object(subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1, stdout="", stderr="already connected"
                )
                with self.assertRaises(_mod.NordVPNError) as ctx:
                    _mod._run_nordvpn("-c", "-g", "Mars")
                self.assertIn("already connected", str(ctx.exception))

    @patch.object(Path, "is_dir", return_value=True)
    @patch.object(Path, "is_file", return_value=True)
    def test_run_nordvpn_includes_timeout(self, _is_file, _is_dir):
        cfg = self._mock_cfg(NORDVPN_CLI_TIMEOUT=15)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):  # noqa: SIM117
            with patch.object(subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
                _mod._run_nordvpn("-c")
                call_kwargs = mock_run.call_args[1]
                # timeout should be at least min(CLI_TIMEOUT, 5) = 15
                self.assertGreaterEqual(call_kwargs.get("timeout", 0), 5)

    # ------------------------------------------------------------------
    # is_service_running
    # ------------------------------------------------------------------

    def test_is_service_running_returns_false_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.is_service_running())

    @patch.object(_mod, "_is_port_listening", return_value=True)
    def test_is_service_running_returns_true_when_port_open(self, _listen):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.is_service_running())

    @patch.object(_mod, "_is_port_listening", return_value=False)
    def test_is_service_running_returns_false_when_port_closed(self, _listen):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.is_service_running())

    # ------------------------------------------------------------------
    # is_connected
    # ------------------------------------------------------------------

    def test_is_connected_returns_false_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.is_connected())

    def test_is_connected_returns_true_when_route_exists(self):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):  # noqa: SIM117
            with patch.object(subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(stdout="1\n", returncode=0)
                self.assertTrue(_mod.is_connected())

    def test_is_connected_returns_false_when_no_route(self):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):  # noqa: SIM117
            with patch.object(subprocess, "run") as mock_run:
                mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
                self.assertFalse(_mod.is_connected())

    # ------------------------------------------------------------------
    # connect
    # ------------------------------------------------------------------

    def test_connect_noop_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.connect())

    @patch.object(_mod, "_run_nordvpn")
    def test_connect_calls_nordvpn_c(self, mock_run):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.connect())
            mock_run.assert_called_once_with("-c")

    @patch.object(_mod, "_run_nordvpn")
    def test_connect_with_country_group(self, mock_run):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.connect(country_group="Japan"))
            mock_run.assert_called_once_with("-c", "-g", "Japan")

    @patch.object(_mod, "_run_nordvpn")
    def test_connect_picks_random_configured_group(self, mock_run):
        cfg = self._mock_cfg(NORDVPN_COUNTRY_GROUPS="Japan,United_States")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.connect())
            args = mock_run.call_args[0]
            self.assertEqual(args[0], "-c")
            self.assertEqual(args[1], "-g")
            self.assertIn(args[2], {"Japan", "United_States"})

    @patch.object(_mod, "_run_nordvpn")
    def test_connect_returns_false_on_failure(self, mock_run):
        mock_run.side_effect = _mod.NordVPNError("fail")
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.connect())

    # ------------------------------------------------------------------
    # disconnect
    # ------------------------------------------------------------------

    def test_disconnect_noop_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.disconnect())

    @patch.object(_mod, "_run_nordvpn")
    def test_disconnect_calls_nordvpn_d(self, mock_run):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.disconnect())
            mock_run.assert_called_once_with("-d")

    @patch.object(_mod, "_run_nordvpn")
    def test_disconnect_returns_false_on_failure(self, mock_run):
        mock_run.side_effect = _mod.NordVPNError("fail")
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.disconnect())

    # ------------------------------------------------------------------
    # VPNContext
    # ------------------------------------------------------------------

    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    @patch.object(_mod, "is_connected", return_value=False)
    def test_vpn_context_connects_and_disconnects(self, _ic, _dc, _c):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            with _mod.VPNContext(country_group="Japan"):
                pass
            _c.assert_called_once_with(country_group="Japan")
            _dc.assert_called_once()

    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    @patch.object(_mod, "is_connected", return_value=False)
    def test_vpn_context_noop_when_disabled(self, _ic, _dc, _c):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            with _mod.VPNContext():
                pass
            _c.assert_not_called()
            _dc.assert_not_called()

    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    @patch.object(_mod, "is_connected", return_value=False)
    def test_vpn_context_disconnects_on_exception(self, _ic, _dc, _c):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            with self.assertRaises(ValueError):  # noqa: SIM117
                with _mod.VPNContext(country_group="Japan"):
                    raise ValueError("boom")
            _c.assert_called_once()
            _dc.assert_called_once()

    # ------------------------------------------------------------------
    # _pick_country_group
    # ------------------------------------------------------------------

    def test_pick_country_group_returns_none_when_empty(self):
        cfg = self._mock_cfg(NORDVPN_COUNTRY_GROUPS="")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertIsNone(_mod._pick_country_group())

    def test_pick_country_group_returns_single_entry(self):
        cfg = self._mock_cfg(NORDVPN_COUNTRY_GROUPS="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertEqual(_mod._pick_country_group(), "Japan")

    # ------------------------------------------------------------------
    # ensure_vpn_ready
    # ------------------------------------------------------------------

    def test_ensure_vpn_ready_returns_false_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.ensure_vpn_ready())

    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=True)
    def test_ensure_vpn_ready_returns_true_when_all_ok(self, _ic, _is):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.ensure_vpn_ready())

    @patch.object(_mod, "is_service_running", return_value=False)
    @patch.object(_mod, "is_connected", return_value=True)
    def test_ensure_vpn_ready_fails_when_service_down(self, _ic, _is):
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.ensure_vpn_ready())
    # ------------------------------------------------------------------
    # notify_registration_success
    # ------------------------------------------------------------------

    def test_notify_success_noop_when_disabled(self):
        cfg = self._mock_cfg(NORDVPN_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.notify_registration_success())

    def test_notify_success_noop_when_auto_rotate_disabled(self):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_ENABLED=False)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.notify_registration_success())

    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True)
    def test_notify_success_noop_when_per_profile_proxy_enabled(self, _enabled):
        cfg = self._mock_cfg(
            NORDVPN_ENABLED=True,
            NORDVPN_AUTO_ROTATE_ENABLED=True,
            NORDVPN_AUTO_ROTATE_INTERVAL=1,
        )
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            _mod._rotate_counter = 0
            self.assertFalse(_mod.notify_registration_success())
            self.assertEqual(_mod._rotate_counter, 0)

    def test_notify_success_noop_when_interval_zero(self):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_INTERVAL=0)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.notify_registration_success())

    def test_notify_success_returns_false_before_threshold(self):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_INTERVAL=3)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            _mod._rotate_counter = 0
            self.assertFalse(_mod.notify_registration_success())  # 1/3
            self.assertFalse(_mod.notify_registration_success())  # 2/3

    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False)
    def test_notify_success_triggers_rotation_at_threshold(self, _disabled):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_INTERVAL=2)
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            _mod._rotate_counter = 0
            self.assertFalse(_mod.notify_registration_success())  # 1/2
            self.assertTrue(_mod.notify_registration_success())   # 2/2 triggers
            self.assertEqual(_mod._rotate_counter, 0)  # reset

    @patch.object(_mod.time, "sleep")
    @patch.object(_mod, "public_ip", side_effect=["198.51.100.10", "203.0.113.20"])
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=True)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_disconnects_before_connecting(
        self,
        mock_disconnect,
        mock_connect,
        _mock_connected,
        _mock_service,
        mock_public_ip,
        mock_sleep,
    ):
        """System-wide rotation must force a disconnect before reconnecting."""
        events = []
        mock_disconnect.side_effect = lambda: events.append("disconnect") or True
        mock_connect.side_effect = lambda **_kwargs: events.append("connect") or True
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.execute_rotation())

        self.assertEqual(events, ["disconnect", "connect"])
        mock_sleep.assert_called_once_with(2.0)
        mock_connect.assert_called_once_with(country_group="Japan")
        self.assertEqual(mock_public_ip.call_count, 2)

    @patch.object(_mod.time, "sleep")
    @patch.object(
        _mod,
        "public_ip",
        side_effect=["198.51.100.10", "198.51.100.10", "203.0.113.20"],
    )
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=True)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_waits_for_public_ip_change(
        self,
        _mock_disconnect,
        _mock_connect,
        _mock_connected,
        _mock_service,
        mock_public_ip,
        mock_sleep,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.execute_rotation())

        self.assertEqual(mock_public_ip.call_count, 3)
        self.assertEqual(
            [call.args for call in mock_sleep.call_args_list],
            [(2.0,), (2.0,)],
        )

    @patch.object(_mod.time, "sleep")
    @patch.object(
        _mod,
        "public_ip",
        side_effect=[
            "198.51.100.10",
            "198.51.100.10",
            "198.51.100.10",
            "198.51.100.10",
            "198.51.100.10",
            "198.51.100.10",
            "198.51.100.10",
            "203.0.113.20",
        ],
    )
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=True)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_reconnects_again_when_ip_is_unchanged(
        self,
        mock_disconnect,
        mock_connect,
        _mock_connected,
        _mock_service,
        _mock_public_ip,
        _mock_sleep,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.execute_rotation())

        self.assertEqual(mock_disconnect.call_count, 2)
        self.assertEqual(mock_connect.call_count, 2)

    @patch.object(_mod.time, "sleep")
    @patch.object(_mod, "public_ip", side_effect=["198.51.100.10", "203.0.113.20"])
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", side_effect=[True, False, False, True])
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_waits_for_nordlynx_route(
        self,
        _mock_disconnect,
        _mock_connect,
        mock_connected,
        _mock_service,
        _mock_public_ip,
        mock_sleep,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.execute_rotation())

        self.assertEqual(mock_connected.call_count, 4)
        self.assertIn((1.0,), [call.args for call in mock_sleep.call_args_list])

    @patch.object(_mod.time, "sleep")
    @patch.object(_mod, "public_ip", side_effect=["198.51.100.10", "203.0.113.20"])
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", side_effect=[True, False, True])
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_records_success_detail(
        self,
        _mock_disconnect,
        _mock_connect,
        _mock_connected,
        _mock_service,
        _mock_public_ip,
        _mock_sleep,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertTrue(_mod.execute_rotation())

        detail = _mod.rotation_status_detail()
        self.assertIsNone(detail["error"])
        self.assertIn("198.51.100.10 -> 203.0.113.20", detail["detail"])

    @patch.object(_mod, "public_ip", return_value="198.51.100.10")
    @patch.object(_mod, "is_connected", return_value=True)
    @patch.object(_mod, "disconnect", return_value=False)
    def test_execute_rotation_records_disconnect_failure(
        self,
        _mock_disconnect,
        _mock_connected,
        _mock_public_ip,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.execute_rotation())

        self.assertIn("disconnect", _mod.rotation_status_detail()["error"])

    @patch.object(_mod.time, "sleep")
    @patch.object(_mod, "public_ip", return_value="198.51.100.10")
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=True)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_fails_when_public_ip_is_unchanged(
        self,
        _mock_disconnect,
        _mock_connect,
        _mock_connected,
        _mock_service,
        _mock_public_ip,
        _mock_sleep,
    ):
        cfg = self._mock_cfg(NORDVPN_AUTO_ROTATE_COUNTRY_GROUP="Japan")
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.execute_rotation())

    @patch.object(_mod, "public_ip", return_value="198.51.100.10")
    @patch.object(_mod, "is_service_running", return_value=False)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_fails_when_service_not_running_after_connect(
        self, _mock_disconnect, mock_connect, _mock_svc, _mock_public_ip
    ):
        """execute_rotation() returns False when post-condition service check fails."""
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.execute_rotation())

    @patch.object(_mod, "public_ip", return_value="198.51.100.10")
    @patch.object(_mod, "is_service_running", return_value=True)
    @patch.object(_mod, "is_connected", return_value=False)
    @patch.object(_mod, "connect", return_value=True)
    @patch.object(_mod, "disconnect", return_value=True)
    def test_execute_rotation_fails_when_tunnel_dead_after_connect(
        self, _mock_disconnect, mock_connect, mock_connected, _mock_svc, _mock_public_ip
    ):
        """execute_rotation() returns False when post-condition tunnel check fails."""
        cfg = self._mock_cfg()
        with patch.object(_mod, "_cfg_attr", side_effect=lambda k, d=None: getattr(cfg, k, d)):
            self.assertFalse(_mod.execute_rotation())

    # ------------------------------------------------------------------
    # ensure_vpn_ready
    # ------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
