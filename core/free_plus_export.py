# -*- coding: utf-8 -*-
"""选择并生成 Free Plus 账号 TXT 导出。"""
from __future__ import annotations

from datetime import datetime

from core import db

_MAX_EXPORT_ACCOUNTS = 5000


def is_free_plus_account(row: dict) -> bool:
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    return plan == "free" and bool(row.get("plus_trial_eligible"))


def prepare_export(
    *,
    scope: str,
    account_ids: list | None = None,
    format_name: str = "modern",
    allow_reexport: bool = False,
    archived: str | bool | None = "all",
    q: str | None = None,
) -> dict:
    normalized_format = db._normalize_account_line_format(format_name)
    scope = str(scope or "selected").strip().lower()
    if scope not in {"selected", "all_filtered"}:
        raise ValueError("scope 仅支持 selected 或 all_filtered")

    skipped: list[dict] = []
    if scope == "selected":
        if not isinstance(account_ids, list) or not account_ids:
            raise ValueError("selected 模式需要非空 account_ids")
        if len(account_ids) > _MAX_EXPORT_ACCOUNTS:
            raise ValueError(f"单次最多导出 {_MAX_EXPORT_ACCOUNTS} 个账号")
        rows = []
        seen: set[int] = set()
        for raw_id in account_ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            row = db.get_account(account_id)
            if row is None:
                skipped.append({"id": account_id, "reason": "账号不存在"})
                continue
            rows.append(row)
    else:
        rows = db.list_accounts(
            limit=_MAX_EXPORT_ACCOUNTS + 1,
            archived=archived,
            plan_filter="free_plus",
            q=q,
            free_plus_export_filter="all" if allow_reexport else "unexported",
        )
        if len(rows) > _MAX_EXPORT_ACCOUNTS:
            raise ValueError(f"筛选结果超过 {_MAX_EXPORT_ACCOUNTS} 个，请缩小范围后再导出")

    included: list[dict] = []
    lines: list[str] = []
    for row in rows:
        account_id = int(row.get("id") or 0)
        email = row.get("email")
        if not is_free_plus_account(row):
            skipped.append({"id": account_id, "email": email, "reason": "不是可用 Plus 试用账号"})
            continue
        if row.get("free_plus_exported_at") and not allow_reexport:
            skipped.append({"id": account_id, "email": email, "reason": "已导出"})
            continue
        line = db.account_line(row, normalized_format)
        if not line.strip():
            skipped.append({"id": account_id, "email": email, "reason": "导出内容为空"})
            continue
        lines.append(line)
        included.append({"id": account_id, "email": email})

    if not included:
        raise ValueError("没有符合条件且尚未导出的 Free Plus 账号")

    now = datetime.now()
    filename = f"free-plus-{normalized_format}-{now.strftime('%Y%m%d-%H%M%S')}.txt"
    content = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
    return {
        "content": content,
        "filename": filename,
        "format": normalized_format,
        "accounts": included,
        "account_ids": [item["id"] for item in included],
        "count": len(included),
        "skipped": skipped,
    }
