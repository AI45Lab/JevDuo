"""Batch-side controller backpressure guard."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig

logger = logging.getLogger(__name__)
_StopRequested = Callable[[], bool | Awaitable[bool]]

_RUNTIME_TELEMETRY_QUERY = """
SELECT payload_json, created_at
FROM events
WHERE event_type='controller_runtime_telemetry'
ORDER BY created_at DESC, event_id DESC
LIMIT ?
"""


async def _stop_requested_fired(stop_requested: _StopRequested | None) -> bool:
    """Evaluate a sync or async stop predicate without blocking the loop."""

    if stop_requested is None:
        return False
    result = stop_requested()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


@dataclass(frozen=True, slots=True)
class BackpressureSnapshot:
    """Latest controller pressure view used by batch throttling.

    Attributes:
        active: Whether callers should delay new controller/sandbox work.
        reason: Comma-separated threshold names that triggered pressure.
        samples: Number of recent controller telemetry samples considered.
        latest_event_loop_lag_ms: Most recent controller event-loop lag.
        latest_recv_q_max: Most recent maximum socket receive queue depth.
        sample_duration_seconds: Time spent reading the controller DB.
        sample_timed_out: Whether the latest DB sample hit the hard timeout.
        sample_error: Redacted sampling failure class, if any.
    """

    active: bool
    reason: str = ""
    samples: int = 0
    latest_event_loop_lag_ms: float = 0.0
    latest_recv_q_max: float = 0.0
    sample_duration_seconds: float = 0.0
    sample_timed_out: bool = False
    sample_error: str = ""


class ControllerBackpressureGate:
    """Pause new batch pressure while the controller event loop is backed up."""

    def __init__(
        self,
        *,
        controller_db_path: Path | None,
        enabled: bool = True,
        loop_lag_threshold_ms: float = 10_000.0,
        recv_q_max_threshold: float = 120_000.0,
        sustained_samples: int = 3,
        sleep_seconds: float = 5.0,
        sample_interval_seconds: float = 2.0,
        max_sample_age_seconds: float = 120.0,
        sample_timeout_seconds: float = 1.0,
        slow_sample_log_seconds: float = 0.5,
    ) -> None:
        self.controller_db_path = controller_db_path
        self.enabled = enabled and controller_db_path is not None
        self.loop_lag_threshold_ms = max(loop_lag_threshold_ms, 0.0)
        self.recv_q_max_threshold = max(recv_q_max_threshold, 0.0)
        self.sustained_samples = max(int(sustained_samples), 1)
        self.sleep_seconds = max(sleep_seconds, 0.1)
        self.sample_interval_seconds = max(sample_interval_seconds, 0.1)
        self.max_sample_age_seconds = max(max_sample_age_seconds, 1.0)
        self.sample_timeout_seconds = max(sample_timeout_seconds, 0.001)
        self.slow_sample_log_seconds = max(slow_sample_log_seconds, 0.0)
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._cached_at = 0.0
        self._cached_snapshot = BackpressureSnapshot(active=False)
        self._sample_task: asyncio.Task[None] | None = None
        self._sampler_task: asyncio.Task[None] | None = None
        self._last_active_at = 0.0
        self._last_log_at_by_scope: dict[str, float] = {}
        self._last_sample_log_at_by_reason: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: BatchRunConfig) -> "ControllerBackpressureGate":
        """Create a gate from run config and environment overrides.

        Args:
            config: Batch run configuration containing the controller DB path.

        Returns:
            Backpressure gate configured from environment thresholds.
        """

        return cls(
            controller_db_path=config.telemetry_controller_db_path,
            enabled=_env_bool("NORTHCUA_BATCH_BACKPRESSURE_ENABLED", True),
            loop_lag_threshold_ms=_env_float("NORTHCUA_BATCH_BACKPRESSURE_LOOP_LAG_MS", 10_000.0),
            recv_q_max_threshold=_env_float("NORTHCUA_BATCH_BACKPRESSURE_RECV_Q_MAX", 120_000.0),
            sustained_samples=_env_int("NORTHCUA_BATCH_BACKPRESSURE_SUSTAINED_SAMPLES", 3),
            sleep_seconds=_env_float("NORTHCUA_BATCH_BACKPRESSURE_SLEEP_SECONDS", 5.0),
            sample_interval_seconds=_env_float("NORTHCUA_BATCH_BACKPRESSURE_SAMPLE_INTERVAL_SECONDS", 2.0),
            max_sample_age_seconds=_env_float("NORTHCUA_BATCH_BACKPRESSURE_MAX_SAMPLE_AGE_SECONDS", 120.0),
            sample_timeout_seconds=_env_float("NORTHCUA_BATCH_BACKPRESSURE_SAMPLE_TIMEOUT_SECONDS", 1.0),
            slow_sample_log_seconds=_env_float("NORTHCUA_BATCH_BACKPRESSURE_SLOW_SAMPLE_LOG_SECONDS", 0.5),
        )

    async def start(self) -> None:
        """Start the background sampler.

        The initial sample is bounded by ``sample_timeout_seconds`` and happens
        before workers start, so submit/claim hot paths only read memory.
        """

        if not self.enabled or self.controller_db_path is None:
            return
        async with self._lock:
            if self._sampler_task is not None and not self._sampler_task.done():
                return
        await self._refresh_snapshot()
        async with self._lock:
            if self._sampler_task is None or self._sampler_task.done():
                self._sampler_task = asyncio.create_task(
                    self._sample_loop(),
                    name="northcua-controller-backpressure-sampler",
                )

    async def snapshot(self) -> BackpressureSnapshot:
        """Return the cached throttling snapshot and refresh in the background.

        Returns:
            Cached pressure state for the controller. Sampling is detached from
            submit/claim hot paths so slow SQLite reads cannot hold workers in
            ``WAITING_DEVICE``.
        """

        if not self.enabled or self.controller_db_path is None:
            return BackpressureSnapshot(active=False)
        now = time.monotonic()
        async with self._lock:
            self._ensure_sample_task_locked(now)
            return self._cached_snapshot

    async def refresh_now(self) -> BackpressureSnapshot:
        """Synchronously refresh the snapshot for tests and maintenance probes.

        Returns:
            The freshly sampled pressure state, or an inactive snapshot when
            sampling is disabled or no controller DB is configured.
        """

        if not self.enabled or self.controller_db_path is None:
            return BackpressureSnapshot(active=False)
        await self._refresh_snapshot()
        return self._cached_snapshot

    def cached_snapshot(self) -> BackpressureSnapshot:
        """Return the last completed sample without scheduling new work.

        Returns:
            Cached pressure state, or an inactive snapshot when disabled.
        """

        return self._cached_snapshot if self.enabled else BackpressureSnapshot(active=False)

    def recently_active(self, *, window_seconds: float) -> bool:
        """Return whether a completed sample was active inside the window.

        Args:
            window_seconds: Lookback window measured from the current monotonic
                clock sample.

        Returns:
            True when a prior sample reported active backpressure recently.
        """

        return (
            window_seconds > 0
            and self._last_active_at > 0
            and time.monotonic() - self._last_active_at <= window_seconds
        )

    async def close(self) -> None:
        """Cancel an in-flight background sampler."""

        tasks = [task for task in (self._sampler_task, self._sample_task) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_if_needed(
        self,
        *,
        scope: str,
        stop_requested: _StopRequested | None = None,
    ) -> bool:
        """Wait until controller pressure clears.

        Args:
            scope: Operator label for the batch action being held.
            stop_requested: Optional callback that returns true when batch
                control flags should stop the wait.

        Returns:
            ``True`` if the caller may proceed, ``False`` if a stop flag fired.
        """

        if not self.enabled:
            return True
        while True:
            if await _stop_requested_fired(stop_requested):
                return False
            snapshot = await self.snapshot()
            if not snapshot.active:
                return True
            self._log_wait(scope=scope, snapshot=snapshot)
            await asyncio.sleep(self.sleep_seconds)

    async def active_snapshot(self, *, scope: str) -> BackpressureSnapshot:
        """Return and log an active snapshot without sleeping.

        Args:
            scope: Operator label for the batch action being sampled.

        Returns:
            Current pressure snapshot without waiting for recovery.
        """

        snapshot = await self.snapshot()
        if snapshot.active:
            self._log_wait(scope=scope, snapshot=snapshot)
        return snapshot

    def _log_wait(self, *, scope: str, snapshot: BackpressureSnapshot) -> None:
        now = time.monotonic()
        last = self._last_log_at_by_scope.get(scope, 0.0)
        if now - last < 60.0:
            return
        self._last_log_at_by_scope[scope] = now
        logger.warning(
            "controller backpressure delaying batch work",
            extra={
                "scope": scope,
                "reason": snapshot.reason,
                "samples": snapshot.samples,
                "event_loop_lag_ms": snapshot.latest_event_loop_lag_ms,
                "recv_q_max": snapshot.latest_recv_q_max,
                "controller_db_path": str(self.controller_db_path or ""),
                "sample_duration_seconds": snapshot.sample_duration_seconds,
                "sample_timed_out": snapshot.sample_timed_out,
                "sample_error": snapshot.sample_error,
            },
        )

    def _ensure_sample_task_locked(self, now: float) -> None:
        if self._sampler_task is not None and not self._sampler_task.done():
            return
        if self._sample_task is not None and not self._sample_task.done():
            return
        if self._cached_at > 0 and now - self._cached_at < self.sample_interval_seconds:
            return
        self._sample_task = asyncio.create_task(self._refresh_snapshot(), name="northcua-controller-backpressure")

    async def _sample_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.sample_interval_seconds)
                await self._refresh_snapshot()
        except asyncio.CancelledError:
            raise

    async def _refresh_snapshot(self) -> None:
        async with self._refresh_lock:
            try:
                controller_db_path = self.controller_db_path
                if controller_db_path is None:
                    snapshot = BackpressureSnapshot(active=False, reason="controller_db_unavailable")
                    now = time.monotonic()
                    async with self._lock:
                        self._cached_snapshot = snapshot
                        self._cached_at = now
                    return
                snapshot = await asyncio.to_thread(
                    _read_backpressure_snapshot,
                    controller_db_path,
                    loop_lag_threshold_ms=self.loop_lag_threshold_ms,
                    recv_q_max_threshold=self.recv_q_max_threshold,
                    sustained_samples=self.sustained_samples,
                    max_sample_age_seconds=self.max_sample_age_seconds,
                    query_timeout_seconds=self.sample_timeout_seconds,
                )
            except Exception as exc:
                logger.debug("controller backpressure background sample failed", exc_info=True)
                snapshot = BackpressureSnapshot(
                    active=False,
                    reason=type(exc).__name__,
                    sample_error=type(exc).__name__,
                )
        now = time.monotonic()
        async with self._lock:
            preserved_cached_active = False
            if (
                (snapshot.sample_timed_out or snapshot.sample_error)
                and self._cached_snapshot.active
                and self._cached_at > 0
                and now - self._cached_at <= self.max_sample_age_seconds
            ):
                preserved_cached_active = True
                snapshot = replace(
                    self._cached_snapshot,
                    sample_duration_seconds=snapshot.sample_duration_seconds,
                    sample_timed_out=snapshot.sample_timed_out,
                    sample_error=snapshot.sample_error,
                )
            if not preserved_cached_active:
                self._cached_at = now
            self._cached_snapshot = snapshot
            if snapshot.active and not preserved_cached_active:
                self._last_active_at = now
            self._log_sample_if_needed(snapshot=snapshot)

    def _log_sample_if_needed(self, *, snapshot: BackpressureSnapshot) -> None:
        reason = ""
        if snapshot.sample_timed_out:
            reason = "timeout"
        elif snapshot.sample_error:
            reason = snapshot.sample_error
        elif snapshot.sample_duration_seconds >= self.slow_sample_log_seconds > 0:
            reason = "slow"
        if not reason:
            return
        now = time.monotonic()
        last = self._last_sample_log_at_by_reason.get(reason, 0.0)
        if now - last < 30.0:
            return
        self._last_sample_log_at_by_reason[reason] = now
        logger.warning(
            "controller backpressure sample slow or failed",
            extra={
                "reason": reason,
                "active": snapshot.active,
                "snapshot_reason": snapshot.reason,
                "samples": snapshot.samples,
                "event_loop_lag_ms": snapshot.latest_event_loop_lag_ms,
                "recv_q_max": snapshot.latest_recv_q_max,
                "sample_duration_seconds": snapshot.sample_duration_seconds,
                "sample_timed_out": snapshot.sample_timed_out,
                "sample_error": snapshot.sample_error,
                "controller_db_path": str(self.controller_db_path or ""),
            },
        )


def _read_backpressure_snapshot(
    controller_db_path: Path,
    *,
    loop_lag_threshold_ms: float,
    recv_q_max_threshold: float,
    sustained_samples: int,
    max_sample_age_seconds: float,
    query_timeout_seconds: float = 1.0,
) -> BackpressureSnapshot:
    if not controller_db_path.exists():
        return BackpressureSnapshot(active=False)
    started_at = time.monotonic()
    if query_timeout_seconds <= 0:
        return BackpressureSnapshot(active=False, reason="sample_timeout", sample_timed_out=True)
    try:
        with sqlite3.connect(controller_db_path, timeout=min(query_timeout_seconds, 1.0)) as connection:
            deadline = time.monotonic() + query_timeout_seconds

            def _abort_slow_query() -> int:
                return 1 if time.monotonic() >= deadline else 0

            connection.set_progress_handler(_abort_slow_query, 1000)
            rows = connection.execute(_RUNTIME_TELEMETRY_QUERY, (sustained_samples,)).fetchall()
            connection.set_progress_handler(None, 0)
    except sqlite3.OperationalError as exc:
        duration = round(time.monotonic() - started_at, 3)
        if "interrupted" in str(exc).lower():
            return BackpressureSnapshot(
                active=False,
                reason="sample_timeout",
                sample_duration_seconds=duration,
                sample_timed_out=True,
                sample_error=type(exc).__name__,
            )
        logger.debug("controller backpressure sample failed", exc_info=True)
        return BackpressureSnapshot(
            active=False,
            reason=type(exc).__name__,
            sample_duration_seconds=duration,
            sample_error=type(exc).__name__,
        )
    except sqlite3.Error as exc:
        logger.debug("controller backpressure sample failed", exc_info=True)
        return BackpressureSnapshot(
            active=False,
            reason=type(exc).__name__,
            sample_duration_seconds=round(time.monotonic() - started_at, 3),
            sample_error=type(exc).__name__,
        )
    duration = round(time.monotonic() - started_at, 3)
    if len(rows) < sustained_samples:
        return BackpressureSnapshot(active=False, samples=len(rows), sample_duration_seconds=duration)
    now = time.time()
    created_ats = [_parse_time_to_epoch(str(row[1] or "")) for row in rows]
    recent_created_ats = [
        created_at for created_at in created_ats if created_at > 0 and now - created_at <= max_sample_age_seconds
    ]
    if len(recent_created_ats) < sustained_samples:
        return BackpressureSnapshot(
            active=False,
            samples=len(recent_created_ats),
            reason="stale_controller_telemetry",
            sample_duration_seconds=duration,
        )
    payloads = [_loads(str(row[0] or "{}")) for row in rows]
    latest = payloads[0]
    bad_samples = [
        payload
        for payload in payloads
        if _float(payload.get("event_loop_lag_ms")) > loop_lag_threshold_ms
        or _float(payload.get("recv_q_max")) > recv_q_max_threshold
    ]
    latest_lag = _float(latest.get("event_loop_lag_ms"))
    latest_recv_q = _float(latest.get("recv_q_max"))
    if len(bad_samples) < sustained_samples:
        return BackpressureSnapshot(
            active=False,
            samples=len(rows),
            latest_event_loop_lag_ms=latest_lag,
            latest_recv_q_max=latest_recv_q,
            sample_duration_seconds=duration,
        )
    reasons: list[str] = []
    if any(_float(payload.get("event_loop_lag_ms")) > loop_lag_threshold_ms for payload in payloads):
        reasons.append(f"event_loop_lag_ms>{loop_lag_threshold_ms:g}")
    if any(_float(payload.get("recv_q_max")) > recv_q_max_threshold for payload in payloads):
        reasons.append(f"recv_q_max>{recv_q_max_threshold:g}")
    return BackpressureSnapshot(
        active=True,
        reason=",".join(reasons),
        samples=len(rows),
        latest_event_loop_lag_ms=latest_lag,
        latest_recv_q_max=latest_recv_q,
        sample_duration_seconds=duration,
    )


def _loads(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _float(value: object) -> float:
    if not isinstance(value, str | int | float):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _parse_time_to_epoch(value: str) -> float:
    raw = value.strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


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
