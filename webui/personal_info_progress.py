"""Shared in-memory progress operations for personal-information batches."""
from __future__ import annotations

import threading

_MAX_PROGRESS_BATCHES = 24
_PROGRESS_TERMINAL_STATUSES = frozenset({"success", "partial_failure", "failed"})
_PASSWORD_TERMINAL_STATUSES = frozenset({"success", "failed"})
_PASSWORD_APPLY_STATUSES = frozenset({"queued", "running", "success", "failed"})
_progress_lock = threading.RLock()


def _batch_is_terminal(progress: dict[str, dict], batch_id: str, terminal: frozenset[str]) -> bool:
    with _progress_lock:
        current = progress.get(batch_id)
        if not current:
            return False
        rows = current.get("results") or []
        return bool(rows) and all(row.get("status") in terminal for row in rows)


def _force_fail_rows_locked(rows: list, row_indexes: list[int], detail: str) -> None:
    """Mark still-running retry rows as failed after their worker crashed."""
    for row_index in row_indexes:
        if 0 <= row_index < len(rows) and rows[row_index].get("status") in {"queued", "running"}:
            rows[row_index]["status"] = "failed"
            rows[row_index]["detail"] = detail


def _public_results_for_batch(
    progress: dict[str, dict],
    batch_id: str,
    *,
    missing_error: str,
) -> list[dict] | None:
    with _progress_lock:
        current = progress.get(batch_id)
        if not current:
            return None
        rows = current.get("results") or []
        stored = current.get("public_results")
        if not isinstance(stored, list) or len(stored) != len(rows):
            return None
        return [
            dict(value) if isinstance(value, dict) else {
                "email": row.get("email"),
                "change_status": "failed",
                "error": row.get("detail") or missing_error,
            }
            for value, row in zip(stored, rows)
        ]
