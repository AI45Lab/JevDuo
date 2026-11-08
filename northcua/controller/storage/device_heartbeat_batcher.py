"""Coalesced durable persistence for device heartbeats."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL_SECONDS = 1.0
_DEFAULT_MAX_BATCH_SIZE = 1024
_DEFAULT_MAX_WRITE_BATCH_SIZE = 128
_DEFAULT_MAX_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
_DEFAULT_FLUSH_TIMEOUT_SECONDS = 10.0


def _consume_background_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve a detached shutdown task result to avoid warning leaks."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


@dataclass(frozen=True, slots=True)
class _PendingDeviceHeartbeat:
    device_id: str
    last_heartbeat_at: str
    load_json: str
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


class DeviceHeartbeatBatcher:
    """Keep the latest heartbeat per device and persist it off the ingress path."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        flush_interval_seconds: float | None = None,
        max_batch_size: int | None = None,
        max_write_batch_size: int | None = None,
        max_retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
        flush_timeout_seconds: float | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._flush_interval_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_FLUSH_INTERVAL_SECONDS",
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
                    "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_BATCH_SIZE",
                    _DEFAULT_MAX_BATCH_SIZE,
                )
                if max_batch_size is None
                else int(max_batch_size)
            ),
        )
        self._max_write_batch_size = max(
            1,
            min(
                self._max_batch_size,
                (
                    _env_int(
                        "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_WRITE_BATCH_SIZE",
                        _DEFAULT_MAX_WRITE_BATCH_SIZE,
                    )
                    if max_write_batch_size is None
                    else int(max_write_batch_size)
                ),
            ),
        )
        self._max_retry_attempts = max(
            1,
            (
                _env_int(
                    "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_MAX_RETRY_ATTEMPTS",
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
                    "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_RETRY_BACKOFF_SECONDS",
                    _DEFAULT_RETRY_BACKOFF_SECONDS,
                )
                if retry_backoff_seconds is None
                else float(retry_backoff_seconds)
            ),
        )
        self._flush_timeout_seconds = max(
            0.0,
            (
                _env_float(
                    "NORTHCUA_CONTROLLER_DEVICE_HEARTBEAT_FLUSH_TIMEOUT_SECONDS",
                    _DEFAULT_FLUSH_TIMEOUT_SECONDS,
                )
                if flush_timeout_seconds is None
                else float(flush_timeout_seconds)
            ),
        )
        self._pending: dict[str, _PendingDeviceHeartbeat] = {}
        self._flush_wakeup = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._flush_immediate = False
        self._closing = False
        self._closed = False
        self._published = 0
        self._persisted = 0
        self._coalesced = 0
        self._failures = 0
        self._dropped = 0
        self._skipped = 0
        self._queue_age_ms_max = 0
        self._in_flight_publish_count = 0

    def publish(self, *, device_id: str, last_heartbeat_at: str, load_json: str) -> None:
        """Queue one heartbeat in memory and return without waiting for SQLite."""

        if self._closing or self._closed:
            self._dropped += 1
            return
        now = time.monotonic()
        previous = self._pending.get(device_id)
        self._pending[device_id] = _PendingDeviceHeartbeat(
            device_id=device_id,
            last_heartbeat_at=last_heartbeat_at,
            load_json=load_json,
            first_queued_at=previous.first_queued_at if previous is not None else now,
            publish_count=(previous.publish_count + 1) if previous is not None else 1,
        )
        self._published += 1
        if previous is not None:
            self._coalesced += 1
        self._ensure_flush_task()

    def snapshot(self) -> dict[str, int]:
        """Return bounded operational counters for controller telemetry."""

        return {
            "pending_devices": len(self._pending),
            "published": self._published,
            "persisted": self._persisted,
            "coalesced": self._coalesced,
            "failures": self._failures,
            "dropped": self._dropped,
            "skipped": self._skipped,
            "queue_age_ms_max": self._queue_age_ms_max,
            "in_flight_publishes": self._in_flight_publish_count,
            "max_batch_size": self._max_batch_size,
            "max_write_batch_size": self._max_write_batch_size,
        }

    async def flush(self, *, timeout_seconds: float | None = None) -> None:
        """Best-effort persist pending heartbeats within a bounded deadline."""

        if self._closed:
            return
        timeout = self._flush_timeout_seconds if timeout_seconds is None else max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout
        self._flush_immediate = True
        self._flush_wakeup.set()
        self._ensure_flush_task(immediate=True)
        while True:
            task = self._flush_task
            if task is None:
                return
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining <= 0:
                logger.warning(
                    "controller device heartbeat flush deadline elapsed",
                    extra={"pending_devices": len(self._pending), "timeout_seconds": timeout},
                )
                return
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                logger.warning(
                    "controller device heartbeat flush deadline elapsed",
                    extra={"pending_devices": len(self._pending), "timeout_seconds": timeout},
                )
                return
            if not self._pending:
                self._flush_immediate = False
                return
            self._ensure_flush_task(immediate=True)

    async def aclose(self) -> None:
        """Bounded best-effort flush followed by cancellation of background work."""

        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_impl(),
                name="northcua-device-heartbeat-batcher-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        self._closing = True
        await self.flush()
        self._closed = True
        self._dropped += sum(item.publish_count for item in self._pending.values())
        self._pending.clear()
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            done, still_running = await asyncio.wait({task}, timeout=self._flush_timeout_seconds)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for pending_task in still_running:
                pending_task.add_done_callback(_consume_background_task_result)
            if self._in_flight_publish_count:
                self._dropped += self._in_flight_publish_count
                self._in_flight_publish_count = 0
            if still_running:
                logger.warning(
                    "controller device heartbeat close deadline elapsed",
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
            name="northcua-device-heartbeat-batcher",
        )

    async def _flush_loop(self, *, initial_delay_seconds: float) -> None:
        delay = initial_delay_seconds
        consecutive_failures = 0
        try:
            while self._pending and not self._closed:
                if delay > 0 and not self._flush_immediate:
                    try:
                        await asyncio.wait_for(self._flush_wakeup.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    self._flush_wakeup.clear()
                device_ids = list(self._pending)[: self._max_write_batch_size]
                batch = [self._pending.pop(device_id) for device_id in device_ids]
                self._in_flight_publish_count = sum(item.publish_count for item in batch)
                started_at = time.monotonic()
                try:
                    updated = await controller_store_call(
                        self._db_path,
                        lambda store, batch=batch: store.heartbeat_devices_batch(
                            [(item.last_heartbeat_at, item.load_json, item.device_id) for item in batch]
                        ),
                        operation_name="device_heartbeat_batch",
                        priority=ControllerStorePriority.BACKGROUND,
                    )
                except Exception:
                    self._failures += 1
                    consecutive_failures += 1
                    exhausted = consecutive_failures >= self._max_retry_attempts
                    if exhausted:
                        self._dropped += sum(item.publish_count for item in batch)
                    else:
                        for item in batch:
                            if item.device_id not in self._pending:
                                self._pending[item.device_id] = item
                    self._in_flight_publish_count = 0
                    logger.warning(
                        "controller device heartbeat batch persistence failed",
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
                    self._persisted += updated
                    self._skipped += max(len(batch) - updated, 0)
                    age_ms = int(max((started_at - item.first_queued_at) * 1000 for item in batch))
                    self._queue_age_ms_max = max(self._queue_age_ms_max, age_ms)
                    self._in_flight_publish_count = 0
                    delay = (
                        0.0
                        if self._flush_immediate or len(self._pending) >= self._max_write_batch_size
                        else self._flush_interval_seconds
                    )
        finally:
            if not self._pending:
                self._flush_immediate = False
            self._flush_task = None
            if self._pending and not self._closed and not self._closing:
                self._ensure_flush_task()


__all__ = ["DeviceHeartbeatBatcher"]
