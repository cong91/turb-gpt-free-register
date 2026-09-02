from __future__ import annotations

import argparse
import gc
import importlib.util
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODIFIED = ROOT / "diagnostics_sqlite_lock" / "MODIFIED_FILE.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    spec.loader.exec_module(module)
    return module


def _baseline_module():
    source = subprocess.check_output(
        ["git", "show", "HEAD:core/app_state_db.py"],
        cwd=ROOT,
        text=True,
    )
    spec = importlib.util.spec_from_loader("baseline_app_state_db", loader=None)
    if spec is None:
        raise RuntimeError("cannot create baseline module")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(ROOT / "core" / "app_state_db.py")
    exec(compile(source, "HEAD:core/app_state_db.py", "exec"), module.__dict__)
    return module


def run(module, *, hold_seconds: float, timeout_ms: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "lock.sqlite3"
        setup = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        setup.execute("CREATE TABLE probe (value INTEGER)")
        setup.execute("BEGIN EXCLUSIVE")
        released = threading.Event()

        def release_lock() -> None:
            time.sleep(hold_seconds)
            setup.rollback()
            setup.close()
            released.set()

        threading.Thread(target=release_lock, daemon=True).start()
        started = time.monotonic()
        try:
            connection = module.connect(path, busy_timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - probe records the observed error.
            result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            result = {
                "status": "connected",
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
            }
            connection.close()
        released.wait(timeout=5)
        gc.collect()
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("baseline", "modified"))
    args = parser.parse_args()
    module = _baseline_module() if args.mode == "baseline" else _load(MODIFIED, "modified_app_state_db")
    timeout_ms = 100 if args.mode == "baseline" else 30000
    print(run(module, hold_seconds=1.5, timeout_ms=timeout_ms))


if __name__ == "__main__":
    main()
