"""Coalesced durable persistence for process-worker heartbeats."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call

logger = logging.getLogger(__name__)


def _consume_background_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve a detached shutdown task result to avoid warning leaks."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


_DEFAULT_FLUSH_INTERVAL_SECONDS = 1.0
_DEFAULT_MAX_BATCH_SIZE = 1024
_DEFAULT_MAX_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
_DEFAULT_DISCARD_TIMEOUT_SECONDS = 5.0
_DEFAULT_FLUSH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class _PendingHeartbeat:
    job_id: str
    worker_pid: int
    payload: dict[str, object]
    first_queued_at: float
    publish_count: int = 1


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class WorkerHeartbeatBatcher:
    """Keep only the latest heartbeat per job and commit batches off-loop."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        flush_interval_seconds: float | None = None,
        max_batch_size: int | None = None,
        max_retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
        discard_timeout_seconds: float | None = None,
        flush_timeout_seconds: float | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._flush_interval_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_FLUSH_INTERVAL_SECONDS",
                    _DEFAULT_FLUSH_INTERVAL_SECONDS,
                )
                if flush_interval_seconds is None
                else float(flush_interval_seconds)
            ),
        )
        self._max_batch_size = max(
            1,
            (
                _env_int(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_BATCH_SIZE",
                    _DEFAULT_MAX_BATCH_SIZE,
                )
                if max_batch_size is None
                else int(max_batch_size)
            ),
        )
        self._max_retry_attempts = max(
            1,
            (
                _env_int(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_MAX_RETRY_ATTEMPTS",
                    _DEFAULT_MAX_RETRY_ATTEMPTS,
                )
                if max_retry_attempts is None
                else int(max_retry_attempts)
            ),
        )
        self._retry_backoff_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_RETRY_BACKOFF_SECONDS",
                    _DEFAULT_RETRY_BACKOFF_SECONDS,
                )
                if retry_backoff_seconds is None
                else float(retry_backoff_seconds)
            ),
        )
        self._discard_timeout_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_DISCARD_TIMEOUT_SECONDS",
                    _DEFAULT_DISCARD_TIMEOUT_SECONDS,
                )
                if discard_timeout_seconds is None
                else float(discard_timeout_seconds)
            ),
        )
        self._flush_timeout_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_WORKER_HEARTBEAT_FLUSH_TIMEOUT_SECONDS",
                    _DEFAULT_FLUSH_TIMEOUT_SECONDS,
                )
                if flush_timeout_seconds is None
                else float(flush_timeout_seconds)
            ),
        )
        self._pending: dict[str, _PendingHeartbeat] = {}
        self._in_flight: set[str] = set()
        self._in_flight_done = asyncio.Event()
        self._in_flight_done.set()
        self._flush_wakeup = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    def publish(self, *, job_id: str, worker_pid: int, payload: dict[str, object]) -> None:
        """Queue one latest-value heartbeat without waiting for SQLite."""

        if self._closing or self._closed:
            logger.debug("discarding worker heartbeat after batcher close", extra={"job_id": job_id})
            return
        now = time.monotonic()
        previous = self._pending.get(job_id)
        self._pending[job_id] = _PendingHeartbeat(
            job_id=job_id,
            worker_pid=worker_pid,
            payload=dict(payload),
            first_queued_at=previous.first_queued_at if previous is not None else now,
            publish_count=(previous.publish_count + 1) if previous is not None else 1,
        )
        self._ensure_flush_task()

    async def discard(self, job_id: str) -> None:
        """Drop pending state and wait a bounded time for an in-flight update."""

        self._pending.pop(job_id, None)
        deadline = time.monotonic() + self._discard_timeout_seconds
        while job_id in self._in_flight and self._discard_timeout_seconds > 0:
            completed = self._in_flight_done
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(completed.wait(), timeout=remaining)
            except TimeoutError:
                break
        if job_id in self._in_flight:
            logger.warning(
                "controller worker heartbeat discard deadline elapsed; terminal-state SQL guard remains authoritative",
                extra={"job_id": job_id, "timeout_seconds": self._discard_timeout_seconds},
            )
        self._pending.pop(job_id, None)

    async def flush(self, *, timeout_seconds: float | None = None) -> None:
        """Best-effort persist pending heartbeats within a bounded deadline."""

        if self._closed:
            return
        timeout = self._flush_timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        self._flush_wakeup.set()
        self._ensure_flush_task(immediate=True)
        while True:
            task = self._flush_task
            if task is None:
                return
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining <= 0:
                logger.warning(
                    "controller worker heartbeat flush deadline elapsed",
                    extra={"pending_jobs": len(self._pending), "timeout_seconds": timeout},
                )
                return
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                logger.warning(
                    "controller worker heartbeat flush deadline elapsed",
                    extra={"pending_jobs": len(self._pending), "timeout_seconds": timeout},
                )
                return
            if not self._pending:
                return
            self._ensure_flush_task(immediate=True)

    async def aclose(self) -> None:
        """Bounded best-effort flush followed by cancellation of background work."""

        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_impl(),
                name="northcua-worker-heartbeat-batcher-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        self._closing = True
        await self.flush()
        self._closed = True
        self._pending.clear()
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            done, still_running = await asyncio.wait(
                {task},
                timeout=self._flush_timeout_seconds,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for pending_task in still_running:
                pending_task.add_done_callback(_consume_background_task_result)
            if still_running:
                logger.warning(
                    "controller worker heartbeat close deadline elapsed",
                    extra={"timeout_seconds": self._flush_timeout_seconds},
                )

    def _ensure_flush_task(self, *, immediate: bool = False) -> None:
        if self._closed:
            return
        task = self._flush_task
        if task is not None and not task.done():
            if immediate:
                self._flush_wakeup.set()
            return
        if not self._pending:
            return
        delay = 0.0 if immediate else self._flush_interval_seconds
        self._flush_task = asyncio.create_task(
            self._flush_loop(initial_delay_seconds=delay),
            name="northcua-worker-heartbeat-batcher",
        )

    async def _flush_loop(self, *, initial_delay_seconds: float) -> None:
        delay = initial_delay_seconds
        consecutive_failures = 0
        try:
            while self._pending and not self._closed:
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._flush_wakeup.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    self._flush_wakeup.clear()
                job_ids = list(self._pending)[: self._max_batch_size]
                batch = [self._pending.pop(job_id) for job_id in job_ids]
                self._in_flight = set(job_ids)
                self._in_flight_done = asyncio.Event()
                started_at = time.monotonic()
                try:
                    updated = await controller_store_call(
                        self._db_path,
                        lambda store, batch=batch: store.heartbeat_worker_jobs_batch(
                            [(item.job_id, item.worker_pid, item.payload) for item in batch]
                        ),
                        operation_name="worker_heartbeat_batch",
                        priority=ControllerStorePriority.BACKGROUND,
                    )
                except Exception:
                    consecutive_failures += 1
                    exhausted = consecutive_failures >= self._max_retry_attempts
                    if exhausted:
                        self._pending.clear()
                    else:
                        for item in batch:
                            self._pending.setdefault(item.job_id, item)
                    logger.warning(
                        "controller worker heartbeat batch persistence failed",
                        extra={
                            "batch_size": len(batch),
                            "retry_attempt": consecutive_failures,
                            "retry_exhausted": exhausted,
                        },
                        exc_info=True,
                    )
                    if exhausted:
                        return
                    delay = max(self._retry_backoff_seconds * (2 ** (consecutive_failures - 1)), 0.01)
                else:
                    consecutive_failures = 0
                    queue_seconds = [max(started_at - item.first_queued_at, 0.0) for item in batch]
                    logger.debug(
                        "controller worker heartbeat batch persisted",
                        extra={
                            "batch_size": len(batch),
                            "rows_updated": updated,
                            "heartbeats_coalesced": sum(item.publish_count for item in batch) - len(batch),
                            "queue_seconds_max": round(max(queue_seconds, default=0.0), 6),
                            "db_call_seconds": round(time.monotonic() - started_at, 6),
                        },
                    )
                    delay = 0.0 if len(self._pending) >= self._max_batch_size else self._flush_interval_seconds
                finally:
                    self._in_flight.clear()
                    self._in_flight_done.set()
        finally:
            self._flush_task = None


__all__ = ["WorkerHeartbeatBatcher"]
