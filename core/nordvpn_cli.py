# -*- coding: utf-8 -*-
"""NordVPN CLI wrapper for the locally installed Windows NordVPN app.

Controls the NordVPN desktop application through its documented
Command Prompt interface (nordvpn -c / nordvpn -d).  All operations
are no-ops unless config.nordvpn.NORDVPN_ENABLED is True.

Usage::

    from core.nordvpn_cli import connect, disconnect, is_connected, VPNContext

    connect()                          # best server
    connect(country_group="Japan")     # specific country
    disconnect()
    is_connected()                    # check NordLynx adapter state

    with VPNContext(country_group="Japan"):
        ...  # VPN connected for this block, disconnected afterwards

Design decisions (mirroring JnmBrowser's per-session IP rotation):
  - Every connect call picks a random server from the configured group,
    giving a different exit IP each time.
  - The VPNContext context manager ensures cleanup (disconnect on exit)
    even when an exception occurs.
  - All public functions are safe to call when NORDVPN_ENABLED=False
    (they return False/None without side effects).
"""
from __future__ import annotations

import ipaddress
import logging
import random
import socket
import subprocess  # nosec B404
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_rotation_status_lock = threading.Lock()
_last_command_error: str | None = None
_last_rotation_error: str | None = None
_last_rotation_detail: str | None = None


class NordVPNError(RuntimeError):
    """Raised when a NordVPN CLI command fails unexpectedly."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cfg_attr(name: str, default=None):
    """Read a config.nordvpn attribute with hot-reload support."""
    from config import nordvpn as _cfg

    return getattr(_cfg, name, default)


def _nordvpn_exe() -> Path:
    """Return the absolute path to NordVPN.exe.

    Raises NordVPNError if the installation directory does not exist
    or NordVPN.exe is missing.
    """
    install_dir = Path(str(_cfg_attr("NORDVPN_INSTALL_DIR", "") or "").strip())
    if not install_dir.is_dir():
        raise NordVPNError(f"NordVPN 安装目录不存在: {install_dir}")
    exe = install_dir / "NordVPN.exe"
    if not exe.is_file():
        raise NordVPNError(f"找不到 NordVPN.exe: {exe}")
    return exe


def _run_nordvpn(*args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a NordVPN command via subprocess and return the result.

    Raises NordVPNError on non-zero exit or timeout.
    """
    if timeout is None:
        timeout = int(_cfg_attr("NORDVPN_CLI_TIMEOUT", 30) or 30)
    exe = _nordvpn_exe()
    cmd = [str(exe), *args]
    logger.info("[NordVPN] 执行命令: %s", " ".join(cmd))
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": max(timeout, 5),
        "cwd": str(exe.parent),
    }
    # Only pass encoding on Python >= 3.11 where it's not redundant.
    try:
        result = subprocess.run(cmd, encoding="utf-8", **kwargs)  # type: ignore[call-overload]
    except TypeError:
        # Python 3.10 compat: encoding was removed from subprocess.run.
        kwargs.pop("encoding", None)  # type: ignore[arg-type]
        result = subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]

    def _log_stream(label: str, text: str | None) -> None:
        if text:
            for line in text.strip().splitlines():
                logger.debug("[NordVPN] %s: %s", label, line)

    _log_stream("stdout", result.stdout)
    _log_stream("stderr", result.stderr)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise NordVPNError(
            f"NordVPN 命令失败 (exit {result.returncode}): {' '.join(cmd)}"
            + (f" — {detail}" if detail else "")
        )
    return result


def _is_port_listening(host: str, port: int) -> bool:
    """Check whether a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _set_last_command_error(message: str | None) -> None:
    global _last_command_error
    with _rotation_status_lock:
        _last_command_error = str(message) if message else None


def _command_error(fallback: str) -> str:
    with _rotation_status_lock:
        return _last_command_error or fallback


def rotation_status_detail() -> dict[str, str | None]:
    """Return the latest automatic-rotation result for durable diagnostics."""
    with _rotation_status_lock:
        return {
            "error": _last_rotation_error,
            "detail": _last_rotation_detail,
        }


def _rotation_failed(message: str) -> bool:
    global _last_rotation_error, _last_rotation_detail
    with _rotation_status_lock:
        _last_rotation_error = str(message)
        _last_rotation_detail = None
    logger.warning("[NordVPN] rotation failed: %s", message)
    return False


def _rotation_succeeded(message: str) -> bool:
    global _last_rotation_error, _last_rotation_detail
    with _rotation_status_lock:
        _last_rotation_error = None
        _last_rotation_detail = str(message)
    return True


def public_ip() -> str | None:
    """Return the current public IP using short, validated fallback requests."""
    urls = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ipinfo.io/ip",
        "https://icanhazip.com",
    )
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as response:  # nosec B310
                value = response.read(128).decode("ascii", errors="ignore").strip()
            ipaddress.ip_address(value)
            return value
        except (OSError, ValueError):
            continue
    return None


def is_service_running() -> bool:
    """Return True if the NordVPN background service appears to be running.

    Checks that the local service port is listening.
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        return False
    host = str(_cfg_attr("NORDVPN_SERVICE_HOST", "127.0.0.1") or "127.0.0.1")
    port = int(_cfg_attr("NORDVPN_SERVICE_PORT", 9247) or 9247)
    return _is_port_listening(host, port)


def is_connected() -> bool:
    """Return True if the NordLynx adapter has an active default route.

    Uses PowerShell Get-NetRoute to check for a 0.0.0.0/0 route
    on the NordLynx interface.  Returns False when NORDVPN_ENABLED
    is False, the adapter is absent, or no default route exists.
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
                "-InterfaceAlias NordLynx -ErrorAction SilentlyContinue "
                "| Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    try:
        count = int((result.stdout or "").strip() or "0")
    except ValueError:
        return False
    return count > 0


def _pick_country_group() -> str | None:
    """Pick a random country group from the configured list.

    Returns None when NORDVPN_COUNTRY_GROUPS is empty (meaning
    connect to the best server without a group filter).
    """
    raw = str(_cfg_attr("NORDVPN_COUNTRY_GROUPS", "") or "").strip()
    if not raw:
        return None
    groups = [g.strip() for g in raw.replace("，", ",").split(",") if g.strip()]
    if not groups:
        return None
    picked = random.choice(groups)
    logger.info("[NordVPN] 随机选择国家分组: %s (from %s)", picked, groups)
    return picked


def connect(country_group: str | None = None) -> bool:
    """Connect to a NordVPN server.

    When *country_group* is None the configured NORDVPN_COUNTRY_GROUPS
    is used (random pick if multiple groups).  An explicit *country_group*
    overrides configuration.

    Returns True on success.  Returns False (no error) when
    NORDVPN_ENABLED is False.
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        logger.debug("[NordVPN] 未启用，跳过 connect")
        return False
    group = country_group if country_group is not None else _pick_country_group()
    args = ["-c"]
    if group:
        args.extend(["-g", group])
    try:
        _run_nordvpn(*args)
    except NordVPNError as exc:
        _set_last_command_error(str(exc))
        logger.exception("[NordVPN] connect 失败")
        return False
    _set_last_command_error(None)
    delay = float(_cfg_attr("NORDVPN_POST_CONNECT_DELAY", 3.0) or 3.0)
    if delay > 0:
        logger.info("[NordVPN] 等待 %.1f 秒让 tunnel 稳定...", delay)
        time.sleep(delay)
    logger.info("[NordVPN] 已连接%s", f" (group={group})" if group else "")
    return True


def disconnect() -> bool:
    """Disconnect from the current NordVPN server.

    Returns True on success (or already disconnected).  Returns False
    (no error) when NORDVPN_ENABLED is False.
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        logger.debug("[NordVPN] 未启用，跳过 disconnect")
        return False
    try:
        _run_nordvpn("-d")
    except NordVPNError as exc:
        _set_last_command_error(str(exc))
        logger.exception("[NordVPN] disconnect 失败")
        return False
    _set_last_command_error(None)
    logger.info("[NordVPN] 已断开")
    return True


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class VPNContext:
    """Context manager that connects to NordVPN on enter and disconnects on exit.

    Mirrors JnmBrowser's per-session IP rotation pattern: each registration
    session gets a fresh VPN connection (and thus a fresh exit IP).  The
    disconnect on exit ensures the machine returns to its pre-VPN state.

    Usage::

        with VPNContext(country_group="Japan"):
            # VPN connected, traffic routes through Japan exit node
            ...
        # VPN disconnected automatically
    """

    def __init__(self, country_group: str | None = None, *,
                 disconnect_on_exit: bool = True):
        """Args:
            country_group: NordVPN group code (e.g. "Japan").
                None uses the configured NORDVPN_COUNTRY_GROUPS.
            disconnect_on_exit: If True (default), disconnect on __exit__.
        """
        self._country_group = country_group
        self._disconnect_on_exit = disconnect_on_exit
        self._was_connected_before = False

    def __enter__(self) -> VPNContext:
        if _cfg_attr("NORDVPN_ENABLED", False):
            self._was_connected_before = is_connected()
            connect(country_group=self._country_group)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._disconnect_on_exit and _cfg_attr("NORDVPN_ENABLED", False):
            try:
                disconnect()
            except Exception:
                logger.warning(
                    "[NordVPN] VPNContext __exit__ disconnect 失败，"
                    "VPN 可能仍在连接状态", exc_info=True,
                )
        return None  # don't suppress exceptions


# ---------------------------------------------------------------------------
# Auto-rotation counter (thread-safe)
# ---------------------------------------------------------------------------

_rotate_counter: int = 0
_rotate_lock = threading.Lock()


def notify_registration_success() -> bool:
    """Call after each successful registration.

    Increments an internal counter. Returns True when the counter
    reaches NORDVPN_AUTO_ROTATE_INTERVAL (meaning it's time to
    rotate IP). The caller is responsible for actually performing
    the rotation and resetting the counter.

    Returns True if threshold reached, False otherwise.
    Safe to call when NORDVPN_ENABLED or NORDVPN_AUTO_ROTATE_ENABLED
    is False (returns False immediately).
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        return False
    from core.nordvpn_wireguard import is_per_profile_proxy_enabled

    if is_per_profile_proxy_enabled():
        return False
    if not _cfg_attr("NORDVPN_AUTO_ROTATE_ENABLED", False):
        return False
    interval = int(_cfg_attr("NORDVPN_AUTO_ROTATE_INTERVAL", 1))
    if interval <= 0:
        return False
    global _rotate_counter
    with _rotate_lock:
        _rotate_counter += 1
        if _rotate_counter < interval:
            logger.debug(
                "[NordVPN] 轮换计数 %d/%d，尚未达到阈值",
                _rotate_counter, interval,
            )
            return False
        _rotate_counter = 0
    logger.info(
        "[NordVPN] 已达轮换阈值 (%d 个账号)，通知 caller 执行 IP 轮换",
        interval,
    )
    return True


def _wait_until(predicate, *, attempts: int = 10, delay: float = 1.0) -> bool:
    for attempt in range(attempts):
        if predicate():
            return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


def _reconnect_and_wait(country: str | None) -> str | None:
    _set_last_command_error(None)
    if not disconnect():
        _rotation_failed(_command_error("NordVPN disconnect failed"))
        return None
    time.sleep(2.0)
    if not connect(country_group=country):
        _rotation_failed(_command_error(f"NordVPN connect failed (group={country or 'best'})"))
        return None
    if not _wait_until(is_service_running):
        _rotation_failed("NordVPN service did not become ready after connect")
        return None
    if not _wait_until(is_connected):
        _rotation_failed("NordLynx default route did not become ready after connect")
        return None
    return public_ip()


def execute_rotation() -> bool:
    """Force NordVPN to reconnect before the next registration starts.

    Mirrors JnmBrowser's system-wide Nord CLI flow: disconnect, wait for
    teardown, connect to the configured group, then verify NordLynx.
    """
    global _last_rotation_error, _last_rotation_detail
    with _rotation_status_lock:
        _last_rotation_error = None
        _last_rotation_detail = None
    country = (
        str(_cfg_attr("NORDVPN_AUTO_ROTATE_COUNTRY_GROUP", "") or "").strip()
        or None
    )
    old_ip = public_ip()
    was_connected = is_connected()
    new_ip = None
    for rotation_attempt in range(2):
        new_ip = _reconnect_and_wait(country)
        if new_ip is None and rotation_status_detail()["error"]:
            return False
        for ip_attempt in range(6):
            if old_ip and new_ip and old_ip != new_ip:
                break
            if ip_attempt < 5:
                time.sleep(2.0)
                new_ip = public_ip()
        if old_ip and new_ip and old_ip != new_ip:
            break
        if rotation_attempt == 0:
            logger.warning(
                "[NordVPN] rotation: public IP 仍为 %s，重新选择一次服务器",
                old_ip or "unknown",
            )
    if not old_ip or not new_ip:
        return _rotation_failed("Public IP could not be read before and after reconnect")
    if old_ip == new_ip:
        return _rotation_failed(f"Public IP did not change ({old_ip})")
    detail = f"NordVPN rotation succeeded: {old_ip} -> {new_ip} (group={country or 'best'})"
    logger.info(
        "[NordVPN] rotation 完成 (was_connected=%s, %s -> %s)",
        was_connected,
        old_ip,
        new_ip,
    )
    return _rotation_succeeded(detail)


# ---------------------------------------------------------------------------
# Preflight helpers


def ensure_vpn_ready() -> bool:
    """Check that the VPN is ready for use.

    Returns True when NordVPN is connected and the service is running.
    Returns False (no error) when NORDVPN_ENABLED is False, the service
    is not running, or no VPN connection is active.

    Call this before starting a registration batch to fail fast if the
    VPN environment is not set up.
    """
    if not _cfg_attr("NORDVPN_ENABLED", False):
        return False
    if not is_service_running():
        logger.warning("[NordVPN] nordvpn-service 未运行")
        return False
    if not is_connected():
        logger.warning("[NordVPN] NordLynx 未连接")
        return False
    logger.info("[NordVPN] VPN 就绪")
    return True
