"""Durable, local-only recovery for an uncertain password change."""
from __future__ import annotations

import argparse
import json
from contextlib import closing

from core import db

_PREFIX = "named:password_change_candidate:"


def _key(account_id: int) -> str:
    return f"{_PREFIX}{int(account_id)}"


def stage(account_id: int, password: str) -> None:
    """Reserve one candidate before the remote operation; never overwrite one."""
    if not str(password or "").strip():
        raise ValueError("password candidate is required")
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO app_documents(document_key, payload_json) VALUES (?, ?)",
            (_key(account_id), json.dumps({"password": password})),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("Pending password recovery requires local reconciliation")


def discard(account_id: int) -> None:
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        conn.execute("DELETE FROM app_documents WHERE document_key = ?", (_key(account_id),))


def resolve(account_id: int, *, applied: bool) -> bool:
    """Commit the candidate only after an operator verifies the remote state."""
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        stored = conn.execute(
            "SELECT payload_json FROM app_documents WHERE document_key = ?", (_key(account_id),)
        ).fetchone()
    if stored is None:
        return False
    candidate = json.loads(stored["payload_json"])
    password = str(candidate.get("password") or "")
    if not password:
        raise ValueError("Invalid password recovery record")
    if applied and not db.update_account_password(
        account_id, password=password, extra_updates={"registration_password": password}
    ):
        raise RuntimeError("Password recovery could not update the account")
    discard(account_id)
    return True


def pending_account_ids() -> list[int]:
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        rows = conn.execute(
            "SELECT document_key FROM app_documents WHERE document_key LIKE ? ORDER BY document_key",
            (_PREFIX + "%",),
        ).fetchall()
    return [int(str(row["document_key"])[len(_PREFIX):]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile uncertain password changes locally")
    parser.add_argument("action", choices=("list", "applied", "not-applied"))
    parser.add_argument("--account-id", type=int)
    args = parser.parse_args()
    if args.action == "list":
        print("Pending account IDs:", ", ".join(map(str, pending_account_ids())) or "none")
        return
    if args.account_id is None:
        parser.error("--account-id is required")
    if not resolve(args.account_id, applied=args.action == "applied"):
        parser.error("no pending recovery for that account")
    print("Recovery record reconciled")


if __name__ == "__main__":
    main()
