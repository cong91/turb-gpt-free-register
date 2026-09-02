"""NordVPN WireGuard → SOCKS5 代理管理器单元测试。

覆盖配置扫描、临时 conf 生成、端口分配/探测、wireproxy 生命周期以及
``proxy_for_registration`` 上下文管理器的启用/禁用与异常释放语义。
所有子进程、socket 与配置读取均在使用点打桩，不触发真实外部副作用。
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import nordvpn_wireguard as wg
from core.registration_network_identity import NetworkIdentityError


def _write_conf(directory: Path, name: str, body: str = "") -> Path:
    """在目录写入一个最小可用的 WireGuard .conf 并返回其路径。"""
    path = directory / name
    text = body or (
        "[Interface]\n"
        "PrivateKey = aaa\n"
        "Address = 10.5.0.2/32\n"
        "DNS = 103.86.96.100\n"
        "\n"
        "[Peer]\n"
        "PublicKey = bbb\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "Endpoint = 185.93.0.1:51820\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


class ScanConfigsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_missing_directory_returns_empty(self) -> None:
        missing = self.dir / "nope"
        self.assertEqual(wg.scan_wg_configs(missing), [])

    def test_no_conf_files_returns_empty(self) -> None:
        (self.dir / "readme.txt").write_text("x", encoding="utf-8")
        self.assertEqual(wg.scan_wg_configs(self.dir), [])

    def test_lists_all_conf_sorted(self) -> None:
        _write_conf(self.dir, "us9999.nordvpn.com.conf")
        _write_conf(self.dir, "jp123.nordvpn.com.conf")
        names = [p.name for p in wg.scan_wg_configs(self.dir)]
        self.assertEqual(names, ["jp123.nordvpn.com.conf", "us9999.nordvpn.com.conf"])

    def test_country_filter_matches_substring(self) -> None:
        _write_conf(self.dir, "us9999.nordvpn.com.conf")
        _write_conf(self.dir, "jp123.nordvpn.com.conf")
        names = [p.name for p in wg.scan_wg_configs(self.dir, "us")]
        self.assertEqual(names, ["us9999.nordvpn.com.conf"])

    def test_country_filter_case_insensitive(self) -> None:
        _write_conf(self.dir, "JP123.nordvpn.com.conf")
        names = [p.name for p in wg.scan_wg_configs(self.dir, "jp")]
        self.assertEqual(names, ["JP123.nordvpn.com.conf"])

    def test_blank_country_filter_returns_all(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "jp1.conf")
        self.assertEqual(len(wg.scan_wg_configs(self.dir, "   ")), 2)


class BuildWireproxyConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _read_and_cleanup(self, temp_path: str) -> str:
        self.addCleanup(lambda: Path(temp_path).unlink(missing_ok=True))
        return Path(temp_path).read_text(encoding="utf-8")

    def test_appends_socks5_section(self) -> None:
        conf = _write_conf(self.dir, "us1.conf")
        out = self._read_and_cleanup(wg._build_wireproxy_config(str(conf), 25000))
        self.assertIn("[Interface]", out)
        self.assertIn("[Peer]", out)
        self.assertIn("[Socks5]", out)
        self.assertIn("BindAddress = 127.0.0.1:25000", out)

    def test_strips_existing_socks5_section(self) -> None:
        body = (
            "[Interface]\nPrivateKey = aaa\n\n"
            "[Peer]\nPublicKey = bbb\nEndpoint = 1.2.3.4:51820\n\n"
            "[Socks5]\nBindAddress = 127.0.0.1:9999\n"
        )
        conf = _write_conf(self.dir, "us1.conf", body=body)
        out = self._read_and_cleanup(wg._build_wireproxy_config(str(conf), 25001))
        self.assertNotIn("127.0.0.1:9999", out)
        self.assertIn("BindAddress = 127.0.0.1:25001", out)
        self.assertEqual(out.count("[Socks5]"), 1)

    def test_preserves_interface_and_peer_after_stripping(self) -> None:
        body = (
            "[Interface]\nPrivateKey = keep_me\n\n"
            "[Peer]\nEndpoint = 9.9.9.9:51820\n\n"
            "[HTTP]\nBindAddress = 127.0.0.1:8080\n"
        )
        conf = _write_conf(self.dir, "jp1.conf", body=body)
        out = self._read_and_cleanup(wg._build_wireproxy_config(str(conf), 25002))
        self.assertIn("PrivateKey = keep_me", out)
        self.assertIn("Endpoint = 9.9.9.9:51820", out)
        self.assertNotIn("127.0.0.1:8080", out)


class PortHelperTests(unittest.TestCase):
    def test_is_port_free_when_connection_refused(self) -> None:
        with mock.patch.object(wg.socket, "create_connection", side_effect=OSError):
            self.assertTrue(wg._is_port_free(25000))

    def test_is_port_free_false_when_listening(self) -> None:
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        with mock.patch.object(wg.socket, "create_connection", return_value=fake):
            self.assertFalse(wg._is_port_free(25000))

    def test_wait_for_port_returns_when_ready(self) -> None:
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        with mock.patch.object(wg.socket, "create_connection", return_value=fake):
            wg._wait_for_port("127.0.0.1", 25000, timeout=1.0)  # 不抛出即通过

    def test_wait_for_port_timeout_raises(self) -> None:
        with mock.patch.object(wg.socket, "create_connection", side_effect=OSError), \
             mock.patch.object(wg.time, "sleep"):  # noqa: SIM117
            with self.assertRaises(wg.WireGuardProxyError):
                wg._wait_for_port("127.0.0.1", 25000, timeout=0.2)


class ProxyPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._probe_patch = mock.patch(
            "core.registration_network_identity.probe_socks_public_ip",
            side_effect=lambda proxy_url, **kwargs: f"8.8.8.{proxy_url.rsplit(':', 1)[-1][-1]}",
        )
        self._probe_patch.start()
        self.addCleanup(self._probe_patch.stop)
        self._port_free_patch = mock.patch.object(wg, "_is_port_free", return_value=True)
        self._port_free_patch.start()
        self.addCleanup(self._port_free_patch.stop)

    def _pool(self, **kwargs) -> wg.WireGuardProxyPool:
        return wg.WireGuardProxyPool(
            configs_dir=self.dir,
            wireproxy_exe="wireproxy.exe",
            port_start=kwargs.get("port_start", 25000),
            port_end=kwargs.get("port_end", 25001),
            connect_timeout=kwargs.get("connect_timeout", 1.0),
            lease_store=kwargs.get(
                "lease_store",
                wg.NordVPNWireGuardLeaseStore(self.dir / "leases.sqlite3"),
            ),
        )

    def test_acquire_no_configs_raises(self) -> None:
        pool = self._pool()
        with self.assertRaises(wg.WireGuardProxyError):
            pool.acquire()

    def test_acquire_spawns_and_tracks(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="us1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", return_value=fake_proxy) as spawn:
            proxy = pool.acquire()
        self.assertEqual(proxy.proxy_url, "socks5://127.0.0.1:25000")
        self.assertIn(25000, pool._active)
        self.assertIn(25000, pool._used_ports)
        spawn.assert_called_once()

    def test_acquire_frees_port_on_spawn_failure(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool()
        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(  # noqa: SIM117
                 wg, "_spawn_wireproxy", side_effect=wg.WireGuardProxyError("boom")
             ):
            with self.assertRaises(wg.WireGuardProxyError):
                pool.acquire()
        self.assertEqual(pool._used_ports, set())
        self.assertEqual(pool._active, {})

    def test_acquire_fails_closed_when_egress_probe_fails(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool()
        process = mock.MagicMock()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=process,
            conf_path="us1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", return_value=fake_proxy), \
             mock.patch(  # noqa: SIM117
                 "core.registration_network_identity.probe_socks_public_ip",
                 side_effect=NetworkIdentityError("egress unavailable"),
             ), \
             mock.patch.object(wg.os, "unlink"):
            with self.assertRaisesRegex(wg.WireGuardProxyError, "egress probe failed"):
                pool.acquire()

        process.terminate.assert_called_once()
        self.assertEqual(pool._used_ports, set())
        self.assertEqual(pool._active, {})
        self.assertEqual(pool._active_sources, set())

    def test_acquire_passes_country_filter(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "jp1.conf")
        pool = self._pool()
        captured: dict = {}

        def _fake_spawn(*, conf_path, port, wireproxy_exe, connect_timeout):
            captured["conf_path"] = conf_path
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf="/tmp/x.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=_fake_spawn):
            pool.acquire(country_filter="jp")
        self.assertIn("jp1.conf", captured["conf_path"])

    def test_release_terminates_and_frees(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool()
        proc = mock.MagicMock()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=proc,
            conf_path="us1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool._used_ports.add(25000)
        pool._active[25000] = fake_proxy
        with mock.patch.object(wg.os, "unlink") as unlink:
            pool.release(fake_proxy)
        proc.terminate.assert_called_once()
        unlink.assert_called_once_with("/tmp/x.conf")
        self.assertEqual(pool._used_ports, set())
        self.assertEqual(pool._active, {})

    def test_allocate_port_exhausted_raises(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool(port_start=25000, port_end=25000)
        pool._used_ports.add(25000)
        with self.assertRaises(wg.WireGuardProxyError):
            pool._allocate_port()

    def test_allocate_port_skips_occupied(self) -> None:
        pool = self._pool(port_start=25000, port_end=25001)
        # 25000 视为被占用（有监听），25001 空闲
        def _free(port, host="127.0.0.1"):
            return port == 25001

        with mock.patch.object(wg, "_is_port_free", side_effect=_free):
            self.assertEqual(pool._allocate_port(), 25001)

    def test_release_all_clears_active(self) -> None:
        pool = self._pool()
        for port in (25000, 25001):
            proxy = wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path="x.conf",
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )
            pool._used_ports.add(port)
            pool._active[port] = proxy
        with mock.patch.object(wg.os, "unlink"):
            pool.release_all()
        self.assertEqual(pool._active, {})
        self.assertEqual(pool._used_ports, set())

    def test_concurrent_acquisitions_use_distinct_ports_and_sources(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "us2.conf")
        pool = self._pool()

        def spawn(*, conf_path, port, **_kwargs):
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=spawn), \
             mock.patch.object(wg.random, "choice", side_effect=lambda values: values[0]):
            first = pool.acquire()
            second = pool.acquire()

        self.assertEqual({first.port, second.port}, {25000, 25001})
        self.assertNotEqual(first.source_label, second.source_label)

    def test_persistent_store_prevents_source_reuse_across_pool_instances(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "us2.conf")
        store = wg.NordVPNWireGuardLeaseStore(self.dir / "shared-leases.sqlite3")
        first_pool = self._pool(port_start=35100, port_end=35101, lease_store=store)
        second_pool = self._pool(port_start=35100, port_end=35101, lease_store=store)

        def spawn(*, conf_path, port, **_kwargs):
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(first_pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(second_pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=spawn), \
             mock.patch.object(wg, "_is_port_free", return_value=True), \
             mock.patch.object(wg.random, "shuffle", side_effect=lambda values: None):
            first = first_pool.acquire(owner_id="job-1")
            second = second_pool.acquire(owner_id="job-2")

        self.assertNotEqual(first.source_label, second.source_label)
        self.assertEqual(
            {row["owner_id"] for row in store.list_active()},
            {"job-1", "job-2"},
        )

    def test_profile_binding_is_persisted_and_unique(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "us2.conf")
        pool = self._pool(port_start=35100, port_end=35101)

        def spawn(*, conf_path, port, **_kwargs):
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=spawn), \
             mock.patch.object(wg, "_is_port_free", return_value=True), \
             mock.patch.object(wg.random, "shuffle", side_effect=lambda values: None):
            first = pool.acquire(owner_id="job-1")
            second = pool.acquire(owner_id="job-2")
            pool.bind_profile(first, "roxy-1")
            with self.assertRaisesRegex(wg.WireGuardProxyError, "Browser profile/session.*绑定其它"):
                pool.bind_profile(second, "roxy-1")

        lease = pool._lease_store.get_owner_lease("job-1")
        self.assertEqual(lease["profile_id"], "roxy-1")
        pool.release(first)
        pool.release(second)

    def test_released_port_and_source_can_be_reused_sequentially(self) -> None:
        _write_conf(self.dir, "us1.conf")
        pool = self._pool(port_start=25000, port_end=25000)

        def spawn(*, conf_path, port, **_kwargs):
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=spawn), \
             mock.patch.object(wg.os, "unlink"):
            first = pool.acquire()
            pool.release(first)
            second = pool.acquire()

        self.assertEqual(first.port, 25000)
        self.assertEqual(second.port, 25000)

    def test_duplicate_active_egress_fails_and_rolls_back(self) -> None:
        _write_conf(self.dir, "us1.conf")
        _write_conf(self.dir, "us2.conf")
        pool = self._pool()

        def spawn(*, conf_path, port, **_kwargs):
            return wg.WireGuardProxy(
                port=port,
                process=mock.MagicMock(),
                conf_path=conf_path,
                temp_conf=f"/tmp/{port}.conf",
                proxy_url=f"socks5://127.0.0.1:{port}",
            )

        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", side_effect=spawn), \
             mock.patch.object(wg.random, "choice", side_effect=lambda values: values[0]), \
             mock.patch(
                 "core.registration_network_identity.probe_socks_public_ip",
                 return_value="8.8.8.8",
             ), mock.patch.object(wg.os, "unlink"):
            first = pool.acquire()
            with self.assertRaisesRegex(wg.WireGuardProxyError, "出口 IP 重复"):
                pool.acquire()

        self.assertEqual(set(pool._active), {first.port})
        self.assertEqual(pool._used_ports, {first.port})
        self.assertEqual(pool._active_egress_ips, {"8.8.8.8"})

    def test_acquire_from_conf_text_tracks_source_temp_file(self) -> None:
        pool = self._pool()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="dynamic.conf",
            temp_conf="/tmp/wireproxy.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        conf_text = "[Interface]\nPrivateKey = key\n[Peer]\nPublicKey = peer\n"
        with mock.patch.object(pool, "_wireproxy_executable", return_value="wireproxy.exe"), \
             mock.patch.object(wg, "_spawn_wireproxy", return_value=fake_proxy), \
             mock.patch.object(wg, "_is_port_free", return_value=True):
            proxy = pool.acquire_from_conf_text(conf_text, source_label="jp749")

        self.addCleanup(lambda: Path(proxy.source_temp_conf).unlink(missing_ok=True))
        self.assertTrue(Path(proxy.source_temp_conf).exists())
        self.assertEqual(Path(proxy.source_temp_conf).read_text(encoding="utf-8"), conf_text)
        self.assertIn(25000, pool._active)

    def test_release_removes_dynamic_source_and_wireproxy_temp_files(self) -> None:
        pool = self._pool()
        proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="dynamic.conf",
            temp_conf="/tmp/wireproxy.conf",
            proxy_url="socks5://127.0.0.1:25000",
            source_temp_conf="/tmp/account.conf",
        )
        with mock.patch.object(wg.os, "unlink") as unlink:
            pool.release(proxy)

        self.assertEqual(
            [call.args[0] for call in unlink.call_args_list],
            ["/tmp/wireproxy.conf", "/tmp/account.conf"],
        )


class SpawnWireproxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_spawn_success_returns_ready_proxy(self) -> None:
        conf = _write_conf(self.dir, "us1.conf")
        proc = mock.MagicMock()
        proc.poll.return_value = None
        with mock.patch.object(wg.subprocess, "Popen", return_value=proc), \
             mock.patch.object(wg, "_wait_for_port"), \
             mock.patch.object(wg, "_build_wireproxy_config", return_value="/tmp/t.conf"):
            proxy = wg._spawn_wireproxy(
                conf_path=str(conf),
                port=25000,
                wireproxy_exe="wireproxy.exe",
                connect_timeout=1.0,
            )
        self.assertEqual(proxy.proxy_url, "socks5://127.0.0.1:25000")
        self.assertEqual(proxy.process, proc)

    def test_spawn_popen_failure_cleans_temp_and_raises(self) -> None:
        conf = _write_conf(self.dir, "us1.conf")
        with mock.patch.object(wg, "_build_wireproxy_config", return_value="/tmp/t.conf"), \
             mock.patch.object(wg.subprocess, "Popen", side_effect=OSError("not found")), \
             mock.patch.object(wg.os, "unlink") as unlink:  # noqa: SIM117
            with self.assertRaises(wg.WireGuardProxyError):
                wg._spawn_wireproxy(
                    conf_path=str(conf),
                    port=25000,
                    wireproxy_exe="missing.exe",
                    connect_timeout=1.0,
                )
        unlink.assert_called_once_with("/tmp/t.conf")

    def test_spawn_port_timeout_terminates_and_raises(self) -> None:
        conf = _write_conf(self.dir, "us1.conf")
        proc = mock.MagicMock()
        with mock.patch.object(wg, "_build_wireproxy_config", return_value="/tmp/t.conf"), \
             mock.patch.object(wg.subprocess, "Popen", return_value=proc), \
             mock.patch.object(  # noqa: SIM117
                 wg, "_wait_for_port", side_effect=wg.WireGuardProxyError("timeout")
             ), \
             mock.patch.object(wg.os, "unlink") as unlink:
            with self.assertRaises(wg.WireGuardProxyError):
                wg._spawn_wireproxy(
                    conf_path=str(conf),
                    port=25000,
                    wireproxy_exe="wireproxy.exe",
                    connect_timeout=0.2,
                )
        proc.terminate.assert_called_once()
        unlink.assert_called_once_with("/tmp/t.conf")
    def test_spawn_detects_child_exit_after_listener_ready(self) -> None:
        conf = _write_conf(self.dir, "us1.conf")
        proc = mock.MagicMock()
        proc.poll.return_value = 1
        proc.returncode = 1
        with mock.patch.object(wg.subprocess, "Popen", return_value=proc), \
             mock.patch.object(wg, "_wait_for_port"), \
             mock.patch.object(wg, "_build_wireproxy_config", return_value="/tmp/t.conf"), \
             mock.patch.object(wg.os, "unlink"):  # noqa: SIM117
            with self.assertRaisesRegex(wg.WireGuardProxyError, "已退出"):
                wg._spawn_wireproxy(
                    conf_path=str(conf),
                    port=25000,
                    wireproxy_exe="wireproxy.exe",
                    connect_timeout=1.0,
                )
        proc.terminate.assert_called_once()


class TerminateProcessTests(unittest.TestCase):
    def test_kills_on_wait_timeout(self) -> None:
        proc = mock.MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="wireproxy", timeout=5)
        wg._terminate_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_swallows_terminate_exception(self) -> None:
        proc = mock.MagicMock()
        proc.terminate.side_effect = OSError("already dead")
        wg._terminate_process(proc)  # 不抛出即通过


class ProxyForRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._account_client_patch = mock.patch(
            "core.nordvpn_account.get_account_client",
            return_value=mock.MagicMock(configured=False),
        )
        self._account_client_patch.start()
        self.addCleanup(self._account_client_patch.stop)

    def _enabled_patch(self, enabled: bool):
        return mock.patch.object(wg, "is_per_profile_proxy_enabled", return_value=enabled)

    def test_disabled_yields_none(self) -> None:
        with self._enabled_patch(False):  # noqa: SIM117
            with wg.proxy_for_registration() as proxy_url:
                self.assertIsNone(proxy_url)

    def test_disabled_setting_overrides_configured_access_token(self) -> None:
        with mock.patch.object(wg, "_cfg_attr", return_value=False), mock.patch(
            "config.nordvpn_account.NORDVPN_ACCESS_TOKEN", "configured-token"
        ):
            self.assertFalse(wg.is_per_profile_proxy_enabled())

    def test_enabled_yields_url_and_releases(self) -> None:
        pool = mock.MagicMock()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="us1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool.acquire.return_value = fake_proxy

        def _cfg(name, default):
            return {"NORDVPN_WG_ENABLED": True, "NORDVPN_WG_COUNTRY_FILTER": ""}.get(name, default)

        with self._enabled_patch(True), \
             mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):  # noqa: SIM117
            with wg.proxy_for_registration(pool=pool) as proxy_url:
                self.assertEqual(proxy_url, "socks5://127.0.0.1:25000")
        pool.acquire.assert_called_once()
        pool.release.assert_called_once_with(fake_proxy)

    def test_releases_on_exception(self) -> None:
        pool = mock.MagicMock()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="us1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool.acquire.return_value = fake_proxy

        def _cfg(name, default):
            return {"NORDVPN_WG_ENABLED": True, "NORDVPN_WG_COUNTRY_FILTER": ""}.get(name, default)

        with self._enabled_patch(True), \
             mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):  # noqa: SIM117
            with self.assertRaises(RuntimeError):
                with wg.proxy_for_registration(pool=pool):
                    raise RuntimeError("registration blew up")
        pool.release.assert_called_once_with(fake_proxy)

    def test_explicit_country_filter_overrides_config(self) -> None:
        pool = mock.MagicMock()
        fake_proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="jp1.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool.acquire.return_value = fake_proxy

        def _cfg(name, default):
            return {"NORDVPN_WG_ENABLED": True, "NORDVPN_WG_COUNTRY_FILTER": "us"}.get(name, default)

        with self._enabled_patch(True), \
             mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):  # noqa: SIM117
            with wg.proxy_for_registration(country_filter="jp", pool=pool):
                pass
        pool.acquire.assert_called_once_with("jp")

    def test_account_token_mode_builds_dynamic_config(self) -> None:
        pool = mock.MagicMock()
        proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="dynamic.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool.acquire_from_conf_text.return_value = proxy
        account_client = mock.MagicMock(configured=True)
        account_client.build_config.return_value = mock.Mock(
            text="[Interface]\nPrivateKey = key\n[Peer]\nPublicKey = peer\n",
            server=mock.Mock(hostname="jp749.nordvpn.com"),
        )

        def _cfg(name, default):
            return {"NORDVPN_WG_ENABLED": True, "NORDVPN_WG_COUNTRY_FILTER": "JP"}.get(name, default)

        with self._enabled_patch(True), \
             mock.patch.object(wg, "_cfg_attr", side_effect=_cfg), \
             mock.patch("core.nordvpn_account.get_account_client", return_value=account_client):  # noqa: SIM117
            with wg.proxy_for_registration(pool=pool) as proxy_url:
                self.assertEqual(proxy_url, "socks5://127.0.0.1:25000")

        account_client.build_config.assert_called_once_with("JP")
        pool.acquire.assert_not_called()
        pool.acquire_from_conf_text.assert_called_once_with(
            account_client.build_config.return_value.text,
            source_label="jp749.nordvpn.com",
            server_country=None,
            server_load=None,
        )
        pool.release.assert_called_once_with(proxy)

    def test_forwards_explicit_owner_to_lease_acquisition(self) -> None:
        pool = mock.MagicMock()
        proxy = wg.WireGuardProxy(
            port=25000,
            process=mock.MagicMock(),
            conf_path="dynamic.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25000",
        )
        pool.acquire_from_conf_text.return_value = proxy
        account_client = mock.MagicMock(configured=True)
        account_client.build_config.return_value = mock.Mock(
            text="[Interface]\nPrivateKey = a\n[Peer]\nPublicKey = b\n",
            server=mock.Mock(hostname="jp1.nordvpn.com"),
        )

        with self._enabled_patch(True), \
             mock.patch("core.nordvpn_account.get_account_client", return_value=account_client):  # noqa: SIM117
            with wg.proxy_for_registration(pool=pool, owner_id="registration-job:42"):
                pass

        self.assertEqual(
            pool.acquire_from_conf_text.call_args.kwargs["owner_id"],
            "registration-job:42",
        )
    def test_retries_duplicate_egress_with_another_dynamic_server(self) -> None:
        pool = mock.MagicMock()
        proxy = wg.WireGuardProxy(
            port=25001,
            process=mock.MagicMock(),
            conf_path="dynamic.conf",
            temp_conf="/tmp/x.conf",
            proxy_url="socks5://127.0.0.1:25001",
            tunnel_egress_ip="1.1.1.1",
        )
        pool.acquire_from_conf_text.side_effect = [
            wg.WireGuardProxyError("出口 IP 重复"),
            proxy,
        ]
        account_client = mock.MagicMock(configured=True)
        account_client.build_config.side_effect = [
            mock.Mock(text="[Interface]\nPrivateKey = a\n[Peer]\nPublicKey = b\n", server=mock.Mock(hostname="jp1")),
            mock.Mock(text="[Interface]\nPrivateKey = c\n[Peer]\nPublicKey = d\n", server=mock.Mock(hostname="jp2")),
        ]

        with self._enabled_patch(True), \
             mock.patch("core.nordvpn_account.get_account_client", return_value=account_client):  # noqa: SIM117
            with wg.proxy_for_registration(pool=pool) as proxy_url:
                self.assertEqual(proxy_url, proxy.proxy_url)

        self.assertEqual(account_client.build_config.call_count, 2)
        self.assertEqual(pool.acquire_from_conf_text.call_count, 2)
        pool.release.assert_called_once_with(proxy)


class GetPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个测试后复位全局单例，避免相互污染。
        self.addCleanup(self._reset_pool)

    @staticmethod
    def _reset_pool() -> None:
        wg._POOL = None

    def test_builds_pool_from_config(self) -> None:
        def _cfg(name, default):
            return {
                "NORDVPN_WG_CONFIGS_DIR": r"C:\wg",
                "NORDVPN_WG_WIREPROXY_EXE": "wireproxy.exe",
                "NORDVPN_WG_PORT_START": 25000,
                "NORDVPN_WG_PORT_END": 25099,
                "NORDVPN_WG_CONNECT_TIMEOUT": 10.0,
            }.get(name, default)

        with mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):
            pool = wg.get_pool()
        self.assertEqual(pool._wireproxy_exe, "wireproxy.exe")
        self.assertEqual(pool._port_start, 25000)
        self.assertEqual(pool._port_end, 25099)

    def test_rebuilds_when_config_changes(self) -> None:
        values = {
            "NORDVPN_WG_CONFIGS_DIR": r"C:\wg",
            "NORDVPN_WG_WIREPROXY_EXE": "wireproxy.exe",
            "NORDVPN_WG_PORT_START": 25000,
            "NORDVPN_WG_PORT_END": 25099,
            "NORDVPN_WG_CONNECT_TIMEOUT": 10.0,
        }

        def _cfg(name, default):
            return values.get(name, default)

        with mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):
            first = wg.get_pool()
            values["NORDVPN_WG_PORT_END"] = 26000
            second = wg.get_pool()
        self.assertIsNot(first, second)
        self.assertEqual(second._port_end, 26000)

    def test_reuses_token_mode_pool_when_configs_dir_is_blank(self) -> None:
        def _cfg(name, default):
            return {
                "NORDVPN_WG_CONFIGS_DIR": "",
                "NORDVPN_WG_WIREPROXY_EXE": "wireproxy.exe",
                "NORDVPN_WG_PORT_START": 25000,
                "NORDVPN_WG_PORT_END": 25099,
                "NORDVPN_WG_CONNECT_TIMEOUT": 10.0,
            }.get(name, default)

        with mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):
            first = wg.get_pool()
            second = wg.get_pool()
        self.assertIs(first, second)

    def test_reuses_pool_when_config_unchanged(self) -> None:
        def _cfg(name, default):
            return {
                "NORDVPN_WG_CONFIGS_DIR": r"C:\wg",
                "NORDVPN_WG_WIREPROXY_EXE": "wireproxy.exe",
                "NORDVPN_WG_PORT_START": 25000,
                "NORDVPN_WG_PORT_END": 25099,
                "NORDVPN_WG_CONNECT_TIMEOUT": 10.0,
            }.get(name, default)

        with mock.patch.object(wg, "_cfg_attr", side_effect=_cfg):
            first = wg.get_pool()
            second = wg.get_pool()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
