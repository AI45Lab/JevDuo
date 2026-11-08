"""Out-of-band batch runner event-loop lag diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import TracebackType

from northcua.telemetry.contracts import utc_now_iso

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL_SECONDS = 0.5
_DEFAULT_TICK_INTERVAL_SECONDS = 0.25
_DEFAULT_LAG_THRESHOLD_SECONDS = 5.0
_DEFAULT_LOG_INTERVAL_SECONDS = 10.0
_DEFAULT_STACK_LIMIT = 48


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class RunnerEventLoopWatchdog:
    """Record runner event-loop lag and the blocked loop thread stack.

    The controller already emits event-loop lag telemetry, but the batch runner
    is a separate process. A coroutine-only lag probe runs after a blockage has
    already cleared, so it cannot capture the blocking stack. This watchdog uses
    a tiny side thread to observe missed heartbeats and snapshot the loop thread
    while it is still stalled.
    """

    def __init__(self, *, output_dir: Path, batch_id: str) -> None:
        self._output_dir = output_dir
        self._batch_id = batch_id
        self._path = output_dir / "batch_runner_runtime_events.jsonl"
        self._check_interval_seconds = max(
            _env_float(
                "NORTHCUA_BATCH_RUNNER_LOOP_LAG_CHECK_INTERVAL_SECONDS",
                _DEFAULT_CHECK_INTERVAL_SECONDS,
            ),
            0.05,
        )
        self._tick_interval_seconds = max(
            _env_float(
                "NORTHCUA_BATCH_RUNNER_LOOP_LAG_TICK_INTERVAL_SECONDS",
                _DEFAULT_TICK_INTERVAL_SECONDS,
            ),
            0.05,
        )
        self._threshold_seconds = max(
            _env_float(
                "NORTHCUA_BATCH_RUNNER_LOOP_LAG_THRESHOLD_SECONDS",
                _DEFAULT_LAG_THRESHOLD_SECONDS,
            ),
            0.0,
        )
        self._log_interval_seconds = max(
            _env_float(
                "NORTHCUA_BATCH_RUNNER_LOOP_LAG_LOG_INTERVAL_SECONDS",
                _DEFAULT_LOG_INTERVAL_SECONDS,
            ),
            0.0,
        )
        self._stack_limit = max(_env_int("NORTHCUA_BATCH_RUNNER_LOOP_LAG_STACK_LIMIT", _DEFAULT_STACK_LIMIT), 1)
        self._last_tick = time.monotonic()
        self._last_emit_at = 0.0
        self._loop_thread_id = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        """Return the JSONL destination for watchdog events.

        Returns:
            Runtime event file written by the side-thread lag sampler.
        """

        return self._path

    async def __aenter__(self) -> "RunnerEventLoopWatchdog":
        if self._threshold_seconds <= 0:
            return self
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._loop_thread_id = threading.get_ident()
        self._last_tick = time.monotonic()
        self._stop.clear()
        self._task = asyncio.create_task(self._heartbeat_loop(), name="northcua-runner-loop-watchdog-heartbeat")
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="northcua-runner-loop-watchdog",
            daemon=True,
        )
        self._thread.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=max(self._check_interval_seconds * 2, 1.0))
            if self._thread.is_alive():
                logger.warning(
                    "batch runner event-loop watchdog thread did not stop before timeout",
                    extra={"path": str(self._path), "batch_id": self._batch_id},
                )
        self._task = None
        self._thread = None

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._set_last_tick(time.monotonic())
            await asyncio.sleep(self._tick_interval_seconds)

    def _set_last_tick(self, value: float) -> None:
        with self._lock:
            self._last_tick = value

    def _get_last_tick(self) -> float:
        with self._lock:
            return self._last_tick

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(self._check_interval_seconds):
            now = time.monotonic()
            lag_seconds = max(now - self._get_last_tick() - self._tick_interval_seconds, 0.0)
            if lag_seconds < self._threshold_seconds:
                continue
            if self._log_interval_seconds > 0 and now - self._last_emit_at < self._log_interval_seconds:
                continue
            self._last_emit_at = now
            payload = self._lag_payload(lag_seconds=lag_seconds)
            self._write_payload(payload)

    def _lag_payload(self, *, lag_seconds: float) -> dict[str, object]:
        frame = sys._current_frames().get(self._loop_thread_id)
        stack = traceback.format_stack(frame, limit=self._stack_limit) if frame is not None else []
        return {
            "timestamp": utc_now_iso(),
            "batch_id": self._batch_id,
            "message": "runner_event_loop_lag",
            "lag_seconds": round(lag_seconds, 3),
            "threshold_seconds": round(self._threshold_seconds, 3),
            "tick_interval_seconds": round(self._tick_interval_seconds, 3),
            "loop_thread_id": self._loop_thread_id,
            "stack": stack,
        }

    def _write_payload(self, payload: dict[str, object]) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("batch runner event-loop watchdog failed to write telemetry", exc_info=True)
