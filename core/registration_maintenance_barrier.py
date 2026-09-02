"""In-process maintenance barrier for staging and mock job orchestration."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


OPEN = "open"
DRAINING = "draining"
AWAITING_CONFIRMATION = "awaiting_confirmation"
TIMEOUT = "timeout"


class MaintenanceBarrierError(RuntimeError):
    """Raised when a maintenance transition is invalid."""


class RegistrationMaintenanceBarrier:
    """Block new job starts while active jobs drain for manual maintenance."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._state = OPEN
        self._barrier_id: str | None = None
        self._reason = ""
        self._started_at: str | None = None
        self._deadline: float | None = None
        self._drain_timeout_seconds = 0.0
        self._active_job_ids: set[int] = set()
        self._waiting_job_ids: set[int] = set()
        self._blocked_start_count = 0
        self._last_outcome: str | None = None

    def assert_open_for_submit(self) -> None:
        """Reject creation of new jobs while maintenance blocks starts."""
        with self._condition:
            self._refresh_locked()
            if self._state != OPEN:
                raise MaintenanceBarrierError(
                    f"维护屏障已启用，当前状态: {self._state}"
                )

    def start(self, reason: str, drain_timeout_seconds: float) -> dict:
        """Close the start gate and begin draining active jobs."""
        timeout = float(drain_timeout_seconds)
        if timeout <= 0:
            raise ValueError("drain_timeout_seconds 必须大于 0")
        with self._condition:
            self._refresh_locked()
            if self._state != OPEN:
                raise MaintenanceBarrierError(
                    f"已有维护屏障正在运行: {self._state}"
                )
            self._barrier_id = uuid.uuid4().hex
            self._reason = str(reason or "手动网络维护").strip()[:300]
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._drain_timeout_seconds = timeout
            self._deadline = self._clock() + timeout
            self._last_outcome = None
            self._state = (
                DRAINING if self._active_job_ids else AWAITING_CONFIRMATION
            )
            self._condition.notify_all()
            return self._snapshot_locked()

    def wait_before_start(
        self,
        job_id: int,
        is_cancelled: Callable[[], bool],
    ) -> bool:
        """Wait for an open gate, then atomically claim an active job slot."""
        job_id = int(job_id)
        with self._condition:
            while True:
                self._refresh_locked()
                if is_cancelled():
                    self._waiting_job_ids.discard(job_id)
                    self._condition.notify_all()
                    return False
                if self._state == OPEN:
                    self._waiting_job_ids.discard(job_id)
                    self._active_job_ids.add(job_id)
                    return True
                if job_id not in self._waiting_job_ids:
                    self._waiting_job_ids.add(job_id)
                    self._blocked_start_count += 1
                self._condition.wait(timeout=0.1)

    def notify_job_finished(self, job_id: int) -> None:
        """Release an active claim and advance draining when the last job exits."""
        with self._condition:
            self._active_job_ids.discard(int(job_id))
            self._waiting_job_ids.discard(int(job_id))
            self._refresh_locked()
            if self._state in {DRAINING, TIMEOUT} and not self._active_job_ids:
                self._state = AWAITING_CONFIRMATION
            self._condition.notify_all()

    def auto_maintenance(
        self,
        callback: Callable[[], bool],
        reason: str = "auto",
        drain_timeout_seconds: float = 300.0,
    ) -> bool:
        """Automatically drain active workers, run callback, then resume.
        
        Returns True if maintenance completed successfully (callback returned True),
        False if timeout or callback failed.
        """
        try:
            self.start(reason, drain_timeout_seconds)
        except MaintenanceBarrierError:
            logger.debug("[Barrier] 已有维护屏障正在运行，跳过")
            return False
        
        # Wait for all active workers to finish (DRAINING → AWAITING_CONFIRMATION)
        deadline = time.time() + drain_timeout_seconds
        with self._condition:
            while self._state == DRAINING and time.time() < deadline:
                self._condition.wait(timeout=0.5)
            
            if self._state != AWAITING_CONFIRMATION:
                logger.warning(
                    "[Barrier] drain 超时或状态异常 (%s)，取消维护",
                    self._state,
                )
                if self._barrier_id:
                    try:
                        self.cancel(self._barrier_id)
                    except Exception:  # noqa: BLE001, S110
                        pass
                return False
        
        # Run maintenance callback
        try:
            success = callback()
        except Exception:
            logger.exception("[Barrier] maintenance callback 失败")
            success = False
        
        # Reopen gate
        if self._barrier_id:
            try:
                self.confirm(self._barrier_id)
                logger.info("[Barrier] 维护完成，已恢复")
            except Exception:
                logger.exception("[Barrier] confirm 失败，尝试 cancel")
                try:
                    self.cancel(self._barrier_id)
                except Exception:  # noqa: BLE001, S110
                    pass
        
        return success

    def deferred_rotation(
        self,
        rotation_callback: Callable[[], bool],
        reason: str = "auto",
    ) -> bool:
        """Close the gate, drain active jobs, and rotate before reopening."""
        drain_timeout_seconds = 600.0
        try:
            self.start(reason, drain_timeout_seconds=drain_timeout_seconds)
        except MaintenanceBarrierError:
            logger.debug("[Barrier] 已有屏障运行，跳过 deferred rotation")
            return False

        deadline = time.time() + drain_timeout_seconds
        with self._condition:
            while self._active_job_ids and time.time() < deadline:
                self._condition.wait(timeout=0.5)
            self._refresh_locked()

        if self._active_job_ids:
            logger.warning(
                "[Barrier] deferred rotation 仍有 %d active，gate 保持关闭",
                len(self._active_job_ids),
            )
            return False

        return self._run_rotation_while_closed(rotation_callback)

    def retry_rotation(self, rotation_callback: Callable[[], bool]) -> bool:
        """Retry a failed rotation while the existing start gate stays closed."""
        return self._run_rotation_while_closed(rotation_callback)

    def _run_rotation_while_closed(
        self,
        rotation_callback: Callable[[], bool],
    ) -> bool:
        with self._condition:
            self._refresh_locked()
            if self._state != AWAITING_CONFIRMATION:
                raise MaintenanceBarrierError(
                    f"当前状态不能执行网络轮换: {self._state}"
                )
            if self._active_job_ids:
                raise MaintenanceBarrierError("仍有运行中的任务，不能执行网络轮换")
            barrier_id = self._barrier_id

        try:
            success = bool(rotation_callback())
        except Exception:
            logger.exception("[Barrier] rotation callback 失败")
            success = False

        if not success:
            with self._condition:
                if self._barrier_id == barrier_id and self._state == AWAITING_CONFIRMATION:
                    self._last_outcome = "rotation_failed"
                    self._condition.notify_all()
            logger.warning("[Barrier] rotation 未验证成功，gate 保持关闭")
            return False

        if not barrier_id:
            logger.error("[Barrier] rotation 成功但 barrier_id 缺失，gate 保持关闭")
            return False
        try:
            self.confirm(barrier_id)
        except MaintenanceBarrierError:
            logger.exception("[Barrier] rotation 成功但 confirm 失败，gate 保持关闭")
            return False
        logger.info("[Barrier] rotation 完成，gate 已恢复")
        return True

    def confirm(self, barrier_id: str) -> dict:
        """Acknowledge completed manual maintenance and reopen the start gate."""
        with self._condition:
            self._refresh_locked()
            self._require_current_locked(barrier_id)
            if self._state != AWAITING_CONFIRMATION:
                raise MaintenanceBarrierError(
                    f"当前状态不能确认维护完成: {self._state}"
                )
            if self._active_job_ids:
                raise MaintenanceBarrierError("仍有运行中的任务，不能恢复")
            self._open_locked("confirmed")
            return self._snapshot_locked()

    def cancel(self, barrier_id: str) -> dict:
        """Cancel maintenance and reopen the gate without confirmation."""
        with self._condition:
            self._refresh_locked()
            self._require_current_locked(barrier_id)
            if self._state == OPEN:
                raise MaintenanceBarrierError("维护屏障已经打开")
            self._open_locked("cancelled")
            return self._snapshot_locked()

    def status(self) -> dict:
        """Return a stable snapshot suitable for API responses."""
        with self._condition:
            self._refresh_locked()
            return self._snapshot_locked()

    def reset_for_tests(self) -> None:
        """Reset process-local state; intended for isolated tests only."""
        with self._condition:
            self._state = OPEN
            self._barrier_id = None
            self._reason = ""
            self._started_at = None
            self._deadline = None
            self._drain_timeout_seconds = 0.0
            self._active_job_ids.clear()
            self._waiting_job_ids.clear()
            self._blocked_start_count = 0
            self._last_outcome = None
            self._condition.notify_all()

    def _refresh_locked(self) -> None:
        if (
            self._state == DRAINING
            and self._active_job_ids
            and self._deadline is not None
            and self._clock() >= self._deadline
        ):
            self._state = TIMEOUT
        elif self._state in {DRAINING, TIMEOUT} and not self._active_job_ids:
            self._state = AWAITING_CONFIRMATION

    def _require_current_locked(self, barrier_id: str) -> None:
        if not self._barrier_id or str(barrier_id) != self._barrier_id:
            raise MaintenanceBarrierError("barrier_id 已过期或不匹配")

    def _open_locked(self, outcome: str) -> None:
        self._state = OPEN
        self._last_outcome = outcome
        self._deadline = None
        self._condition.notify_all()

    def _snapshot_locked(self) -> dict:
        remaining = None
        if self._deadline is not None:
            remaining = max(0.0, self._deadline - self._clock())
        return {
            "state": self._state,
            "barrier_id": self._barrier_id,
            "reason": self._reason,
            "started_at": self._started_at,
            "drain_timeout_seconds": self._drain_timeout_seconds,
            "drain_remaining_seconds": remaining,
            "active_job_ids": sorted(self._active_job_ids),
            "waiting_job_ids": sorted(self._waiting_job_ids),
            "active_count": len(self._active_job_ids),
            "waiting_count": len(self._waiting_job_ids),
            "blocked_start_count": self._blocked_start_count,
            "can_confirm": (
                self._state == AWAITING_CONFIRMATION
                and not self._active_job_ids
            ),
            "last_outcome": self._last_outcome,
        }
