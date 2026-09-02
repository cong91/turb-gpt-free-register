"""Launch a copied profile with the installed RoxyChrome core."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class RoxyProfileLauncherError(RuntimeError):
    """The local RoxyChrome process could not be started or stopped."""


@dataclass(frozen=True)
class RoxyLocalLaunch:
    profile_path: Path
    executable: Path
    pid: int
    debugger_address: str
    process_started_at: str
    capability: str = "browser_state_only"
    fingerprint_status: str = "unknown"
    signature_sha256: str = ""


def _executable_core_version(executable: Path) -> str:
    escaped = str(executable).replace("'", "''")
    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"(Get-Item -LiteralPath '{escaped}' -ErrorAction Stop).VersionInfo.ProductVersion",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\d+", result.stdout.strip())
    return match.group(0) if match else ""


def _validated_core_version(executable: Path) -> str:
    value = _executable_core_version(executable)
    match = re.search(r"\d+", value)
    return match.group(0) if match else ""


def find_roxy_chrome(
    configured_path: str = "",
    *,
    core_version: str = "",
    allow_version_mismatch: bool = False,
) -> Path:
    candidates = []
    if configured_path:
        configured = Path(configured_path).expanduser()
        if not configured.is_file() or configured.is_symlink():
            raise RoxyProfileLauncherError("Configured RoxyChrome.exe was not found")
        resolved = configured.resolve()
        actual_version = _validated_core_version(resolved) if core_version else ""
        expected_version = re.search(r"\d+", str(core_version or ""))
        if (
            core_version
            and actual_version != (expected_version.group(0) if expected_version else "")
            and not allow_version_mismatch
        ):
            raise RoxyProfileLauncherError(
                f"Configured RoxyChrome core version does not match {core_version}"
            )
        return resolved
    appdata = os.environ.get("APPDATA", "")
    if appdata and core_version:
        candidates.append(Path(appdata) / "RoxyBrowser" / "chrome-bin" / core_version / "RoxyChrome.exe")
    if appdata:
        root = Path(appdata) / "RoxyBrowser" / "chrome-bin"
        if root.is_dir():
            candidates.extend(sorted(root.glob("*/RoxyChrome.exe"), reverse=True))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve()
            actual_version = _validated_core_version(resolved) if core_version else ""
            expected_version = re.search(r"\d+", str(core_version or ""))
            if (
                core_version
                and actual_version != (expected_version.group(0) if expected_version else "")
                and not allow_version_mismatch
            ):
                continue
            return resolved
    if core_version and not allow_version_mismatch:
        raise RoxyProfileLauncherError(
            f"RoxyChrome core version {core_version} was not found"
        )
    raise RoxyProfileLauncherError("RoxyChrome.exe was not found")


def allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_command(executable: Path, profile_path: Path, port: int, *, headless: bool = False) -> list[str]:
    command = [
        str(executable),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile_path}",
        "--password-store=basic",
        "--lang=en",
        "--disable-dev-shm-usage",
        "about:blank",
    ]
    if headless:
        command.insert(-1, "--headless=new")
    return command


def wait_for_cdp(port: int, *, timeout: float = 20.0) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            web_socket = str(payload.get("webSocketDebuggerUrl") or "")
            if web_socket or payload.get("Browser"):
                return f"127.0.0.1:{port}", web_socket
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.2)
    raise RoxyProfileLauncherError("RoxyChrome CDP readiness timed out")


def _cdp_command(web_socket: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not web_socket:
        return {}
    try:
        import websocket
    except ImportError:
        return {}
    connection = websocket.create_connection(
        web_socket,
        timeout=3,
        origin="http://127.0.0.1",
        suppress_origin=True,
    )
    try:
        connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        for _ in range(20):
            response = json.loads(connection.recv())
            if response.get("id") == 1:
                return response.get("result") or {}
    finally:
        connection.close()
    return {}


def _page_web_socket(debugger_address: str) -> str:
    host, _, raw_port = debugger_address.partition(":")
    if host != "127.0.0.1" or not raw_port.isdigit():
        return ""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(raw_port)}/json",
            timeout=2,
        ) as response:
            pages = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return ""
    if not isinstance(pages, list):
        return ""
    for page in pages[:20]:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        web_socket = str(page.get("webSocketDebuggerUrl") or "")
        parsed = urlsplit(web_socket)
        if parsed.scheme in {"ws", "wss"} and parsed.hostname in {"127.0.0.1", "localhost"}:
            return web_socket
    return ""


def capture_signature_from_debugger(
    debugger_address: str,
    *,
    official_signature_sha256: str = "",
) -> tuple[str, str]:
    host, _, raw_port = str(debugger_address or "").partition(":")
    if host != "127.0.0.1" or not raw_port.isdigit():
        return "", "unknown"
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(raw_port)}/json/version",
            timeout=2,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return "", "unknown"
    web_socket = str(payload.get("webSocketDebuggerUrl") or "")
    parsed = urlsplit(web_socket)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1", "localhost",
    }:
        return "", "unknown"
    return capture_signature(
        web_socket,
        debugger_address=debugger_address,
        official_signature_sha256=official_signature_sha256,
    )


def capture_signature(
    web_socket: str,
    *,
    debugger_address: str = "",
    official_signature_sha256: str = "",
) -> tuple[str, str]:
    try:
        result = _cdp_command(web_socket, "SystemInfo.getInfo")
        gpu = result.get("gpu") if isinstance(result.get("gpu"), dict) else {}
        devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
        page_result = {}
        page_socket = _page_web_socket(debugger_address) if debugger_address else ""
        if page_socket:
            page_result = _cdp_command(
                page_socket,
                "Runtime.evaluate",
                {
                    "returnByValue": True,
                    "expression": """
                        (() => {
                          const canvas = document.createElement('canvas');
                          const gl = canvas.getContext('webgl');
                          const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
                          return {
                            platform: navigator.platform || '',
                            language: navigator.language || '',
                            languages: Array.from(navigator.languages || []).slice(0, 8),
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                            screen: [screen.width, screen.height, screen.colorDepth, devicePixelRatio],
                            webglVendor: gl && ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : '',
                            webglRenderer: gl && ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : ''
                          };
                        })()
                    """,
                },
            )
        runtime_result = page_result.get("result") if isinstance(page_result, dict) else {}
        browser_state = runtime_result.get("value") if isinstance(runtime_result, dict) else {}
        if not isinstance(browser_state, dict):
            browser_state = {}
        signature = {
            "model_name": result.get("modelName"),
            "model_version": result.get("modelVersion"),
            "gpu_devices": [
                {
                    "vendor_id": item.get("vendorId"),
                    "device_id": item.get("deviceId"),
                    "vendor_string": item.get("vendorString"),
                    "device_string": item.get("deviceString"),
                }
                for item in devices[:4]
                if isinstance(item, dict)
            ],
            "browser_state": {
                key: browser_state.get(key)
                for key in (
                    "platform", "language", "languages", "timezone", "screen",
                    "webglVendor", "webglRenderer",
                )
            },
        }
        encoded = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest()
        expected = str(official_signature_sha256 or "").strip().lower()
        if not expected:
            return actual, "unknown"
        return actual, "matched" if actual == expected else "mismatched"
    except Exception:  # noqa: BLE001
        return "", "unknown"


def _process_started_at(pid: int) -> str:
    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"(Get-Process -Id {int(pid)} -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o')",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
        value = result.stdout.strip()
        if value:
            return value
    except (OSError, subprocess.SubprocessError) as exc:
        raise RoxyProfileLauncherError(
            "Unable to verify offline RoxyChrome process start time"
        ) from exc
    raise RoxyProfileLauncherError(
        "Unable to verify offline RoxyChrome process start time"
    )


def _tracked_process_matches(launch: RoxyLocalLaunch) -> bool:
    executable = str(launch.executable).replace("'", "''")
    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(launch.pid)}\"; "
            "if(-not $p){exit 2}; "
            "$e=$p.ExecutablePath; "
            f"$s=(Get-Process -Id {int(launch.pid)} -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o'); "
            f"if($e -ne '{executable}' -or $s -ne '{str(launch.process_started_at).replace(chr(39), chr(39) * 2)}'){{exit 3}}"
        ),
    ]
    try:
        subprocess.run(command, capture_output=True, timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def launch_offline(
    profile_path: str | Path,
    *,
    executable: str = "",
    core_version: str = "",
    port: int | None = None,
    timeout: float = 20.0,
    headless: bool = False,
    allow_version_mismatch: bool = False,
    official_signature_sha256: str = "",
) -> RoxyLocalLaunch:
    path = Path(profile_path).resolve(strict=True)
    if not (path / "Default" / "Preferences").is_file():
        raise RoxyProfileLauncherError("Offline profile lacks Default/Preferences")
    binary = find_roxy_chrome(
        executable,
        core_version=core_version,
        allow_version_mismatch=allow_version_mismatch,
    )
    selected_port = int(port or allocate_port())
    process = subprocess.Popen(build_command(binary, path, selected_port, headless=headless), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        address, web_socket = wait_for_cdp(selected_port, timeout=timeout)
        signature_sha256, fingerprint_status = capture_signature(
            web_socket,
            debugger_address=address,
            official_signature_sha256=official_signature_sha256,
        )
        process_started_at = _process_started_at(process.pid)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise
    return RoxyLocalLaunch(
        path,
        binary,
        process.pid,
        address,
        process_started_at,
        fingerprint_status=fingerprint_status,
        signature_sha256=signature_sha256,
    )


def stop_offline(launch: RoxyLocalLaunch, *, timeout: float = 10.0) -> None:
    if not _tracked_process_matches(launch):
        raise RoxyProfileLauncherError("Tracked offline process identity does not match")
    try:
        process = subprocess.Popen(["taskkill", "/PID", str(launch.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return_code = process.wait(timeout=timeout)
        if return_code not in {0, None}:
            raise RoxyProfileLauncherError(
                f"Unable to stop offline RoxyChrome (taskkill exit {return_code})"
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoxyProfileLauncherError("Unable to stop offline RoxyChrome") from exc
