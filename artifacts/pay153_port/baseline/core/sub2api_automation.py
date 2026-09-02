"""Callbacks for the sub2api/turb account automation contract."""

from __future__ import annotations

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from config import sub2api as sub2api_config
from core import db

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"success", "failed", "stopped", "cancelled"})


def registration_completion_event(request_id: str, jobs: list[dict]) -> dict | None:
    """Build one deterministic event after every registration job is terminal."""
    request_id = str(request_id or "").strip()
    if not request_id or not jobs:
        return None
    expected_count = len(jobs)
    for job in jobs:
        context = job.get("provider_context") if isinstance(job, dict) else None
        if isinstance(context, dict):
            try:
                expected_count = max(
                    expected_count,
                    int(context.get("sub2api_automation_requested_count") or 0),
                )
            except (TypeError, ValueError):
                pass
    if len(jobs) < expected_count:
        return None
    if any(
        str(job.get("status") or "").strip().lower() not in _TERMINAL_STATUSES
        for job in jobs
    ):
        return None
    succeeded = sum(
        1 for job in jobs if str(job.get("status") or "").strip().lower() == "success"
    )
    return {
        "request_id": request_id,
        "event_id": f"{request_id}:registration:completed",
        "kind": "registration",
        "status": "completed",
        "requested_count": expected_count,
        "succeeded_count": succeeded,
        "failed_count": len(jobs) - succeeded,
        "pending_count": 0,
    }


def reauthorization_completion_event(job: dict) -> dict | None:
    context = job.get("provider_context") if isinstance(job, dict) else None
    if (
        not isinstance(context, dict)
        or context.get("sub2api_automation_kind") != "reauthorization"
    ):
        return None
    status = str(job.get("status") or "").strip().lower()
    if status not in _TERMINAL_STATUSES:
        return None
    request_id = str(context.get("sub2api_automation_request_id") or "").strip()
    account_id = int(context.get("sub2api_account_id") or 0)
    if not request_id or account_id <= 0:
        return None
    event = {
        "request_id": request_id,
        "event_id": f"{request_id}:reauthorization:completed",
        "kind": "reauthorization",
        "status": "succeeded" if status == "success" else "failed",
        "account_id": account_id,
        "email": str(context.get("sub2api_automation_email") or "").strip(),
    }
    if status != "success":
        error = str(
            job.get("error_message")
            or job.get("error")
            or "Codex reauthorization failed"
        ).strip()
        if error:
            event["error"] = error[:500]
    return event


def notify_registration_job(job_id: int) -> bool:
    job = db.get_job(int(job_id)) or {}
    context = job.get("provider_context") if isinstance(job, dict) else None
    if (
        not isinstance(context, dict)
        or context.get("sub2api_automation_kind") != "registration"
    ):
        return False
    request_id = str(context.get("sub2api_automation_request_id") or "").strip()
    event = registration_completion_event(
        request_id, db.list_jobs_for_automation_request(request_id)
    )
    return _send_event(str(context.get("sub2api_callback_url") or ""), event)


def notify_reauthorization_job(job_id: int) -> bool:
    job = db.get_job(int(job_id)) or {}
    context = job.get("provider_context") if isinstance(job, dict) else None
    event = reauthorization_completion_event(job)
    if not isinstance(context, dict):
        return False
    return _send_event(
        str(context.get("sub2api_callback_url") or ""),
        event,
        endpoint="reauthorization/completion",
    )


def notify_job_completion(job_id: int) -> bool:
    """Dispatch the terminal callback for either automation job kind."""
    job = db.get_job(int(job_id)) or {}
    context = job.get("provider_context") if isinstance(job, dict) else None
    if not isinstance(context, dict):
        return False
    kind = str(context.get("sub2api_automation_kind") or "").strip()
    if kind == "registration":
        return notify_registration_job(job_id)
    if kind == "reauthorization":
        return notify_reauthorization_job(job_id)
    return False


def _completion_callback_url(callback_url: str, endpoint: str) -> str:
    parsed = urlparse(str(callback_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    path = path.removesuffix("/callback")
    path = f"{path}/{endpoint.lstrip('/')}"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _send_event(
    callback_url: str, event: dict | None, *, endpoint: str = "callback"
) -> bool:
    if not event:
        return False
    secret = str(
        getattr(sub2api_config, "SUB2API_AUTOMATION_CALLBACK_SECRET", "") or ""
    ).strip()
    if not secret:
        logger.warning(
            "sub2api automation callback skipped: callback secret is not configured"
        )
        return False
    target = _completion_callback_url(callback_url, endpoint)
    if not target:
        logger.error("sub2api automation callback skipped: callback URL is invalid")
        return False
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
    request = Request(
        target,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Sub2API-Automation-Secret": secret,
        },
    )
    timeout = max(
        1, int(getattr(sub2api_config, "SUB2API_AUTOMATION_CALLBACK_TIMEOUT", 20) or 20)
    )
    for attempt in range(1, 4):
        try:
            with urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True
                logger.warning(
                    "sub2api automation callback returned HTTP %s", response.status
                )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "sub2api automation callback failed attempt=%s: %s",
                attempt,
                type(exc).__name__,
            )
        if attempt < 3:
            time.sleep(0.5 * attempt)
    return False
