"""Bounded executors for synchronous E2B SDK calls."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import heapq
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

logger = logging.getLogger(__name__)

E2BExecutorLane = Literal["control", "command"]
_ResultT = TypeVar("_ResultT")

_DEFAULT_CONTROL_WORKERS = 64
_DEFAULT_COMMAND_WORKERS = 256
_MAX_CONTROL_WORKERS = 256
_MAX_COMMAND_WORKERS = 512
_EXECUTOR_MAX_PENDING = {
    "control": 2048,
    "command": 2048,
}
_REAPER_QUEUE_MAX_SIZE = 4096
_REAPER_WORKERS = 16
_REAPER_FALLBACK_WORKERS = 4
_REAPER_FALLBACK_MAX_PENDING = 4096
_REAPER_EXIT_TIMEOUT_SECONDS = 30.0
_EXECUTOR_ENV = {
    "control": "NORTHCUA_E2B_CONTROL_EXECUTOR_WORKERS",
    "command": "NORTHCUA_E2B_COMMAND_EXECUTOR_WORKERS",
}
_EXECUTOR_DEFAULTS = {
    "control": _DEFAULT_CONTROL_WORKERS,
    "command": _DEFAULT_COMMAND_WORKERS,
}
_EXECUTOR_MAX_WORKERS = {
    "control": _MAX_CONTROL_WORKERS,
    "command": _MAX_COMMAND_WORKERS,
}

_executor_lock = threading.Lock()
_executor_pid = os.getpid()
_executors: dict[E2BExecutorLane, _BoundedThreadPoolExecutor] = {}
_reaper_lock = threading.Lock()
_reaper_condition = threading.Condition(_reaper_lock)
_reaper_pid = os.getpid()
_reaper_queue: queue.Queue[_ReaperJob | _ReaperStop] = queue.Queue(maxsize=_REAPER_QUEUE_MAX_SIZE)
_reaper_threads: list[threading.Thread] = []
_reaper_accepting = True
_reaper_submitted_sequence = 0
_reaper_completed_through = 0
_reaper_completed_sequences: set[int] = set()
_reaper_fallback_running = 0
_reaper_scheduled: list[_ScheduledReaperJob] = []
_reaper_scheduler_thread: threading.Thread | None = None
_reaper_campaign_remaining: dict[str, int] = {}
_reaper_fallback_executor: _BoundedFallbackExecutor | None = None


@dataclass
class _LaneTelemetry:
    queued: int = 0
    running: int = 0
    submitted_total: int = 0
    sdk_started_total: int = 0
    sdk_finished_total: int = 0
    sdk_failed_total: int = 0
    queued_cancelled_total: int = 0
    submission_rejected_total: int = 0
    worker_claimed_waiter_cancelled_total: int = 0
    max_queue_seconds: float = 0.0
    max_sdk_seconds: float = 0.0


@dataclass(frozen=True)
class _ReaperJob:
    sequence: int
    operation: str
    job: Callable[[], None]
    enqueued_at: float
    campaign_key: str | None = None


@dataclass(order=True, frozen=True)
class _ScheduledReaperJob:
    due_at: float
    sequence: int
    item: _ReaperJob = field(compare=False)


@dataclass(frozen=True)
class _ReaperStop:
    pass


@dataclass
class _ReaperTelemetry:
    submitted_total: int = 0
    completed_total: int = 0
    failed_total: int = 0
    rejected_total: int = 0
    queue_full_total: int = 0
    fallback_started_total: int = 0
    fallback_completed_total: int = 0
    fallback_failed_total: int = 0
    campaign_deduplicated_total: int = 0
    scheduled_total: int = 0
    max_queue_seconds: float = 0.0


class E2BExecutorQueueFull(RuntimeError):
    """Raised when a lane reaches its hard in-flight submission bound."""


class _BoundedFallbackExecutor:
    """Minimal fixed-size daemon pool for post-shutdown exact cleanup."""

    def __init__(self, *, max_workers: int, max_pending: int) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=max_pending)
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"northcua-e2b-reaper-fallback-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full as exc:
            raise E2BExecutorQueueFull("E2B reaper fallback queue is full") from exc

    def _run(self) -> None:
        while True:
            self._queue.get()()


class _BoundedWorkQueue:
    """ThreadPoolExecutor-compatible queue with a hard physical work-item bound."""

    def __init__(self, max_pending: int) -> None:
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._slots = threading.BoundedSemaphore(max_pending)

    def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
        del block, timeout
        if item is not None and not self._slots.acquire(blocking=False):
            raise queue.Full
        self._queue.put(item)

    def get(self, block: bool = True, timeout: float | None = None) -> object:
        item = self._queue.get(block=block, timeout=timeout)
        if item is not None:
            self._slots.release()
        return item

    def get_nowait(self) -> object:
        return self.get(block=False)

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()


class _BoundedThreadPoolExecutor(ThreadPoolExecutor):
    """Thread pool with a non-blocking hard bound on physically queued work."""

    def __init__(self, *, max_workers: int, max_pending: int, thread_name_prefix: str) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._work_queue = _BoundedWorkQueue(max_pending)  # type: ignore[assignment]
        self._max_pending = max_pending

    def submit(self, fn: Callable[..., _ResultT], /, *args: object, **kwargs: object) -> Future[_ResultT]:
        try:
            return super().submit(fn, *args, **kwargs)
        except queue.Full as exc:
            raise E2BExecutorQueueFull(
                f"E2B executor saturated: workers={self._max_workers} pending={self._max_pending}"
            ) from exc


_telemetry_lock = threading.Lock()
_lane_telemetry = {
    "control": _LaneTelemetry(),
    "command": _LaneTelemetry(),
}
_reaper_telemetry = _ReaperTelemetry()


def _reset_executors_after_fork() -> None:
    """Discard parent executor state without acquiring an inherited lock."""

    global _executor_lock, _executor_pid, _reaper_accepting, _reaper_completed_sequences
    global _reaper_campaign_remaining, _reaper_fallback_running, _reaper_scheduled
    global _reaper_fallback_executor, _reaper_scheduler_thread
    global _reaper_completed_through, _reaper_condition, _reaper_lock, _reaper_pid, _reaper_queue
    global _reaper_submitted_sequence, _reaper_telemetry, _reaper_threads, _telemetry_lock
    _executor_lock = threading.Lock()
    _telemetry_lock = threading.Lock()
    _executors.clear()
    _reaper_lock = threading.Lock()
    _reaper_condition = threading.Condition(_reaper_lock)
    _reaper_pid = os.getpid()
    _reaper_queue = queue.Queue(maxsize=_REAPER_QUEUE_MAX_SIZE)
    _reaper_threads = []
    _reaper_accepting = True
    _reaper_submitted_sequence = 0
    _reaper_completed_through = 0
    _reaper_completed_sequences = set()
    _reaper_fallback_running = 0
    _reaper_scheduled = []
    _reaper_scheduler_thread = None
    _reaper_campaign_remaining = {}
    _reaper_fallback_executor = None
    _reaper_telemetry = _ReaperTelemetry()
    _reset_telemetry()
    _executor_pid = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_executors_after_fork)


@dataclass(frozen=True)
class E2BExecutorTiming:
    """Point-in-time timing for one submitted SDK call."""

    executor_lane: E2BExecutorLane
    wait_state: str
    executor_queue_seconds: float
    sdk_call_seconds: float
    total_seconds: float

    def progress_fields(self) -> dict[str, object]:
        """Return rounded fields suitable for provider progress output."""

        return {
            "executor_lane": self.executor_lane,
            "wait_state": self.wait_state,
            "executor_queue_seconds": round(self.executor_queue_seconds, 3),
            "sdk_call_seconds": round(self.sdk_call_seconds, 3),
            "total_seconds": round(self.total_seconds, 3),
        }


class E2BExecutorCall(Generic[_ResultT]):
    """One synchronous SDK call submitted to a process-shared executor."""

    def __init__(self, lane: E2BExecutorLane, operation: Callable[[], _ResultT]) -> None:
        self._lane: E2BExecutorLane = lane
        self._operation = operation
        self._enqueued_at = time.perf_counter()
        self._started_at = 0.0
        self._finished_at = 0.0
        self._timing_lock = threading.Lock()
        self._submitted = False
        self._counted_queued = False
        self._async_future: asyncio.Future[_ResultT] | None = None
        self._concurrent_future: Future[_ResultT] | None = None

    async def run(self) -> _ResultT:
        """Run the operation without occupying the event-loop thread."""

        if self._submitted:
            raise RuntimeError("E2B executor call can only be submitted once")
        self._submitted = True
        with self._timing_lock:
            self._counted_queued = True
        _record_submitted(self._lane)
        loop = asyncio.get_running_loop()
        try:
            concurrent_future = _executor(self._lane).submit(self._invoke)
        except E2BExecutorQueueFull:
            with self._timing_lock:
                self._counted_queued = False
            _record_submission_rejected(self._lane)
            raise
        self._concurrent_future = concurrent_future
        async_future = asyncio.wrap_future(concurrent_future, loop=loop)
        self._async_future = async_future
        async_future.add_done_callback(_consume_async_future_exception)
        try:
            return await asyncio.shield(async_future)
        except asyncio.CancelledError:
            cancelled_while_queued = concurrent_future.cancel()
            if cancelled_while_queued:
                async_future.cancel()
            with self._timing_lock:
                if cancelled_while_queued:
                    self._counted_queued = False
            _record_cancelled(
                self._lane,
                queued=cancelled_while_queued,
                timing=self.timing(),
            )
            raise

    async def wait_for_completion(self) -> _ResultT:
        """Await the underlying SDK call after its original waiter is gone."""

        future = self._async_future
        if future is None:
            raise RuntimeError("E2B executor call has not been submitted")
        return await asyncio.shield(future)

    def add_concurrent_done_callback(self, callback: Callable[[Future[_ResultT]], None]) -> bool:
        """Attach loop-independent ownership to a submitted SDK result."""

        future = self._concurrent_future
        if future is None:
            return False
        future.add_done_callback(callback)
        return True

    def timing(self) -> E2BExecutorTiming:
        """Return queue, SDK, and total wall-clock timing so far."""

        now = time.perf_counter()
        with self._timing_lock:
            started_at = self._started_at
            finished_at = self._finished_at
        if started_at <= 0:
            return E2BExecutorTiming(
                executor_lane=self._lane,
                wait_state="executor_queue",
                executor_queue_seconds=max(now - self._enqueued_at, 0.0),
                sdk_call_seconds=0.0,
                total_seconds=max(now - self._enqueued_at, 0.0),
            )
        end_at = finished_at if finished_at > 0 else now
        return E2BExecutorTiming(
            executor_lane=self._lane,
            wait_state="sdk_call",
            executor_queue_seconds=max(started_at - self._enqueued_at, 0.0),
            sdk_call_seconds=max(end_at - started_at, 0.0),
            total_seconds=max(end_at - self._enqueued_at, 0.0),
        )

    def _invoke(self) -> _ResultT:
        with self._timing_lock:
            self._started_at = time.perf_counter()
            counted_queued = self._counted_queued
            self._counted_queued = False
        _record_started(self._lane, queued=counted_queued)
        failed = False
        try:
            return self._operation()
        except BaseException:
            failed = True
            raise
        finally:
            with self._timing_lock:
                self._finished_at = time.perf_counter()
            _record_finished(self._lane, timing=self.timing(), failed=failed)


def executor_telemetry(lane: E2BExecutorLane) -> dict[str, object]:
    """Return a bounded, secret-free snapshot for one executor lane."""

    with _telemetry_lock:
        state = _lane_telemetry[lane]
        return {
            "executor_workers": executor_workers(lane),
            "executor_queued": state.queued,
            "executor_running": state.running,
            "executor_submitted_total": state.submitted_total,
            "executor_sdk_started_total": state.sdk_started_total,
            "executor_sdk_finished_total": state.sdk_finished_total,
            "executor_sdk_failed_total": state.sdk_failed_total,
            "executor_queued_cancelled_total": state.queued_cancelled_total,
            "executor_submission_rejected_total": state.submission_rejected_total,
            "executor_worker_claimed_waiter_cancelled_total": state.worker_claimed_waiter_cancelled_total,
            "executor_max_queue_seconds": round(state.max_queue_seconds, 3),
            "executor_max_sdk_seconds": round(state.max_sdk_seconds, 3),
        }


def submit_e2b_reaper_job(
    *,
    operation: str,
    job: Callable[[], None],
    fallback_to_caller_thread: bool = False,
) -> bool:
    """Queue exact late-result cleanup without depending on an event loop."""

    global _reaper_fallback_running, _reaper_pid, _reaper_submitted_sequence
    current_pid = os.getpid()
    run_fallback = False
    with _reaper_condition:
        if current_pid != _reaper_pid:
            _reset_reaper_state_after_pid_change(current_pid)
        if not _reaper_accepting:
            with _telemetry_lock:
                _reaper_telemetry.rejected_total += 1
            run_fallback = fallback_to_caller_thread
        else:
            _ensure_reaper_workers_locked()
            sequence = _reaper_submitted_sequence + 1
            try:
                _reaper_queue.put_nowait(
                    _ReaperJob(
                        sequence=sequence,
                        operation=operation,
                        job=job,
                        enqueued_at=time.perf_counter(),
                    )
                )
            except queue.Full:
                with _telemetry_lock:
                    _reaper_telemetry.queue_full_total += 1
                logger.error(
                    "E2B late-result reaper queue is full",
                    extra={"operation": operation, "queue_max_size": _REAPER_QUEUE_MAX_SIZE},
                )
                run_fallback = fallback_to_caller_thread
            else:
                _reaper_submitted_sequence = sequence
                with _telemetry_lock:
                    _reaper_telemetry.submitted_total += 1
                return True
        if run_fallback:
            _reaper_fallback_running += 1
            with _telemetry_lock:
                _reaper_telemetry.fallback_started_total += 1
    if not run_fallback:
        return False
    _start_registered_reaper_fallback(operation=operation, job=job)
    return True


def submit_e2b_reaper_campaign(
    *,
    operation: str,
    jobs: tuple[tuple[float, Callable[[], None]], ...],
    dedupe_key: str,
) -> bool:
    """Schedule deduplicated delayed cleanup without sleeping in workers."""

    global _reaper_pid, _reaper_submitted_sequence
    if not jobs or not dedupe_key:
        return False
    normalized = tuple((max(float(delay), 0.0), job) for delay, job in jobs)
    current_pid = os.getpid()
    with _reaper_condition:
        if current_pid != _reaper_pid:
            _reset_reaper_state_after_pid_change(current_pid)
        if not _reaper_accepting:
            with _telemetry_lock:
                _reaper_telemetry.rejected_total += 1
            return False
        if dedupe_key in _reaper_campaign_remaining:
            with _telemetry_lock:
                _reaper_telemetry.campaign_deduplicated_total += 1
            return True
        outstanding = _reaper_submitted_sequence - _reaper_completed_through
        if outstanding + len(normalized) > _REAPER_QUEUE_MAX_SIZE:
            with _telemetry_lock:
                _reaper_telemetry.queue_full_total += 1
            logger.error(
                "E2B late-result reaper campaign capacity is full",
                extra={"operation": operation, "queue_max_size": _REAPER_QUEUE_MAX_SIZE},
            )
            return False

        _ensure_reaper_workers_locked()
        _ensure_reaper_scheduler_locked()
        accepted_at = time.perf_counter()
        _reaper_campaign_remaining[dedupe_key] = len(normalized)
        for delay, job in normalized:
            _reaper_submitted_sequence += 1
            item = _ReaperJob(
                sequence=_reaper_submitted_sequence,
                operation=operation,
                job=job,
                enqueued_at=accepted_at,
                campaign_key=dedupe_key,
            )
            heapq.heappush(
                _reaper_scheduled,
                _ScheduledReaperJob(
                    due_at=accepted_at + delay,
                    sequence=item.sequence,
                    item=item,
                ),
            )
        with _telemetry_lock:
            _reaper_telemetry.submitted_total += len(normalized)
            _reaper_telemetry.scheduled_total += len(normalized)
        _reaper_condition.notify_all()
    return True


def submit_e2b_reaper_fallback_job(
    *,
    operation: str,
    job: Callable[[], None],
) -> None:
    """Run rejected cleanup off-thread while retaining drain ownership."""

    global _reaper_fallback_running
    with _reaper_condition:
        _reaper_fallback_running += 1
        with _telemetry_lock:
            _reaper_telemetry.fallback_started_total += 1
    _start_registered_reaper_fallback(operation=operation, job=job)


def _start_registered_reaper_fallback(
    *,
    operation: str,
    job: Callable[[], None],
) -> None:
    """Start cleanup after its ownership count was registered under the fence."""

    global _reaper_fallback_running

    def run() -> None:
        global _reaper_fallback_running
        failed = False
        try:
            job()
        except BaseException:
            failed = True
            logger.exception(
                "E2B late-result fallback job failed",
                extra={"operation": operation},
            )
        finally:
            with _reaper_condition:
                _reaper_fallback_running -= 1
                with _telemetry_lock:
                    _reaper_telemetry.fallback_completed_total += 1
                    if failed:
                        _reaper_telemetry.fallback_failed_total += 1
                _reaper_condition.notify_all()

    try:
        _fallback_executor().submit(run)
    except BaseException:
        with _reaper_condition:
            _reaper_fallback_running -= 1
            with _telemetry_lock:
                _reaper_telemetry.fallback_completed_total += 1
                _reaper_telemetry.fallback_failed_total += 1
            _reaper_condition.notify_all()
        raise


def _fallback_executor() -> _BoundedFallbackExecutor:
    """Return the process-shared, hard-bounded shutdown fallback lane."""

    global _reaper_fallback_executor
    with _reaper_condition:
        executor = _reaper_fallback_executor
        if executor is None:
            executor = _BoundedFallbackExecutor(
                max_workers=_REAPER_FALLBACK_WORKERS,
                max_pending=_REAPER_FALLBACK_MAX_PENDING,
            )
            _reaper_fallback_executor = executor
        return executor


def cancel_e2b_reaper_campaign(dedupe_key: str) -> int:
    """Skip delayed passes after an earlier pass completed the cleanup."""

    if not dedupe_key:
        return 0
    with _reaper_condition:
        retained: list[_ScheduledReaperJob] = []
        removed: list[_ScheduledReaperJob] = []
        for scheduled in _reaper_scheduled:
            if scheduled.item.campaign_key == dedupe_key:
                removed.append(scheduled)
            else:
                retained.append(scheduled)
        if not removed:
            return 0
        _reaper_scheduled[:] = retained
        heapq.heapify(_reaper_scheduled)
        for scheduled in removed:
            _complete_reaper_sequence_locked(
                scheduled.item.sequence,
                campaign_key=scheduled.item.campaign_key,
            )
        _reaper_condition.notify_all()
        return len(removed)


def _reset_reaper_state_after_pid_change(current_pid: int) -> None:
    global _reaper_accepting, _reaper_completed_sequences, _reaper_completed_through
    global _reaper_campaign_remaining, _reaper_fallback_running, _reaper_pid, _reaper_queue
    global _reaper_scheduled, _reaper_scheduler_thread, _reaper_submitted_sequence, _reaper_threads
    _reaper_pid = current_pid
    _reaper_queue = queue.Queue(maxsize=_REAPER_QUEUE_MAX_SIZE)
    _reaper_threads = []
    _reaper_accepting = True
    _reaper_submitted_sequence = 0
    _reaper_completed_through = 0
    _reaper_completed_sequences = set()
    _reaper_fallback_running = 0
    _reaper_scheduled = []
    _reaper_scheduler_thread = None
    _reaper_campaign_remaining = {}


def _ensure_reaper_workers_locked() -> None:
    alive = [thread for thread in _reaper_threads if thread.is_alive()]
    _reaper_threads[:] = alive
    for index in range(len(alive), _REAPER_WORKERS):
        thread = threading.Thread(
            target=_reaper_loop,
            name=f"northcua-e2b-reaper-{index}",
            daemon=True,
        )
        _reaper_threads.append(thread)
        thread.start()


def _ensure_reaper_scheduler_locked() -> None:
    global _reaper_scheduler_thread
    if _reaper_scheduler_thread is not None and _reaper_scheduler_thread.is_alive():
        return
    _reaper_scheduler_thread = threading.Thread(
        target=_reaper_scheduler_loop,
        name="northcua-e2b-reaper-scheduler",
        daemon=True,
    )
    _reaper_scheduler_thread.start()


def _reaper_scheduler_loop() -> None:
    """Move due delayed jobs to workers without occupying a worker timer."""

    while True:
        with _reaper_condition:
            while True:
                if not _reaper_scheduled:
                    if not _reaper_accepting:
                        return
                    _reaper_condition.wait()
                    continue
                wait_seconds = _reaper_scheduled[0].due_at - time.perf_counter()
                if wait_seconds > 0:
                    _reaper_condition.wait(timeout=wait_seconds)
                    continue
                scheduled = heapq.heappop(_reaper_scheduled)
                break
        while True:
            try:
                _reaper_queue.put(scheduled.item, timeout=0.1)
                break
            except queue.Full:
                continue


def _complete_reaper_sequence_locked(sequence: int, *, campaign_key: str | None) -> None:
    global _reaper_completed_through
    if campaign_key is not None:
        remaining = _reaper_campaign_remaining.get(campaign_key, 0) - 1
        if remaining <= 0:
            _reaper_campaign_remaining.pop(campaign_key, None)
        else:
            _reaper_campaign_remaining[campaign_key] = remaining
    _reaper_completed_sequences.add(sequence)
    while _reaper_completed_through + 1 in _reaper_completed_sequences:
        _reaper_completed_through += 1
        _reaper_completed_sequences.remove(_reaper_completed_through)


def _reaper_loop() -> None:
    global _reaper_completed_through
    while True:
        item = _reaper_queue.get()
        if isinstance(item, _ReaperStop):
            _reaper_queue.task_done()
            return
        failed = False
        queue_seconds = max(time.perf_counter() - item.enqueued_at, 0.0)
        try:
            item.job()
        except BaseException:
            failed = True
            logger.exception(
                "E2B late-result reaper job failed",
                extra={"operation": item.operation},
            )
        finally:
            with _telemetry_lock:
                _reaper_telemetry.completed_total += 1
                if failed:
                    _reaper_telemetry.failed_total += 1
                _reaper_telemetry.max_queue_seconds = max(
                    _reaper_telemetry.max_queue_seconds,
                    queue_seconds,
                )
            with _reaper_condition:
                _complete_reaper_sequence_locked(
                    item.sequence,
                    campaign_key=item.campaign_key,
                )
                _reaper_condition.notify_all()
            _reaper_queue.task_done()


def reaper_telemetry() -> dict[str, object]:
    """Return bounded, secret-free late cleanup queue telemetry."""

    with _telemetry_lock:
        state = _reaper_telemetry
        return {
            "reaper_queue_size": _reaper_queue.qsize(),
            "reaper_workers": _REAPER_WORKERS,
            "reaper_workers_alive": sum(thread.is_alive() for thread in _reaper_threads),
            "reaper_submitted_total": state.submitted_total,
            "reaper_completed_total": state.completed_total,
            "reaper_failed_total": state.failed_total,
            "reaper_rejected_total": state.rejected_total,
            "reaper_queue_full_total": state.queue_full_total,
            "reaper_fallback_running": _reaper_fallback_running,
            "reaper_fallback_workers": _REAPER_FALLBACK_WORKERS,
            "reaper_fallback_started_total": state.fallback_started_total,
            "reaper_fallback_completed_total": state.fallback_completed_total,
            "reaper_fallback_failed_total": state.fallback_failed_total,
            "reaper_campaigns_active": len(_reaper_campaign_remaining),
            "reaper_campaign_deduplicated_total": state.campaign_deduplicated_total,
            "reaper_scheduled_pending": len(_reaper_scheduled),
            "reaper_scheduled_total": state.scheduled_total,
            "reaper_max_queue_seconds": round(state.max_queue_seconds, 3),
        }


def drain_e2b_reaper(*, timeout_seconds: float, stop: bool = False) -> bool:
    """Wait for accepted cleanup jobs and optionally stop all reaper workers."""

    global _reaper_accepting, _reaper_threads
    timeout_seconds = max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout_seconds
    with _reaper_condition:
        if stop:
            _reaper_accepting = False
            _reaper_condition.notify_all()
        target_sequence = _reaper_submitted_sequence
        while _reaper_completed_through < target_sequence or _reaper_fallback_running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "E2B late-result reaper drain deadline elapsed",
                    extra={"timeout_seconds": timeout_seconds, "stop": stop, **reaper_telemetry()},
                )
                return False
            _reaper_condition.wait(timeout=remaining)
        threads = [thread for thread in _reaper_threads if thread.is_alive()]
        if stop:
            # Keep the condition through worker shutdown. A concurrent fallback
            # submission then linearizes strictly after this stop fence returns.
            for _thread in threads:
                try:
                    _reaper_queue.put(
                        _ReaperStop(),
                        timeout=max(deadline - time.monotonic(), 0.0),
                    )
                except queue.Full:
                    return False
            for thread in threads:
                thread.join(timeout=max(deadline - time.monotonic(), 0.0))
            if any(thread.is_alive() for thread in threads):
                return False
            _reaper_threads = []
    return True


def _record_submitted(lane: E2BExecutorLane) -> None:
    with _telemetry_lock:
        state = _lane_telemetry[lane]
        state.queued += 1
        state.submitted_total += 1


def _record_submission_rejected(lane: E2BExecutorLane) -> None:
    with _telemetry_lock:
        state = _lane_telemetry[lane]
        state.queued = max(0, state.queued - 1)
        state.submission_rejected_total += 1


def _record_started(lane: E2BExecutorLane, *, queued: bool) -> None:
    with _telemetry_lock:
        state = _lane_telemetry[lane]
        if queued:
            state.queued = max(0, state.queued - 1)
        state.running += 1
        state.sdk_started_total += 1


def _record_finished(lane: E2BExecutorLane, *, timing: E2BExecutorTiming, failed: bool) -> None:
    with _telemetry_lock:
        state = _lane_telemetry[lane]
        state.running = max(0, state.running - 1)
        state.sdk_finished_total += 1
        if failed:
            state.sdk_failed_total += 1
        state.max_queue_seconds = max(state.max_queue_seconds, timing.executor_queue_seconds)
        state.max_sdk_seconds = max(state.max_sdk_seconds, timing.sdk_call_seconds)


def _record_cancelled(
    lane: E2BExecutorLane,
    *,
    queued: bool,
    timing: E2BExecutorTiming,
) -> None:
    with _telemetry_lock:
        state = _lane_telemetry[lane]
        if queued:
            state.queued = max(0, state.queued - 1)
            state.queued_cancelled_total += 1
            state.max_queue_seconds = max(state.max_queue_seconds, timing.executor_queue_seconds)
        else:
            state.worker_claimed_waiter_cancelled_total += 1


def _reset_telemetry() -> None:
    with _telemetry_lock:
        for lane in _lane_telemetry:
            _lane_telemetry[lane] = _LaneTelemetry()


def _consume_async_future_exception(future: asyncio.Future[Any]) -> None:
    """Mark late SDK errors retrieved even when the original waiter was cancelled."""

    if future.cancelled():
        return
    with contextlib.suppress(Exception):
        future.exception()


def executor_workers(lane: E2BExecutorLane) -> int:
    """Return the configured worker cap for one E2B executor lane."""

    name = _EXECUTOR_ENV[lane]
    default = _EXECUTOR_DEFAULTS[lane]
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        workers = int(raw)
    except ValueError:
        logger.warning("invalid E2B executor worker value", extra={"env_var": name, "default": default})
        return default
    if workers < 1:
        logger.warning("E2B executor workers must be positive", extra={"env_var": name, "default": default})
        return default
    maximum = _EXECUTOR_MAX_WORKERS[lane]
    if workers > maximum:
        logger.warning(
            "E2B executor workers exceed hard cap",
            extra={"env_var": name, "requested": workers, "maximum": maximum},
        )
        return maximum
    return workers


def _executor(lane: E2BExecutorLane) -> _BoundedThreadPoolExecutor:
    global _executor_pid
    current_pid = os.getpid()
    with _executor_lock:
        if current_pid != _executor_pid:
            # Executors inherited across fork have no usable worker threads.
            _executors.clear()
            _executor_pid = current_pid
        executor = _executors.get(lane)
        if executor is None:
            executor = _BoundedThreadPoolExecutor(
                max_workers=executor_workers(lane),
                max_pending=_EXECUTOR_MAX_PENDING[lane],
                thread_name_prefix=f"northcua-e2b-{lane}",
            )
            _executors[lane] = executor
        return executor


def shutdown_e2b_executors(*, wait: bool = True) -> None:
    """Fence new SDK submissions and release process-shared executor workers."""

    with _executor_lock:
        executors = list(_executors.values())
        _executors.clear()
    for executor in executors:
        executor.shutdown(wait=wait, cancel_futures=True)


def _shutdown_executors_for_testing() -> None:
    """Release process-shared executors between worker-count tests."""

    shutdown_e2b_executors()
    drain_e2b_reaper(timeout_seconds=2.0, stop=True)
    _reset_reaper_after_testing()
    _reset_telemetry()


def _reset_reaper_after_testing() -> None:
    global _reaper_accepting, _reaper_completed_sequences, _reaper_completed_through
    global _reaper_campaign_remaining, _reaper_fallback_running, _reaper_scheduled
    global _reaper_queue, _reaper_scheduler_thread, _reaper_submitted_sequence
    global _reaper_telemetry, _reaper_threads
    scheduler = _reaper_scheduler_thread
    if scheduler is not None:
        scheduler.join(timeout=2.0)
    with _reaper_condition:
        _reaper_queue = queue.Queue(maxsize=_REAPER_QUEUE_MAX_SIZE)
        _reaper_threads = []
        _reaper_accepting = True
        _reaper_submitted_sequence = 0
        _reaper_completed_through = 0
        _reaper_completed_sequences = set()
        _reaper_fallback_running = 0
        _reaper_scheduled = []
        _reaper_scheduler_thread = None
        _reaper_campaign_remaining = {}
    with _telemetry_lock:
        _reaper_telemetry = _ReaperTelemetry()


def _drain_reaper_at_exit() -> None:
    drain_e2b_reaper(timeout_seconds=_REAPER_EXIT_TIMEOUT_SECONDS, stop=True)


atexit.register(_drain_reaper_at_exit)


__all__ = [
    "E2BExecutorCall",
    "E2BExecutorTiming",
    "E2BExecutorLane",
    "cancel_e2b_reaper_campaign",
    "drain_e2b_reaper",
    "executor_telemetry",
    "executor_workers",
    "reaper_telemetry",
    "shutdown_e2b_executors",
    "submit_e2b_reaper_campaign",
    "submit_e2b_reaper_fallback_job",
    "submit_e2b_reaper_job",
]
