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
import logging
import os
import random
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows 下隐藏 wireproxy 子进程窗口；其它平台该标志不存在，取 0 表示无特殊标志。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SOCKS5_SECTION = "[Socks5]\nBindAddress = 127.0.0.1:{port}\n"


class WireGuardProxyError(RuntimeError):
    """WireGuard/wireproxy 代理相关的领域错误。"""


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


class WireGuardProxyPool:
    """线程安全的“每注册一个隧道”的 WireGuard → SOCKS5 代理池。"""

    def __init__(
        self,
        configs_dir: "str | os.PathLike[str]",
        wireproxy_exe: str,
        port_start: int = 25000,
        port_end: int = 25099,
        connect_timeout: float = 10.0,
    ) -> None:
        self._configs_dir = Path(configs_dir)
        self._wireproxy_exe = wireproxy_exe
        self._resolved_wireproxy_exe: str | None = None
        self._port_start = int(port_start)
        self._port_end = int(port_end)
        self._connect_timeout = float(connect_timeout)
        self._lock = threading.Lock()
        self._used_ports: set[int] = set()
        self._active: dict[int, WireGuardProxy] = {}

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

    def acquire(self, country_filter: str | None = None) -> WireGuardProxy:
        """为随机选中的 NordVPN 配置启动一个 wireproxy 实例。

        Args:
            country_filter: 可选国家代码子串，如 ``"us"``、``"jp"``。

        Returns:
            已就绪、可用的 :class:`WireGuardProxy`。

        Raises:
            WireGuardProxyError: 无可用配置、端口耗尽或 wireproxy 启动失败。
        """
        conf_path = self._pick_config(country_filter)
        port = self._allocate_port()
        try:
            proxy = _spawn_wireproxy(
                conf_path=str(conf_path),
                port=port,
                wireproxy_exe=self._wireproxy_executable(),
                connect_timeout=self._connect_timeout,
            )
        except Exception:
            with self._lock:
                self._used_ports.discard(port)
            raise
        with self._lock:
            self._active[port] = proxy
        logger.info(
            "WireGuard 代理已启动: %s → socks5://127.0.0.1:%d",
            conf_path.name,
            port,
        )
        return proxy

    def acquire_from_conf_text(
        self,
        conf_text: str,
        source_label: str = "nordvpn-account",
    ) -> WireGuardProxy:
        """启动由 NordVPN access token 动态生成的 WireGuard 配置。"""
        text = str(conf_text or "").strip()
        if "[Interface]" not in text or "[Peer]" not in text:
            raise WireGuardProxyError("动态 WireGuard 配置缺少 [Interface] 或 [Peer]")
        fd, source_temp_conf = tempfile.mkstemp(suffix=".conf", prefix="nordwg_account_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text.rstrip() + "\n")
            port = self._allocate_port()
            try:
                proxy = _spawn_wireproxy(
                    conf_path=source_temp_conf,
                    port=port,
                    wireproxy_exe=self._wireproxy_executable(),
                    connect_timeout=self._connect_timeout,
                )
            except Exception:
                with self._lock:
                    self._used_ports.discard(port)
                raise
        except Exception:
            try:
                os.unlink(source_temp_conf)
            except OSError:
                pass
            raise

        proxy.source_temp_conf = source_temp_conf
        with self._lock:
            self._active[port] = proxy
        logger.info(
            "WireGuard 动态代理已启动: source=%s → socks5://127.0.0.1:%d",
            source_label,
            port,
        )
        return proxy

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
        logger.debug("WireGuard 代理已释放: port=%d", proxy.port)

    def release_all(self) -> None:
        """停止全部活跃 wireproxy 实例（进程退出/关停时调用）。"""
        with self._lock:
            proxies = list(self._active.values())
        for proxy in proxies:
            self.release(proxy)

    def _pick_config(self, country_filter: str | None) -> Path:
        configs = self.list_configs(country_filter)
        if not configs:
            suffix = f"（filter={country_filter!r}）" if country_filter else ""
            raise WireGuardProxyError(
                f"WireGuard 配置目录无可用 .conf 文件: {self._configs_dir}{suffix}"
            )
        return random.choice(configs)

    def _allocate_port(self) -> int:
        with self._lock:
            for port in range(self._port_start, self._port_end + 1):
                if port in self._used_ports:
                    continue
                if _is_port_free(port):
                    self._used_ports.add(port)
                    return port
        raise WireGuardProxyError(
            f"SOCKS5 端口池已耗尽: [{self._port_start}, {self._port_end}]"
        )


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
            or str(_POOL._configs_dir) != configs_dir
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
    """Return whether token mode or manual WireGuard proxy mode is configured."""
    from config import nordvpn_account as _account_cfg

    access_token = str(getattr(_account_cfg, "NORDVPN_ACCESS_TOKEN", "") or "").strip()
    return bool(access_token) or bool(_cfg_attr("NORDVPN_WG_ENABLED", False))


@contextmanager
def proxy_for_registration(
    country_filter: str | None = None,
    pool: WireGuardProxyPool | None = None,
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
    if account_client.configured:
        config = account_client.build_config(country_filter)
        proxy = active_pool.acquire_from_conf_text(
            config.text,
            source_label=config.server.hostname,
        )
    else:
        proxy = active_pool.acquire(country_filter)
    try:
        yield proxy.proxy_url
    finally:
        active_pool.release(proxy)
