"""Gmail API URL batch coordination and alias allocation.

This module owns canonical batch creation, lazy source provisioning, queue
reconciliation, and assignment lifecycle. HTTP/OTP behavior lives in
the Gmail API URL client module.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_api_url_batch_store import (
    GmailApiUrlBatchConflict,
    GmailApiUrlBatchError,
    GmailApiUrlBatchStore,
)
from core.time_utils import local_now

if TYPE_CHECKING:
    from core.gmail_api_url_client import GmailApiUrlAccount

logger = logging.getLogger(__name__)


def _account(email: str, code_url: str) -> GmailApiUrlAccount:
    """Build the HTTP client's account value without importing it eagerly."""
    from core.gmail_api_url_client import GmailApiUrlAccount

    return GmailApiUrlAccount(email=email, code_url=code_url)

_BATCH_STORE_PATH = APP_STATE_DB_PATH
_batch_store_instance: GmailApiUrlBatchStore | None = None
_BATCH_BUILD_LOCK = threading.Lock()


def _batch_store() -> GmailApiUrlBatchStore:
    """Return the canonical Gmail API URL batch store singleton."""
    global _batch_store_instance
    if _batch_store_instance is None:
        _batch_store_instance = GmailApiUrlBatchStore(_BATCH_STORE_PATH)
    return _batch_store_instance


# Account lifecycle

def release_account(
    email: str,
    status: str = "available",
    note: str = "",
    *,
    job_id: int | str | None = None,
) -> bool:
    """Release a raw Gmail account or finalize its canonical batch alias."""
    from . import db

    store = _batch_store()
    if job_id is None:
        batch_context = get_batch_account_context(email)
        active = store.find_active_assignment_for_alias(email) if batch_context else None
    else:
        active = store.find_active_assignment_for_job(str(job_id))
        if active is not None:
            active_alias = str(getattr(active, "inventory_id", "") or "").split(
                "----", 1
            )[0]
            if active_alias.casefold() != str(email or "").strip().casefold():
                active = None
        batch_context = get_batch_account_context(email, job_id=job_id) if active else None
    if active:
        if status in {"used", "consumed"}:
            changed = store.complete(active.assignment_id)
            logger.info(
                "Batch assignment %s completed for alias %s",
                active.assignment_id[:8], email,
            )
        elif status in {"released", "cancelled"}:
            changed = store.release(active.assignment_id, reason=note[:300])
            logger.info(
                "Batch assignment %s released for alias %s",
                active.assignment_id[:8], email,
            )
        else:
            changed = store.discard(active.assignment_id, reason=note[:300])
            logger.warning(
                "Batch assignment %s discarded alias %s sau lỗi: %s",
                active.assignment_id[:8], email, note[:100],
            )
        return bool(changed)

    scope_kwargs = {}
    if store.path != getattr(db, "_DEFAULT_SQLITE_PATH", store.path):
        scope_kwargs["sqlite_path"] = store.path
    db.release_gmail_api_url_email(email, status, note, **scope_kwargs)
    return db.get_gmail_api_url_email_by_email(email, **scope_kwargs) is not None


# ============================================================================
# Multi-alias Batch Registration
# ============================================================================
#
# Mô hình: 1 email record (email----code_url) → sinh tối đa 12 alias
# (6 gmail.com + 6 googlemail.com). Mọi alias forward về cùng hộp thư nên
# TẤT CẢ dùng chung code_url của email gốc để lấy OTP.
# Học từ Gmail CDK, nhưng nguồn là kho email----url, KHÔNG dùng CDK.

def create_registration_batch(
    count: int,
    aliases_per_email: int | None = None,
    *,
    allow_partial: bool = False,
) -> str:
    """Serialize alias inventory selection and create one registration batch.

    ``allow_partial`` is retained for callers that intentionally build a
    partially materialized canonical batch.  New registration jobs use the
    empty-batch lazy path below so source purchase and alias allocation share
    one ledger.
    """
    with _BATCH_BUILD_LOCK:
        return _create_registration_batch(
            count,
            aliases_per_email=aliases_per_email,
            allow_partial=allow_partial,
        )


def _reconcile_source_alias_ownership(
    store: GmailApiUrlBatchStore,
    code_url: str,
) -> None:
    """Release aliases held only by terminal jobs before allocating a new batch."""
    for batch_id in store.list_batch_ids_for_code_urls({str(code_url or "").strip()}):
        _reconcile_batch_queue(store, batch_id)


def _alias_owned_by_other_code_url(
    store: GmailApiUrlBatchStore,
    alias: str,
    code_url: str,
) -> bool:
    """Check exact/root ownership while keeping lightweight test doubles usable."""
    checker = getattr(store, "has_alias_for_other_code_url", None)
    if checker is None:
        return False
    # The concrete store returns ``bool``.  Identity checking deliberately
    # treats an unconfigured MagicMock as false, while still allowing tests and
    # alternate stores to return an explicit True collision result.
    return checker(alias, code_url) is True


def _create_registration_batch(
    count: int,
    aliases_per_email: int | None = None,
    *,
    allow_partial: bool = False,
) -> str:
    """Claim đủ email gốc từ pool để tạo `count` alias, mỗi email sinh tối đa
    `aliases_per_email` alias (share code_url của email đó).

    Mô hình:
        - Mỗi email gốc sinh tối đa 12 alias (6 gmail.com + 6 googlemail.com).
        - Alias đã cấp trước đó của cùng code_url sẽ bị bỏ qua.
        - Nếu một record còn ít alias mới, allocator lấy thêm email gốc khác.
        - Alias cùng một email gốc share code_url của email đó.

    Args:
        count: Tổng số alias (job) cần tạo.
        aliases_per_email: Số alias mỗi email gốc (1..12). None → dùng max 12.

    Returns:
        batch_id

    Raises:
        GmailApiUrlBatchError: pool không đủ email, hoặc tham số sai.
    """
    from core.gmail_aliases import (
        MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
        GmailAliasError,
        canonical_gmail,
        generate_gmail_dual_domain_aliases,
    )

    from . import db

    if count < 1:
        raise GmailApiUrlBatchError("Batch cần ít nhất 1 alias")

    per_email = aliases_per_email or MAX_GMAIL_DUAL_DOMAIN_VARIANTS
    per_email = max(1, min(MAX_GMAIL_DUAL_DOMAIN_VARIANTS, int(per_email)))

    groups: list[dict] = []
    claimed_sources: list[tuple[str, bool]] = []
    excluded_sources: set[str] = set()
    selected_aliases: set[str] = set()
    selected_roots: dict[str, str] = {}
    remaining = count
    store = _batch_store()
    try:
        while remaining > 0:
            record = db.claim_next_gmail_api_url_email(
                include_used=True,
                exclude_emails=excluded_sources,
            )
            if not record:
                if allow_partial:
                    logger.info(
                        "Gmail API URL pool exhausted while building a partial batch "
                        "(created=%d, remaining=%d)",
                        count - remaining,
                        remaining,
                    )
                    break
                raise GmailApiUrlBatchError(
                    f"Gmail API URL pool không đủ alias mới cho {count} tài khoản, "
                    f"đã claim {len(claimed_sources)} email gốc, còn thiếu {remaining} alias"
                )
            source_email = record["email"]
            source_key = str(source_email or "").strip().casefold()
            code_url = record["code_url"]
            claimed_from_available = bool(record.get("_claimed_from_available", True))
            claimed_sources.append((source_email, claimed_from_available))
            excluded_sources.add(source_key)

            want = min(per_email, remaining)
            try:
                candidates = generate_gmail_dual_domain_aliases(
                    source_email, limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS
                )
            except GmailAliasError as exc:
                raise GmailApiUrlBatchError(
                    f"Email gốc {source_email} không hợp lệ: {exc}"
                ) from exc
            _reconcile_source_alias_ownership(store, code_url)
            used_aliases = store.list_allocated_aliases_for_code_url(code_url)
            globally_unavailable = store.list_globally_unavailable_aliases()
            aliases: list[str] = []
            for alias in candidates:
                normalized_alias = alias.strip().casefold()
                if (
                    normalized_alias in used_aliases
                    or normalized_alias in globally_unavailable
                    or normalized_alias in selected_aliases
                    or _alias_owned_by_other_code_url(store, alias, code_url)
                ):
                    continue
                try:
                    root = canonical_gmail(alias)
                except GmailAliasError:
                    root = ""
                selected_url = selected_roots.get(root) if root else None
                if selected_url is not None and selected_url != code_url:
                    continue
                aliases.append(alias)
                if len(aliases) >= want:
                    break
            if not aliases:
                if store.has_pending_alias_for_code_url(code_url) is True:
                    # The source still backs aliases queued or temporarily
                    # reserved by another worker/batch.  Keep the raw row
                    # usable and let the existing canonical queue drain.
                    claimed_sources.remove((source_email, claimed_from_available))
                    continue
                db.release_gmail_api_url_email(
                    source_email,
                    "exhausted",
                    "Record đã dùng hết alias Gmail khả dụng",
                )
                claimed_sources.remove((source_email, claimed_from_available))
                continue
            groups.append({
                "source_email": source_email,
                "code_url": code_url,
                "aliases": aliases,
            })
            selected_aliases.update(alias.strip().casefold() for alias in aliases)
            for alias in aliases:
                try:
                    selected_roots[canonical_gmail(alias)] = code_url
                except GmailAliasError:
                    continue
            remaining -= len(aliases)
            if remaining <= 0:
                break
    except Exception:
        # Rollback: trả tất cả email gốc đã claim về pool
        for source_email, claimed_from_available in claimed_sources:
            if not claimed_from_available:
                continue
            try:
                db.release_gmail_api_url_email(
                    source_email, "available", "Tạo batch thất bại"
                )
            except Exception as exc:  # noqa: BLE001 - rollback must not hide the original failure.
                logger.warning("Không thể release email %s về pool: %s", source_email, exc)
        raise

    try:
        batch_id = (
            store.create_batch_multi(groups)
            if groups
            else store.create_empty_batch()
        )
    except Exception:
        for source_email, claimed_from_available in claimed_sources:
            if not claimed_from_available:
                continue
            try:
                db.release_gmail_api_url_email(
                    source_email, "available", "Tạo batch thất bại"
                )
            except Exception as exc:  # noqa: BLE001 - rollback must not hide the original failure.
                logger.warning("Không thể release email %s về pool: %s", source_email, exc)
        raise

    total_aliases = sum(len(g["aliases"]) for g in groups)
    logger.info(
        "Đã tạo Gmail API URL batch %s từ %d email gốc với %d alias (mỗi email ≤%d)",
        batch_id, len(groups), total_aliases, per_email,
    )
    return batch_id


def materialize_next_available_source(
    batch_id: str,
    *,
    aliases_per_source: int = 12,
    exclude_code_urls: set[str] | None = None,
    store: GmailApiUrlBatchStore | None = None,
) -> bool:
    """Move one raw Gmail API source into the canonical batch ledger.

    Imported Gmail API records predate the batch tables and can still have
    usable aliases even when no canonical batch item exists.  The lazy
    coordinator calls this bridge before purchasing: one source is claimed
    from the raw pool,
    all currently unallocated aliases for that source are appended to the
    requested canonical batch, and workers then claim them through the normal
    Gmail assignment table.

    Returns ``True`` when a source was materialized, or when an already
    canonicalized source was observed and should be retried after a concurrent
    transaction.  ``False`` means the raw pool has no source left to bridge.
    """
    normalized_batch = str(batch_id or "").strip()
    if not normalized_batch:
        raise GmailApiUrlBatchError("Gmail API URL batch ID is required")
    limit = max(1, min(12, int(aliases_per_source or 12)))
    excluded_urls = {
        str(url or "").strip()
        for url in (exclude_code_urls or set())
        if str(url or "").strip()
    }
    target_store = store or _batch_store()

    from core.gmail_aliases import (
        MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
        GmailAliasError,
        generate_gmail_dual_domain_aliases,
    )

    from . import db

    with _BATCH_BUILD_LOCK:
        attempted: set[str] = set()
        while True:
            record = db.claim_next_gmail_api_url_email(
                include_used=True,
                exclude_emails=attempted,
                sqlite_path=target_store.path,
            )
            if not record:
                return False

            source_email = str(record.get("email") or "").strip()
            code_url = str(record.get("code_url") or "").strip()
            source_key = source_email.casefold()
            if source_key:
                attempted.add(source_key)
            if not source_email or not code_url:
                continue
            if code_url in excluded_urls:
                if bool(record.get("_claimed_from_available")):
                    db.release_gmail_api_url_email(
                        source_email,
                        "available",
                        "Gmail API source is already owned by an active Gmail batch",
                        sqlite_path=target_store.path,
                    )
                continue
            owned_batches = {
                str(owner or "").strip()
                for owner in (target_store.list_batch_ids_for_code_urls({code_url}) or [])
                if str(owner or "").strip()
            }
            if owned_batches:
                if bool(record.get("_claimed_from_available")):
                    db.release_gmail_api_url_email(
                        source_email,
                        "exhausted",
                        "Gmail API source đã thuộc canonical batch khác",
                        sqlite_path=target_store.path,
                    )
                continue
            if db.is_gmail_api_url_code_url_failed(
                code_url,
                sqlite_path=target_store.path,
            ):
                # claim_next_gmail_api_url_email() marks a newly selected
                # available row as used.  A sibling row for an already
                # quarantined URL must not be left in that misleading state.
                db.fail_gmail_api_url_sources_for_code_url(
                    code_url,
                    note="Gmail API URL đã bị quarantine sau lỗi code=602",
                    sqlite_path=target_store.path,
                )
                continue

            try:
                candidates = generate_gmail_dual_domain_aliases(
                    source_email,
                    limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
                )
            except GmailAliasError:
                if bool(record.get("_claimed_from_available")):
                    db.release_gmail_api_url_email(
                        source_email,
                        "failed",
                        "Gmail source email is not a valid alias root",
                        sqlite_path=target_store.path,
                    )
                continue

            _reconcile_source_alias_ownership(target_store, code_url)
            usage = target_store.alias_usage_for_code_urls({code_url}).get(
                code_url,
                {"allocated": set()},
            )
            allocated = {
                str(alias or "").strip().casefold()
                for alias in usage.get("allocated", set())
            }
            consumed = {
                str(alias or "").strip().casefold()
                for alias in usage.get("consumed", set())
            }
            failed = {
                str(alias or "").strip().casefold()
                for alias in usage.get("failed", set())
            }
            reusable = allocated - consumed - failed
            unavailable = target_store.list_globally_unavailable_aliases()
            aliases = [
                alias
                for alias in candidates
                if alias.strip().casefold() not in allocated
                and alias.strip().casefold() not in unavailable
                and not target_store.has_alias_for_other_code_url(
                    alias, code_url
                )
            ][:limit]
            if not aliases:
                # Existing canonical rows may be temporarily locked or may
                # have been appended by another worker.  Leave the root row
                # untouched and let the caller re-check the shared ledger.
                if reusable:
                    return True
                db.release_gmail_api_url_email(
                    source_email,
                    "exhausted",
                    "Record đã dùng hết alias Gmail khả dụng",
                    sqlite_path=target_store.path,
                )
                continue

            try:
                target_store.append_source_group(
                    normalized_batch,
                    source_email,
                    code_url,
                    aliases,
                    exclusive_code_url=True,
                )
            except GmailApiUrlBatchConflict:
                if bool(record.get("_claimed_from_available")):
                    db.release_gmail_api_url_email(
                        source_email,
                        "exhausted",
                        "Gmail API source was claimed by another canonical batch",
                        sqlite_path=target_store.path,
                    )
                continue
            return True


def provision_next_gmail_api_url_source(
    batch_id: str,
    *,
    aliases_per_source: int = 12,
    store: GmailApiUrlBatchStore | None = None,
    stop_check=None,
) -> bool:
    """Materialize one raw source, then purchase one source if needed."""
    target_store = store or _batch_store()
    plan = target_store.batch_provision_plan(batch_id)
    desired_sources = plan.get("desired_sources")
    if (
        isinstance(desired_sources, (int, str))
        and not isinstance(desired_sources, bool)
        and target_store.count_source_groups(batch_id) >= int(desired_sources)
    ):
        logger.info(
            "Gmail API URL batch %s reached its source budget (%s); skip purchase",
            batch_id,
            desired_sources,
        )
        return False
    if materialize_next_available_source(
        batch_id,
        aliases_per_source=aliases_per_source,
        store=target_store,
    ):
        return True

    if stop_check is None:
        try:
            from core.registration_service import check_stop_requested

            stop_check = check_stop_requested
        except (ImportError, AttributeError):
            stop_check = None

    from core.qan8_gmail_api_purchaser import Qan8GmailApiPurchaser

    return bool(
        Qan8GmailApiPurchaser().purchase_source(
            batch_id,
            aliases_per_source=aliases_per_source,
            store=target_store,
            stop_check=stop_check,
        )
    )


def _reconcile_batch_queue(store: GmailApiUrlBatchStore, batch_id: str) -> None:
    """Resolve locks and waiters left by terminal registration jobs."""
    from . import db

    terminal_states = {"success", "failed", "stopped", "cancelled"}
    for assignment in store.list_active_assignments(batch_id):
        try:
            job = db.get_job(int(assignment.job_id))
        except (TypeError, ValueError):
            continue
        job_status = str((job or {}).get("status") or "")
        if job_status in {"running", "stopping"}:
            from .registration_service import is_job_active

            if is_job_active(int(assignment.job_id)):
                continue
            db.update_job(
                int(assignment.job_id),
                status="failed",
                error="Worker registration không còn tồn tại sau khi tiến trình dừng",
                completed_at=local_now().astimezone().isoformat(timespec="seconds"),
            )
            job_status = "failed"
        if job is not None and job_status not in terminal_states:
            continue
        alias = assignment.inventory_id.split("----", 1)[0]
        account_exists = bool((job or {}).get("account_id") or db.get_account_by_email(alias))
        if account_exists:
            store.complete(assignment.assignment_id)
            logger.info(
                "Đã hoàn tất assignment mồ côi %s vì job %s đã lưu account",
                assignment.assignment_id[:8],
                assignment.job_id,
            )
        elif job is None:
            store.discard(
                assignment.assignment_id,
                reason="missing job reconciliation",
            )
            logger.warning(
                "Đã loại bỏ alias mồ côi %s vì job %s không còn tồn tại",
                alias,
                assignment.job_id,
            )
        elif job_status == "failed":
            store.discard(
                assignment.assignment_id,
                reason=(
                    "orphaned running job reconciliation"
                    if str(job.get("status") or "") in {"running", "stopping"}
                    else "failed job reconciliation"
                ),
            )
            logger.warning(
                "Đã loại bỏ alias của job thất bại %s để không cấp lại",
                assignment.job_id,
            )
        else:
            store.release(assignment.assignment_id, reason="terminal job reconciliation")
            logger.info(
                "Đã giải phóng assignment mồ côi %s của job %s",
                assignment.assignment_id[:8],
                assignment.job_id,
            )

    # Older workers released failed assignments before the discard rule was
    # introduced. Retire those aliases once they are observed during queue
    # reconciliation so historical failures cannot poison later jobs.
    for assignment in store.list_reusable_assignments(batch_id):
        try:
            job = db.get_job(int(assignment.job_id))
        except (TypeError, ValueError):
            continue
        if job and str(job.get("status") or "") == "failed":
            store.discard(
                assignment.assignment_id,
                reason="failed job reconciliation",
            )
            logger.warning(
                "Đã loại bỏ alias cũ của job thất bại %s để không cấp lại",
                assignment.job_id,
            )

    for waiting_job_id in store.list_waiting_jobs(batch_id):
        try:
            job = db.get_job(int(waiting_job_id))
        except (TypeError, ValueError):
            continue
        if job and str(job.get("status") or "") in terminal_states:
            store.cancel_waiter(batch_id, waiting_job_id, "terminal job reconciliation")


def get_email_from_batch(
    batch_id: str,
    job_id: str,
    *,
    wait_timeout: float | None = None,
    poll_interval: float = 1.0,
    aliases_per_source: int = 12,
) -> GmailApiUrlAccount:
    """Claim alias tiếp theo trong batch cho job_id, có hàng đợi bền.

    Args:
        batch_id: Batch ID
        job_id: Job ID duy nhất cho registration job
        wait_timeout: Giới hạn tùy chọn cho caller đặc biệt; None nghĩa là chờ đến khi
            được cấp hoặc batch thực sự hết slot.
        aliases_per_source: Số alias tối đa cần materialize cho một source (1..12).

    Returns:
        GmailApiUrlAccount(email=alias, code_url=code_url gốc)

    Raises:
        GmailApiUrlBatchConflict: batch hết alias available.
        GmailApiUrlBatchError: inventory_id không đúng định dạng.
    """
    deadline = None if wait_timeout is None else time.monotonic() + max(0.0, float(wait_timeout))
    retry_delay = max(0.0, float(poll_interval))
    source_capacity = max(1, min(12, int(aliases_per_source or 12)))
    store = _batch_store()
    last_status = None
    last_log_at = 0.0
    provision_owner = f"gmail-api:{batch_id}:{job_id}"
    while True:
        try:
            numeric_job_id = int(job_id)
        except (TypeError, ValueError):
            numeric_job_id = None
        if numeric_job_id is not None:
            from core import registration_service

            if registration_service.is_stop_requested(numeric_job_id):
                store.cancel_waiter(batch_id, job_id, "job stopped by email lane quarantine")
                raise registration_service.StopRequested(
                    f"任务 #{job_id} 已因邮箱 lane 被禁用而停止"
                )
        _reconcile_batch_queue(store, batch_id)
        try:
            assignment = store.claim_waiting(batch_id, job_id)
        except GmailApiUrlBatchConflict:
            assignment = None
        if assignment is not None:
            break

        status = store.batch_status(batch_id)
        if status["exhausted_batch"]:
            if numeric_job_id is not None:
                from core import registration_service

                stopped = registration_service.is_stop_requested(numeric_job_id)
            else:
                stopped = False
            if stopped:
                store.cancel_waiter(batch_id, job_id, "job stopped before provisioning")
                raise registration_service.StopRequested(
                    f"Job {job_id} stopped before Gmail source provisioning"
                )
            if store.acquire_provision_lease(
                provision_owner,
                batch_id=batch_id,
            ):
                try:
                    provisioned = provision_next_gmail_api_url_source(
                        batch_id,
                        aliases_per_source=source_capacity,
                        store=store,
                    )
                finally:
                    store.release_provision_lease(
                        provision_owner,
                        batch_id=batch_id,
                    )
                if provisioned:
                    continue
                store.cancel_waiter(batch_id, job_id, "Gmail API source pool exhausted")
                raise GmailApiUrlBatchConflict("No Gmail API URL source available")
            # Another worker is materializing/purchasing the next source. Keep
            # this waiter alive and retry the canonical queue after it commits.
            if retry_delay:
                time.sleep(retry_delay)
            continue
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Job %s vẫn đang chờ batch %s sau %.1fs; giữ waiter trong DB để retry tiếp",
                job_id,
                batch_id,
                max(0.0, float(wait_timeout or 0.0)),
            )
            raise GmailApiUrlBatchConflict(
                "Gmail API URL batch đang bận; job đã được lưu vào hàng đợi"
            )
        status_snapshot = (
            status["pending"],
            status["active_assignments"],
            status["waiting_jobs"],
            status["available_code_urls"],
        )
        now = time.monotonic()
        if status_snapshot != last_status or now - last_log_at >= 30:
            logger.info(
                "Job %s chờ code_url rảnh trong Gmail API URL batch %s "
                "(pending=%s active=%s waiting=%s available_code_urls=%s)",
                job_id,
                batch_id,
                status["pending"],
                status["active_assignments"],
                status["waiting_jobs"],
                status["available_code_urls"],
            )
            last_status = status_snapshot
            last_log_at = now
        time.sleep(retry_delay)
    # inventory_id format từ create_batch_multi: "{alias}----{code_url}"
    try:
        alias, code_url = assignment.inventory_id.split("----", 1)
    except ValueError as exc:
        raise GmailApiUrlBatchError(
            f"inventory_id không đúng định dạng alias----code_url: {assignment.inventory_id}"
        ) from exc
    logger.info(
        "Job %s claim alias %s từ batch %s (code_url dùng chung)",
        job_id, alias, batch_id,
    )
    return _account(alias, code_url)


def get_batch_account_context(
    alias: str,
    *,
    job_id: int | str | None = None,
    batch_id: str | None = None,
) -> GmailApiUrlAccount | None:
    """Tra code_url cho một alias thuộc batch (dùng khi wait_for_otp)."""
    store = _batch_store()
    result = None
    if job_id is not None:
        result = store.find_item_by_alias_for_job(alias, str(job_id))
    if result is None and batch_id:
        result = store.find_item_by_alias_for_batch(alias, str(batch_id))
    if result is None and job_id is None and not batch_id:
        result = store.find_item_by_alias(alias)
    if not result:
        return None
    found_alias, code_url = result
    return _account(found_alias, code_url)


def has_active_batch_assignment(job_id: int | str) -> bool:
    """Return whether a registration job still owns a Gmail API URL alias."""
    return _batch_store().find_active_assignment_for_job(str(job_id)) is not None


def complete_batch_assignment(batch_id: str, job_id: str) -> bool:
    """Đánh dấu alias assignment hoàn thành. Email gốc trong pool để nguyên."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().complete(assignment.assignment_id)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Completed alias %s (assignment %s) cho job %s",
            alias, assignment.assignment_id, job_id,
        )
    return success


def fail_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    """Đánh dấu alias assignment thất bại."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().fail(assignment.assignment_id, reason)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Failed alias %s (assignment %s) cho job %s: %s",
            alias, assignment.assignment_id, job_id, reason,
        )
    return success


def release_batch_assignment(batch_id: str, job_id: str, reason: str = "") -> bool:
    """Release alias assignment về available (chưa dùng)."""
    assignment = _batch_store().find_active_assignment("", job_id)
    if not assignment:
        logger.warning("Không tìm thấy active assignment cho job %s", job_id)
        return False
    success = _batch_store().release(assignment.assignment_id, reason)
    if success:
        try:
            alias, _ = assignment.inventory_id.split("----", 1)
        except ValueError:
            alias = assignment.inventory_id
        logger.info(
            "Released alias %s (assignment %s) cho job %s: %s",
            alias, assignment.assignment_id, job_id, reason,
        )
    return success


def quarantine_code_url(code_url: str, *, reason: str = "") -> int:
    """Retire every batch alias sharing a broken provider URL."""
    return _batch_store().quarantine_code_url(code_url, reason=reason)
