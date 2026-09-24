"""打包可在 macOS 上双击运行的 WebUI dist。

用法:
    python tools/build_dist.py

产物:
    dist/turb-gpt-free-register-mac/        解包目录（可直接拷贝到 Mac）
    dist/turb-gpt-free-register-mac.zip     交付压缩包

流程: 复制源码（显式白名单，杜绝凭证/运行时数据混入）→ 复制 deploy/dist-mac
下的启动模板并统一 LF 行尾 → 校验 dist 内无敏感文件 → 打 zip 并写入 Unix 执行权限。
"""
from __future__ import annotations

import shutil
import sys
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "deploy" / "dist-mac"
DIST_ROOT = REPO_ROOT / "dist"
STAGING_NAME = "turb-gpt-free-register-mac"
STAGING_DIR = DIST_ROOT / STAGING_NAME
ZIP_PATH = DIST_ROOT / f"{STAGING_NAME}.zip"

# 顶层文件与目录白名单：只带运行 WebUI 所需内容
TOP_LEVEL_FILES = (
    "main.py",
    "web.py",
    "wsgi.py",
    "requirements.txt",
    ".env.example",
    "LICENSE",
    "用于注册的邮箱.txt.example",
)
TOP_LEVEL_DIRS = ("config", "core", "webui", "sentinel")
TEMPLATE_FILES = ("start.command", "stop.command", "README-MAC.md")

EXCLUDED_DIR_NAMES = frozenset(
    {"__pycache__", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache", "run", "logs"}
)
EXCLUDED_FILE_NAMES = frozenset({".env", ".DS_Store", "nul", "webui.pid", "webui.log", "sentinel.config.json"})
EXCLUDED_NAME_MARKERS = (".sqlite3", ".local.", "_dump.html")

# 文本文件统一 LF，避免 Windows 构建机把 CRLF 写进 bash 脚本
TEXT_SUFFIXES = frozenset(
    {".py", ".js", ".txt", ".md", ".json", ".html", ".css", ".cfg", ".toml", ".yml", ".yaml", ".example", ".command", ".sh"}
)
EXECUTABLE_SUFFIXES = frozenset({".command", ".sh"})

# 这些名字绝不能出现在 dist 中（凭证 / 运行时数据 / 环境密钥）
FORBIDDEN_EXACT_NAMES = frozenset(
    {
        ".env",
        "turb.sqlite3",
        "accounts_viewer.html",
        "gmail_cdk_ledger.json",
        "paymesh_card_ledger.json",
        "注册任务.json",
    }
)
FORBIDDEN_NAME_PREFIXES = ("注册成功的", "注册任务.json.backup", "注册日志")


def _is_excluded(name: str) -> bool:
    if name in EXCLUDED_FILE_NAMES or name in FORBIDDEN_EXACT_NAMES:
        return True
    return any(marker in name for marker in EXCLUDED_NAME_MARKERS)


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in TEXT_SUFFIXES or not src.suffix:
        data = src.read_bytes().replace(b"\r\n", b"\n")
        dest.write_bytes(data)
    else:
        shutil.copy2(src, dest)


def _copy_sources() -> int:
    copied = 0
    for name in TOP_LEVEL_FILES:
        src = REPO_ROOT / name
        if not src.is_file():
            raise SystemExit(f"[DIST] 缺少顶层文件: {name}")
        _copy_file(src, STAGING_DIR / name)
        copied += 1

    for dir_name in TOP_LEVEL_DIRS:
        src_dir = REPO_ROOT / dir_name
        if not src_dir.is_dir():
            raise SystemExit(f"[DIST] 缺少源码目录: {dir_name}")
        for src in sorted(src_dir.rglob("*")):
            if src.is_dir():
                continue
            if src.parent.name in EXCLUDED_DIR_NAMES or any(part in EXCLUDED_DIR_NAMES for part in src.parts):
                continue
            if _is_excluded(src.name):
                continue
            _copy_file(src, STAGING_DIR / src.relative_to(REPO_ROOT))
            copied += 1
    return copied


def _copy_templates() -> None:
    for name in TEMPLATE_FILES:
        src = TEMPLATE_DIR / name
        if not src.is_file():
            raise SystemExit(f"[DIST] 缺少模板文件: {TEMPLATE_DIR / name}")
        data = src.read_bytes().replace(b"\r\n", b"\n")
        dest = STAGING_DIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if name.endswith(".command") and not data.startswith(b"#!/bin/bash"):
            raise SystemExit(f"[DIST] 模板缺少 bash shebang: {name}")


def _assert_dist_clean() -> None:
    for path in STAGING_DIR.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        rel = path.relative_to(STAGING_DIR).as_posix()
        if name in FORBIDDEN_EXACT_NAMES:
            raise SystemExit(f"[DIST] 发现禁止打包的文件: {rel}")
        if any(name.startswith(prefix) for prefix in FORBIDDEN_NAME_PREFIXES):
            raise SystemExit(f"[DIST] 发现禁止打包的文件: {rel}")
        if name.endswith(".command") and b"\r" in path.read_bytes():
            raise SystemExit(f"[DIST] .command 文件含 CR 字符（必须纯 LF）: {rel}")


def _write_zip() -> int:
    ZIP_PATH.unlink(missing_ok=True)
    entries = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGING_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(STAGING_DIR).as_posix()
            info = zipfile.ZipInfo(f"{STAGING_NAME}/{rel}", date_time=time.localtime(path.stat().st_mtime)[:6])
            mode = 0o100755 if path.suffix in EXECUTABLE_SUFFIXES else 0o100644
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
            entries += 1
    return entries


def main() -> None:
    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"[DIST] 缺少模板目录: {TEMPLATE_DIR}")
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    copied = _copy_sources()
    _copy_templates()
    _assert_dist_clean()
    entries = _write_zip()

    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    print(f"[DIST] 复制 {copied} 个源文件到 {STAGING_DIR}")
    print(f"[DIST] zip 包含 {entries} 个文件: {ZIP_PATH} ({size_mb:.1f} MB)")
    print("[DIST] 完成。把 zip 拷到 Mac 上，解压后双击 start.command 即可运行。")


if __name__ == "__main__":
    sys.exit(main())
