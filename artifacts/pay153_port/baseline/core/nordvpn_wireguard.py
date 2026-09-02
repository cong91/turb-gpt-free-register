# -*- coding: utf-8 -*-
"""NordVPN WireGuard → 本地 SOCKS5 代理管理器（基于 wireproxy 用户态实现）。

替代 NordVPN 桌面/CLI 全局隧道方案：把每个 NordVPN 的 WireGuard ``.conf``
文件通过 wireproxy 暴露成一个独立的本地 SOCKS5 端口，再把
``socks5://127.0.0.1:<port>`` 作为 proxy 传给 Roxy Browser 的单个环境。

优势：
    - 每个注册任务拥有独立出口 IP，可完全并发，无需串行化 workers；
    - wireproxy 为用户态 WireGuard，Windows 上无需管理员权限或安装 TUN 驱动；
    - 不改动 ``roxy_registration.py``：它已接受 proxy URL。

数据流::

    NordVPN access token → Core API → 动态 NordLynx 配置
    → wireproxy(临时 conf + [Socks5]) → 127.0.0.1:<port>
    → RoxyBrowserClient.open_profile(proxy=...)

``NORDVPN_ACCESS_TOKEN`` 存在时优先使用动态配置；未配置 token 时仍可从
``NORDVPN_WG_CONFIGS_DIR`` 读取手工下载的 ``.conf`` 作为回退。

wireproxy 二进制：
    https://github.com/pufferffish/wireproxy （单文件可执行，放入 PATH 或指定路径）
"""
import ctypes
import logging
import os
import random
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.nordvpn_wireguard_store import NordVPNWireGuardLeaseStore

logger = logging.getLogger(__name__)

# Windows 下隐藏 wireproxy 子进程窗口；其它平台该标志不存在，取 0 表示无特殊标志。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SOCKS5_SECTION = "[Socks5]\nBindAddress = 127.0.0.1:{port}\n"


class WireGuardProxyError(RuntimeError):
    """WireGuard/wireproxy 代理相关的领域错误。"""


def _normalize_source_label(source_label: str) -> str:
    """Use one identity for a server hostname and its downloaded .conf file."""
    value = str(source_label or "").strip()
    if value.casefold().endswith(".conf"):
        value = value[:-5].rstrip()
    return value.casefold()


def _cfg_attr(name: str, default):
    """按模块属性读取配置，保持 WebUI 热加载语义。"""
    from config import nordvpn_wireguard as _cfg

    return getattr(_cfg, name, default)


@dataclass
class WireGuardProxy:
    """一个已就绪的 wireproxy 实例，把 NordVPN 隧道暴露为本地 SOCKS5。"""

    port: int
    process: subprocess.Popen
    conf_path: str          # 原始 NordVPN .conf 路径
    temp_conf: str          # wireproxy 使用的临时 conf 路径
    proxy_url: str          # socks5://127.0.0.1:<port>
    source_temp_conf: str | None = None  # 动态 token 配置的临时源文件
    source_label: str | None = None
    server_country: str | None = None
    server_load: int | None = None
    tunnel_egress_ip: str | None = None
    lease_id: str | None = None
    owner_id: str | None = None
    profile_id: str | None = None
    pool: object | None = field(default=None, repr=False, compare=False)
    acquired_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def pid(self) -> int | None:
        value = getattr(self.process, "pid", None)
        return int(value) if isinstance(value, int) else None

    def network_identity(self) -> dict:
        return {
            "proxy_url": self.proxy_url,
            "local_port": self.port,
            "wireproxy_pid": self.pid,
            "server_hostname": self.source_label,
            "owner_id": self.owner_id,
            "profile_id": self.profile_id,
            "lease_id": self.lease_id,
            "server_country": self.server_country,
            "server_load": self.server_load,
            "tunnel_egress_ip": self.tunnel_egress_ip,
            "acquired_at": self.acquired_at,
        }


class RegistrationProxy(str):
    """String-compatible proxy URL carrying its live tunnel metadata."""

    def __new__(cls, proxy: WireGuardProxy):
        value = super().__new__(cls, proxy.proxy_url)
        value.tunnel = proxy
        return value


def scan_wg_configs(
    directory: "str | os.PathLike[str]",
    country_filter: str | None = None,
) -> list[Path]:
    """返回目录下全部 NordVPN ``.conf``，可按国家代码过滤。

    NordVPN 的 WireGuard 文件命名类似 ``us9999.nordvpn.com.conf``、
    ``jp123.nordvpn.com.conf``；``country_filter`` 以子串方式对文件名
    （小写）做匹配，例如 ``"us"``、``"jp"``、``"sg"``。
    """
    base = Path(directory)
    if not base.is_dir():
        return []
    configs = sorted(base.glob("*.conf"))
    if country_filter:
        needle = country_filter.strip().lower()
        if needle:
            configs = [c for c in configs if needle in c.stem.lower()]
    return configs


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口空闲返回 True（无监听方接受连接）。"""
    try:
        with socket.create_connection((host, port), timeout=0.1):
            return False
    except OSError:
        return True


def _wait_for_port(host: str, port: int, timeout: float) -> None:
    """轮询直至端口开始接受 TCP 连接，超时抛出。"""
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise WireGuardProxyError(
        f"wireproxy SOCKS5 未在 {timeout}s 内就绪: {host}:{port}"
    )


def _build_wireproxy_config(wg_conf_path: str, socks5_port: int) -> str:
    """写入临时 wireproxy 配置 = 原 NordVPN conf + ``[Socks5]`` 段。

    会剥离原文件里可能已存在的 ``[Socks5]``/``[HTTP]`` 段，避免重复绑定。
    返回临时文件路径，调用方负责删除。
    """
    original = Path(wg_conf_path).read_text(encoding="utf-8")
    kept: list[str] = []
    skipping = False
    for line in original.splitlines(keepends=True):
        stripped = line.strip().lower()
        if stripped in ("[socks5]", "[http]"):
            skipping = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = False
        if not skipping:
            kept.append(line)
    cleaned = "".join(kept).rstrip() + "\n\n" + _SOCKS5_SECTION.format(port=socks5_port)

    fd, temp_path = tempfile.mkstemp(suffix=".conf", prefix="nordwg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return temp_path


def _spawn_wireproxy(
    conf_path: str,
    port: int,
    wireproxy_exe: str,
    connect_timeout: float,
) -> WireGuardProxy:
    """生成临时 conf、启动 wireproxy 进程、等待 SOCKS5 就绪。"""
    temp_conf = _build_wireproxy_config(conf_path, port)
    try:
        process = subprocess.Popen(
            [wireproxy_exe, "-c", temp_conf],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, ValueError) as exc:
        try:
            os.unlink(temp_conf)
        except OSError:
            pass
        raise WireGuardProxyError(
            f"无法启动 wireproxy（exe={wireproxy_exe!r}）: {exc}"
        ) from exc

    try:
        _wait_for_port("127.0.0.1", port, timeout=connect_timeout)
        if process.poll() is not None:
            raise WireGuardProxyError(
                f"wireproxy 在 SOCKS5 就绪检查后已退出: exit={process.returncode}"
            )
    except WireGuardProxyError:
        _terminate_process(process)
        try:
            os.unlink(temp_conf)
        except OSError:
            pass
        raise

    return WireGuardProxy(
        port=port,
        process=process,
        conf_path=conf_path,
        temp_conf=temp_conf,
        proxy_url=f"socks5://127.0.0.1:{port}",
    )


def _terminate_process(process: subprocess.Popen) -> None:
    """优雅终止 wireproxy 进程，超时则强杀。"""
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    except Exception as exc:  # noqa: BLE001 - 清理路径吞掉一切非致命异常
        logger.warning("wireproxy 进程终止异常: %s", exc)


def _is_process_alive(pid: int) -> bool:
    """Return whether a recorded owner process is still present."""
    process_id = int(pid or 0)
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, process_id)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except (AttributeError, OSError):
            return False
    try:
        os.kill(process_id, 0)
    except (OSError, ValueError, ProcessLookupError):
        return False
    return True


def _terminate_process_by_pid(pid: object) -> None:
    """Stop a stale wireproxy process recorded by a previous application run."""
    try:
        process_id = int(pid or 0)
    except (TypeError, ValueError):
        return
    if process_id <= 0 or process_id == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            os.kill(process_id, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        logger.debug("清理过期 wireproxy 进程失败: pid=%s error=%s", process_id, exc)


class WireGuardProxyPool:
    """线程安全的“每注册一个隧道”的 WireGuard → SOCKS5 代理池。"""

    def __init__(
        self,
        configs_dir: "str | os.PathLike[str]",
        wireproxy_exe: str,
        port_start: int = 25000,
        port_end: int = 25099,
        connect_timeout: float = 10.0,
        lease_store: NordVPNWireGuardLeaseStore | None = None,
    ) -> None:
        self._configs_dir = Path(configs_dir)
        self._wireproxy_exe = wireproxy_exe
        self._resolved_wireproxy_exe: str | None = None
        self._port_start = int(port_start)
        self._port_end = int(port_end)
        self._connect_timeout = float(connect_timeout)
        self._lease_store = lease_store or NordVPNWireGuardLeaseStore()
        self._lock = threading.Lock()
        self._used_ports: set[int] = set()
        self._active: dict[int, WireGuardProxy] = {}
        self._active_sources: set[str] = set()
        self._active_egress_ips: set[str] = set()
        self._reap_stale_leases()

    def _wireproxy_executable(self) -> str:
        with self._lock:
            if self._resolved_wireproxy_exe:
                return self._resolved_wireproxy_exe
        from core.wireproxy_runtime import resolve_wireproxy_executable

        resolved = resolve_wireproxy_executable(self._wireproxy_exe)
        with self._lock:
            self._resolved_wireproxy_exe = resolved
        return resolved

    def list_configs(self, country_filter: str | None = None) -> list[Path]:
        """列出可用的 NordVPN ``.conf``，可选按国家过滤。"""
        return scan_wg_configs(self._configs_dir, country_filter)

    def acquire(
        self,
        country_filter: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> WireGuardProxy:
        """为随机选中的 NordVPN 配置启动一个 wireproxy 实例。

        Args:
            country_filter: 可选国家代码子串，如 ``"us"``、``"jp"``。

        Returns:
            已就绪、可用的 :class:`WireGuardProxy`。

        Raises:
            WireGuardProxyError: 无可用配置、端口耗尽或 wireproxy 启动失败。
        """
        return self._acquire_from_candidates(
            self._candidate_configs(country_filter),
            owner_id=owner_id,
            country_filter=country_filter,
        )

    def _acquire_from_candidates(
        self,
        candidates: list[Path],
        owner_id: str | None,
        country_filter: str | None,
    ) -> WireGuardProxy:
        owner = self._normalize_owner_id(owner_id)
        existing = self._lease_store.get_owner_lease(owner)
        if existing is not None:
            raise WireGuardProxyError(
                f"profile/owner 已有活跃 NordVPN 隧道: {owner} → "
                f"{existing.get('source_label')}"
            )
        last_error: Exception | None = None
        proxy: WireGuardProxy | None = None
        for conf_path in candidates:
            source_label = _normalize_source_label(conf_path.name)
            port = self._allocate_port()
            lease_id = self._try_claim(
                owner_id=owner,
                source_label=source_label,
                port=port,
                conf_path=str(conf_path),
            )
            if lease_id is None:
                self._free_port(port)
                continue
            try:
                proxy = _spawn_wireproxy(
                    conf_path=str(conf_path),
                    port=port,
                    wireproxy_exe=self._wireproxy_executable(),
                    connect_timeout=self._connect_timeout,
                )
                self._set_proxy_metadata(
                    proxy,
                    lease_id=lease_id,
                    owner_id=owner,
                    source_label=source_label,
                )
                self._activate_proxy(proxy)
                break
            except Exception as exc:  # noqa: BLE001 - rollback every failed candidate before retry
                self._rollback_acquire(
                    proxy,
                    port,
                    source_label,
                    lease_id=lease_id,
                )
                proxy = None
                last_error = exc
                continue
        if proxy is None:
            if last_error is not None:
                raise last_error
            suffix = f"（filter={country_filter!r}）" if country_filter else ""
            raise WireGuardProxyError(
                f"WireGuard 配置目录无可用 .conf 文件: {self._configs_dir}{suffix}"
            )
        logger.info(
            "WireGuard 代理已启动: source=%s port=%d pid=%s tunnel_ip=%s",
            proxy.source_label,
            proxy.port,
            proxy.pid,
            proxy.tunnel_egress_ip,
        )
        return proxy

    def acquire_from_conf_text(
        self,
        conf_text: str,
        source_label: str = "nordvpn-account",
        *,
        owner_id: str | None = None,
        server_country: str | None = None,
        server_load: int | None = None,
    ) -> WireGuardProxy:
        """启动由 NordVPN access token 动态生成的 WireGuard 配置。"""
        text = str(conf_text or "").strip()
        if "[Interface]" not in text or "[Peer]" not in text:
            raise WireGuardProxyError("动态 WireGuard 配置缺少 [Interface] 或 [Peer]")
        owner = self._normalize_owner_id(owner_id)
        source_label = _normalize_source_label(source_label)
        existing = self._lease_store.get_owner_lease(owner)
        if existing is not None:
            raise WireGuardProxyError(
                f"profile/owner 已有活跃 NordVPN 隧道: {owner} → "
                f"{existing.get('source_label')}"
            )
        fd, source_temp_conf = tempfile.mkstemp(suffix=".conf", prefix="nordwg_account_")
        port: int | None = None
        lease_id: str | None = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text.rstrip() + "\n")
            port = self._allocate_port()
            lease_id = self._try_claim(
                owner_id=owner,
                source_label=source_label,
                port=port,
                conf_path=source_temp_conf,
            )
            if lease_id is None:
                self._free_port(port)
                raise WireGuardProxyError(
                    f"NordVPN server/config 已被活跃隧道占用: {source_label}"
                )
            proxy = _spawn_wireproxy(
                conf_path=source_temp_conf,
                port=port,
                wireproxy_exe=self._wireproxy_executable(),
                connect_timeout=self._connect_timeout,
            )
            proxy.source_temp_conf = source_temp_conf
            proxy.server_country = server_country
            proxy.server_load = server_load
            self._set_proxy_metadata(
                proxy,
                lease_id=lease_id,
                owner_id=owner,
                source_label=source_label,
            )
            self._activate_proxy(proxy)
            self._lease_store.update_lease(
                lease_id,
                conf_path=source_temp_conf,
                source_temp_conf=source_temp_conf,
                server_country=server_country,
                server_load=server_load,
            )
        except Exception:
            self._rollback_acquire(
                locals().get("proxy"), port, source_label,
                lease_id=lease_id,
                source_temp_conf=source_temp_conf,
            )
            raise

        logger.info(
            "WireGuard 动态代理已启动: source=%s port=%d pid=%s tunnel_ip=%s",
            source_label,
            proxy.port,
            proxy.pid,
            proxy.tunnel_egress_ip,
        )
        return proxy

    def _reserve_source(self, source_label: str) -> None:
        """Keep the legacy in-process marker for diagnostics and existing callers."""
        source_label = _normalize_source_label(source_label)
        with self._lock:
            if source_label in self._active_sources:
                raise WireGuardProxyError(
                    f"NordVPN server/config 已被活跃隧道占用: {source_label}"
                )
            self._active_sources.add(source_label)

    @staticmethod
    def _normalize_owner_id(owner_id: str | None) -> str:
        value = str(owner_id or "").strip()
        return value or f"anonymous:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4()}"

    def _try_claim(
        self,
        *,
        owner_id: str,
        source_label: str,
        port: int,
        conf_path: str,
    ) -> str | None:
        source_label = _normalize_source_label(source_label)
        lease_id = str(uuid.uuid4())
        claimed = self._lease_store.try_claim(
            lease_id=lease_id,
            owner_id=owner_id,
            source_label=source_label,
            local_port=port,
            proxy_url=f"socks5://127.0.0.1:{port}",
            conf_path=conf_path,
            owner_pid=os.getpid(),
            owner_thread_id=threading.get_ident(),
            acquired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if claimed is not None:
            with self._lock:
                self._active_sources.add(source_label)
        return claimed

    def _set_proxy_metadata(
        self,
        proxy: WireGuardProxy,
        *,
        lease_id: str,
        owner_id: str,
        source_label: str,
    ) -> None:
        proxy.lease_id = lease_id
        proxy.owner_id = owner_id
        proxy.source_label = source_label
        proxy.pool = self
        self._lease_store.update_lease(
            lease_id,
            wireproxy_pid=proxy.pid,
        )

    def _reap_stale_leases(self) -> None:
        stale = self._lease_store.cleanup_stale(_is_process_alive)
        for row in stale:
            _terminate_process_by_pid(row.get("wireproxy_pid"))
            for path in (row.get("conf_path"), row.get("source_temp_conf")):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def active_source_labels(self) -> set[str]:
        self._reap_stale_leases()
        with self._lock:
            local = set(self._active_sources)
        return local | self._lease_store.active_source_labels()

    def bind_profile(self, proxy: WireGuardProxy, profile_id: str) -> None:
        value = str(profile_id or "").strip()
        if not value or not proxy.lease_id:
            return
        if not self._lease_store.update_lease(proxy.lease_id, profile_id=value):
            raise WireGuardProxyError(
                f"Browser profile/session 已绑定其它 NordVPN 隧道: {value}"
            )
        proxy.profile_id = value
        logger.info(
            "NordVPN WireGuard lease 已绑定 browser profile/session: profile=%s source=%s port=%s",
            value,
            proxy.source_label,
            proxy.port,
        )

    def _activate_proxy(self, proxy: WireGuardProxy) -> None:
        from core.registration_network_identity import (
            NetworkIdentityError,
            probe_socks_public_ip,
        )

        try:
            tunnel_ip = probe_socks_public_ip(proxy.proxy_url, timeout=15.0, retries=3)
        except NetworkIdentityError as exc:
            # A listening SOCKS port is not enough: wireproxy can stay alive while
            # its WireGuard handshake or route is unusable. Fail here so the
            # caller can discard this server and try another candidate.
            logger.warning(
                "WireGuard 代理不可用，丢弃隧道: port=%d error=%s",
                proxy.port,
                str(exc)[:150],
            )
            raise WireGuardProxyError(
                f"WireGuard SOCKS5 egress probe failed: {str(exc)[:200]}"
            ) from exc

        with self._lock:
            if tunnel_ip and tunnel_ip in self._active_egress_ips:
                raise WireGuardProxyError(
                    f"NordVPN 活跃隧道出口 IP 重复: {tunnel_ip}"
                )
            proxy.tunnel_egress_ip = tunnel_ip
            if tunnel_ip:
                self._active_egress_ips.add(tunnel_ip)
            self._active[proxy.port] = proxy
        if proxy.lease_id and not self._lease_store.update_lease(
            proxy.lease_id,
            tunnel_egress_ip=tunnel_ip,
        ):
            with self._lock:
                self._active.pop(proxy.port, None)
                self._active_sources.discard(str(proxy.source_label or ""))
                if tunnel_ip:
                    self._active_egress_ips.discard(tunnel_ip)
            raise WireGuardProxyError(
                f"NordVPN 活跃隧道出口 IP 重复: {tunnel_ip}"
            )

    def _rollback_acquire(
        self,
        proxy: WireGuardProxy | None,
        port: int | None,
        source_label: str,
        *,
        lease_id: str | None = None,
        source_temp_conf: str | None = None,
    ) -> None:
        if proxy is not None:
            _terminate_process(proxy.process)
            for path in (proxy.temp_conf, proxy.source_temp_conf):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        elif source_temp_conf:
            try:
                os.unlink(source_temp_conf)
            except OSError:
                pass
        with self._lock:
            if port is not None:
                self._used_ports.discard(port)
                self._active.pop(port, None)
            self._active_sources.discard(source_label)
            if proxy and proxy.tunnel_egress_ip:
                self._active_egress_ips.discard(proxy.tunnel_egress_ip)
        self._lease_store.release(lease_id or getattr(proxy, "lease_id", None))

    def release(self, proxy: WireGuardProxy) -> None:
        """停止 wireproxy 进程、删除临时 conf、归还端口。"""
        _terminate_process(proxy.process)
        try:
            os.unlink(proxy.temp_conf)
        except OSError:
            pass
        if proxy.source_temp_conf:
            try:
                os.unlink(proxy.source_temp_conf)
            except OSError:
                pass
        with self._lock:
            self._used_ports.discard(proxy.port)
            self._active.pop(proxy.port, None)
            if proxy.source_label:
                self._active_sources.discard(proxy.source_label)
            if proxy.tunnel_egress_ip:
                self._active_egress_ips.discard(proxy.tunnel_egress_ip)
        self._lease_store.release(proxy.lease_id)
        logger.debug("WireGuard 代理已释放: port=%d", proxy.port)

    def release_all(self) -> None:
        """停止全部活跃 wireproxy 实例（进程退出/关停时调用）。"""
        with self._lock:
            proxies = list(self._active.values())
        for proxy in proxies:
            self.release(proxy)

    def _candidate_configs(self, country_filter: str | None) -> list[Path]:
        active_sources = self.active_source_labels()
        configs = [
            path for path in self.list_configs(country_filter)
            if _normalize_source_label(path.name) not in active_sources
        ]
        if not configs:
            suffix = f"（filter={country_filter!r}）" if country_filter else ""
            raise WireGuardProxyError(
                f"WireGuard 配置目录无可用 .conf 文件: {self._configs_dir}{suffix}"
            )
        random.shuffle(configs)
        return configs

    def _pick_config(self, country_filter: str | None) -> Path:
        return random.choice(self._candidate_configs(country_filter))

    def _allocate_port(self) -> int:
        active_ports = self._lease_store.active_local_ports()
        with self._lock:
            for port in range(self._port_start, self._port_end + 1):
                if port in self._used_ports or port in active_ports:
                    continue
                if _is_port_free(port):
                    self._used_ports.add(port)
                    return port
        raise WireGuardProxyError(
            f"SOCKS5 端口池已耗尽: [{self._port_start}, {self._port_end}]"
        )

    def _free_port(self, port: int) -> None:
        with self._lock:
            self._used_ports.discard(port)

    def status(self) -> dict[str, object]:
        self._reap_stale_leases()
        return {
            "active_count": len(self._lease_store.list_active()),
            "leases": self._lease_store.list_active(),
        }


_POOL: WireGuardProxyPool | None = None
_POOL_LOCK = threading.Lock()


def get_pool() -> WireGuardProxyPool:
    """返回按当前配置构建的全局代理池（配置变动时重建）。"""
    global _POOL
    configs_dir = str(_cfg_attr("NORDVPN_WG_CONFIGS_DIR", "") or "")
    wireproxy_exe = str(_cfg_attr("NORDVPN_WG_WIREPROXY_EXE", "wireproxy") or "wireproxy")
    port_start = int(_cfg_attr("NORDVPN_WG_PORT_START", 25000) or 25000)
    port_end = int(_cfg_attr("NORDVPN_WG_PORT_END", 25099) or 25099)
    connect_timeout = float(_cfg_attr("NORDVPN_WG_CONNECT_TIMEOUT", 10.0) or 10.0)

    with _POOL_LOCK:
        needs_rebuild = (
            _POOL is None
            or _POOL._configs_dir != Path(configs_dir)
            or _POOL._wireproxy_exe != wireproxy_exe
            or _POOL._port_start != port_start
            or _POOL._port_end != port_end
            or _POOL._connect_timeout != connect_timeout
        )
        if needs_rebuild:
            if _POOL is not None:
                _POOL.release_all()
            _POOL = WireGuardProxyPool(
                configs_dir=configs_dir,
                wireproxy_exe=wireproxy_exe,
                port_start=port_start,
                port_end=port_end,
                connect_timeout=connect_timeout,
            )
        return _POOL


def is_per_profile_proxy_enabled() -> bool:
    """Return whether the operator enabled per-profile WireGuard proxies."""
    return bool(_cfg_attr("NORDVPN_WG_ENABLED", False))


@contextmanager
def proxy_for_registration(
    country_filter: str | None = None,
    pool: WireGuardProxyPool | None = None,
    *,
    owner_id: str | None = None,
) -> Iterator[str | None]:
    """产出可传给 Roxy Browser 的 SOCKS5 代理 URL，退出时释放隧道。

    未启用（``NORDVPN_WG_ENABLED=False``）时产出 ``None``，调用方据此回退到
    原有代理来源，因此可以无副作用地包裹注册流程::

        with proxy_for_registration() as proxy_url:
            run_roxy_registration(..., proxy=proxy_url, ...)
    """
    if not is_per_profile_proxy_enabled():
        yield None
        return

    if country_filter is None:
        country_filter = str(_cfg_attr("NORDVPN_WG_COUNTRY_FILTER", "") or "").strip() or None

    active_pool = pool or get_pool()
    from core.nordvpn_account import get_account_client

    account_client = get_account_client()
    last_error: Exception | None = None
    proxy: WireGuardProxy | None = None
    pool_owner_kwargs = {"owner_id": owner_id} if owner_id is not None else {}
    for _attempt in range(3):
        try:
            if account_client.configured:
                active_sources = active_pool.active_source_labels()
                if not isinstance(active_sources, (set, frozenset, list, tuple)):
                    active_sources = set()
                if active_sources:
                    config = account_client.build_config(
                        country_filter,
                        excluded_hostnames=active_sources,
                    )
                else:
                    config = account_client.build_config(country_filter)
                server_country = getattr(
                    config.server,
                    "country_code",
                    getattr(config.server, "country", None),
                )
                server_load = getattr(config.server, "load", None)
                acquire_kwargs = dict(pool_owner_kwargs)
                proxy = active_pool.acquire_from_conf_text(
                    config.text,
                    source_label=config.server.hostname,
                    **acquire_kwargs,
                    server_country=server_country if isinstance(server_country, str) else None,
                    server_load=server_load if isinstance(server_load, int) else None,
                )
            else:
                proxy = active_pool.acquire(country_filter, **pool_owner_kwargs)
            break
        except WireGuardProxyError as exc:
            last_error = exc
            logger.warning("NordVPN 隧道候选不可用，准备重试: %s", str(exc)[:180])
    if proxy is None:
        raise WireGuardProxyError(
            f"连续 3 次无法建立独立 NordVPN 隧道: {last_error}"
        ) from last_error
    try:
        yield RegistrationProxy(proxy)
    finally:
        active_pool.release(proxy)


def list_active_leases(
    lease_store: NordVPNWireGuardLeaseStore | None = None,
) -> list[dict]:
    """Return persisted active leases, reaping owners from dead processes first."""
    store = lease_store or NordVPNWireGuardLeaseStore()
    stale = store.cleanup_stale(_is_process_alive)
    for row in stale:
        _terminate_process_by_pid(row.get("wireproxy_pid"))
        for path in (row.get("conf_path"), row.get("source_temp_conf")):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return store.list_active()
