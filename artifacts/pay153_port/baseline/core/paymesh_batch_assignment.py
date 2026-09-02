# -*- coding: utf-8 -*-
from __future__ import annotations

from core.app_state_db import APP_STATE_DB_PATH
from core.cdk_inventory_store import CdkInventoryStore


def assign_paymesh_jobs(inventory_ids: list[str], count: int) -> list[str]:
    """Assign jobs to Paymesh inventory in stable round-robin order."""
    ids = list(dict.fromkeys(
        str(inventory_id or "").strip()
        for inventory_id in inventory_ids
        if str(inventory_id or "").strip()
    ))
    total = int(count)
    if not ids:
        raise ValueError("Batch Paymesh cần ít nhất một inventory ID")
    if total < 1:
        raise ValueError("Số job Paymesh phải lớn hơn 0")
    return [ids[index % len(ids)] for index in range(total)]


def create_paymesh_job_assignments(
    cdks: list[str],
    *,
    count: int,
    configured_limit: int,
    store: CdkInventoryStore | None = None,
) -> list[str]:
    """Import raw cards once, then return one durable inventory ID per job."""
    raw_cdks = list(dict.fromkeys(
        str(cdk or "").strip() for cdk in cdks if str(cdk or "").strip()
    ))
    if not raw_cdks:
        raise ValueError("Chưa nhập Paymesh MAIL card cho batch đăng ký")
    inventory = store or CdkInventoryStore(
        APP_STATE_DB_PATH
    )
    inventory_ids = [
        inventory.import_cdk(
            "paymesh",
            cdk,
            configured_limit=max(1, min(6, int(configured_limit))),
        )[0].inventory_id
        for cdk in raw_cdks
    ]
    return assign_paymesh_jobs(inventory_ids, count)
