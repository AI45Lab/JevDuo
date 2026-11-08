"""Async helpers for controller SQLite operations.

Controller websocket handlers must not block the event loop on large SQLite
queries or write-lock waits.  These helpers run bounded controller persistence
work on a dedicated writer thread using a thread-local SQLite connection.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, TypeVar

from northcua.controller.storage.persistence import SQLiteControllerStore

_T = TypeVar("_T")
logger = logging.getLogger(__name__)
_thread_local = threading.local()
_read_thread_local = threading.local()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class ControllerStorePriority(IntEnum):
    """Writer scheduling priority for controller lifecycle persistence."""

    CRITICAL = 0
    COMMAND = 5
    NORMAL = 10
    BACKGROUND = 20


class _PrioritizedWriterExecutor:
    """Single SQLite writer with bounded-starvation priority scheduling."""

    _SCHEDULE = (
        *(ControllerStorePriority.CRITICAL for _ in range(4)),
        *(ControllerStorePriority.COMMAND for _ in range(8)),
        *(ControllerStorePriority.NORMAL for _ in range(4)),
        ControllerStorePriority.BACKGROUND,
    )

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queues: dict[
            ControllerStorePriority,
            deque[tuple[Future[Any], Callable[[], Any]]],
        ] = {priority: deque() for priority in ControllerStorePriority}
        self._schedule_index = 0
        self._critical_urgent = False
        self._command_urgent = False
        self._urgent_available = True
        self._active = False
        self._shutdown = False
        self._thread = threading.Thread(
            target=self._run,
            name="northcua-controller-db",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        operation: Callable[[], _T],
        *,
        priority: ControllerStorePriority,
    ) -> Future[_T]:
        future: Future[_T] = Future()
        with self._condition:
            if self._shutdown:
                raise RuntimeError("controller store writer is shut down")
            if priority == ControllerStorePriority.CRITICAL and self._urgent_available and not self._queues[priority]:
                self._critical_urgent = True
            elif priority == ControllerStorePriority.COMMAND and self._urgent_available and not self._queues[priority]:
                self._command_urgent = True
            self._queues[priority].append((future, operation))
            self._condition.notify()
        return future

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until the active operation and every admitted write finish."""

        with self._condition:
            return self._condition.wait_for(
                lambda: not self._active and not any(self._queues.values()),
                timeout=timeout,
            )

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Reject new work and drain or cancel already admitted writes."""

        with self._condition:
            if cancel_futures:
                for queue in self._queues.values():
                    while queue:
                        future, _operation = queue.popleft()
                        future.cancel()
            self._shutdown = True
            self._condition.notify_all()
        if wait and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._shutdown or any(self._queues.values()))
                if self._shutdown and not any(self._queues.values()):
                    return
                future, operation = self._take_next()
                self._active = True
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = operation()
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                with self._condition:
                    self._active = False
                    if not any(self._queues.values()):
                        self._critical_urgent = False
                        self._command_urgent = False
                        self._urgent_available = True
                    self._condition.notify_all()

    def _take_next(self) -> tuple[Future[Any], Callable[[], Any]]:
        critical = self._queues[ControllerStorePriority.CRITICAL]
        if self._critical_urgent and critical:
            self._critical_urgent = False
            self._urgent_available = False
            return critical.popleft()
        command = self._queues[ControllerStorePriority.COMMAND]
        if self._command_urgent and command:
            self._command_urgent = False
            self._urgent_available = False
            return command.popleft()
        for _ in self._SCHEDULE:
            priority = self._SCHEDULE[self._schedule_index]
            self._schedule_index = (self._schedule_index + 1) % len(self._SCHEDULE)
            if self._schedule_index == 0:
                self._urgent_available = True
            if self._queues[priority]:
                return self._queues[priority].popleft()
        raise RuntimeError("writer schedule selected no work from non-empty queues")


_EXECUTOR = _PrioritizedWriterExecutor()
atexit.register(_EXECUTOR.shutdown)


_READ_WORKERS = _positive_int_env("NORTHCUA_CONTROLLER_DB_READ_WORKERS", 4)
_READ_EXECUTOR = ThreadPoolExecutor(
    max_workers=_READ_WORKERS,
    thread_name_prefix="northcua-controller-db-read",
)


class _WriterAdmissionToken:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        limits: tuple[asyncio.Semaphore, ...],
    ) -> None:
        self._loop = loop
        self._limits = limits
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._release_on_loop)

    def _release_on_loop(self) -> None:
        for limit in reversed(self._limits):
            limit.release()


@dataclass(slots=True)
class _LoopAdmissionState:
    total: asyncio.Semaphore
    non_command: asyncio.Semaphore
    non_critical: asyncio.Semaphore
    command: asyncio.Semaphore
    normal: asyncio.Semaphore
    background: asyncio.Semaphore


class _WriterAdmission:
    """Bound queued writes while reserving capacity for task lifecycle work."""

    def __init__(
        self,
        *,
        maximum: int,
        command_maximum: int | None = None,
        command_reserved: int = 0,
        critical_reserved: int = 0,
        normal_maximum: int,
        background_maximum: int,
    ) -> None:
        self.maximum = maximum
        self.command_maximum = min(maximum, command_maximum if command_maximum is not None else maximum)
        self.command_reserved = min(maximum - 1, command_reserved)
        self.critical_reserved = min(maximum - self.command_reserved - 1, critical_reserved)
        self.ordinary_maximum = maximum - self.command_reserved - self.critical_reserved
        self.background_maximum = min(maximum, background_maximum)
        background_reserved = min(self.background_maximum, self.ordinary_maximum - 1)
        self.normal_maximum = min(normal_maximum, self.ordinary_maximum - background_reserved)
        self._states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopAdmissionState] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = threading.Lock()

    async def acquire(self, priority: ControllerStorePriority) -> _WriterAdmissionToken:
        loop = asyncio.get_running_loop()
        with self._lock:
            state = self._states.get(loop)
            if state is None:
                state = _LoopAdmissionState(
                    total=asyncio.Semaphore(self.maximum),
                    non_command=asyncio.Semaphore(self.maximum - self.command_reserved),
                    non_critical=asyncio.Semaphore(self.maximum - self.critical_reserved),
                    command=asyncio.Semaphore(self.command_maximum),
                    normal=asyncio.Semaphore(self.normal_maximum),
                    background=asyncio.Semaphore(self.background_maximum),
                )
                self._states[loop] = state
        limits = {
            ControllerStorePriority.CRITICAL: (state.non_command, state.total),
            ControllerStorePriority.COMMAND: (state.non_critical, state.command, state.total),
            ControllerStorePriority.NORMAL: (
                state.normal,
                state.non_command,
                state.non_critical,
                state.total,
            ),
            ControllerStorePriority.BACKGROUND: (
                state.background,
                state.non_command,
                state.non_critical,
                state.total,
            ),
        }[priority]
        acquired: list[asyncio.Semaphore] = []
        try:
            for limit in limits:
                await limit.acquire()
                acquired.append(limit)
        except BaseException:
            for limit in reversed(acquired):
                limit.release()
            raise
        return _WriterAdmissionToken(loop=loop, limits=limits)


_WRITER_QUEUE_MAX = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_QUEUE_MAX", 128)
_WRITER_COMMAND_MAX = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_COMMAND_MAX", 64)
_WRITER_COMMAND_RESERVED = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_COMMAND_RESERVED", 32)
_WRITER_CRITICAL_RESERVED = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_CRITICAL_RESERVED", 16)
_WRITER_NORMAL_MAX = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_NORMAL_MAX", 96)
_WRITER_BACKGROUND_MAX = _positive_int_env("NORTHCUA_CONTROLLER_DB_WRITER_BACKGROUND_MAX", 16)
_WRITER_ADMISSION = _WriterAdmission(
    maximum=_WRITER_QUEUE_MAX,
    command_maximum=_WRITER_COMMAND_MAX,
    command_reserved=_WRITER_COMMAND_RESERVED,
    critical_reserved=_WRITER_CRITICAL_RESERVED,
    normal_maximum=_WRITER_NORMAL_MAX,
    background_maximum=_WRITER_BACKGROUND_MAX,
)


@dataclass(frozen=True, slots=True)
class ControllerStoreCallMetrics:
    """Executor queue, worker operation, and SQLite call timings."""

    operation: str
    mode: str
    queue_ms: float
    admission_ms: float
    executor_queue_ms: float
    operation_ms: float
    exec_ms: float
    total_ms: float
    queue_depth: int
    admitted_queue_depth: int
    sql_count: int


class _ExecutorLoad:
    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.outstanding = 0
        self.lock = threading.Lock()

    def submit(self) -> _LoadTicket:
        with self.lock:
            self.outstanding += 1
            queue_depth = max(0, self.outstanding - self.workers)
        return _LoadTicket(load=self, queue_depth=queue_depth)

    def complete(self) -> None:
        with self.lock:
            self.outstanding = max(0, self.outstanding - 1)


class _LoadTicket:
    def __init__(self, *, load: _ExecutorLoad, queue_depth: int) -> None:
        self.load = load
        self.queue_depth = queue_depth
        self.started = False
        self.completed = False
        self.lock = threading.Lock()

    def mark_started(self) -> None:
        with self.lock:
            self.started = True

    def cancel_before_start(self) -> None:
        with self.lock:
            if self.started or self.completed:
                return
            self.completed = True
        self.load.complete()

    def complete(self) -> None:
        with self.lock:
            if self.completed:
                return
            self.completed = True
        self.load.complete()


_WRITER_LOAD = _ExecutorLoad(1)
_WRITER_EXECUTOR_LOAD = _ExecutorLoad(1)
_READER_LOAD = _ExecutorLoad(_READ_WORKERS)


class _WriterLifecycle:
    """Reject late writes and track every offered write per controller DB."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._closed: set[str] = set()
        self._outstanding: dict[str, int] = {}

    @staticmethod
    def _key(db_path: str | Path) -> str:
        return str(Path(db_path).resolve())

    def open(self, db_path: str | Path) -> None:
        with self._condition:
            self._closed.discard(self._key(db_path))

    def offer(self, db_path: str | Path) -> str:
        key = self._key(db_path)
        with self._condition:
            if key in self._closed:
                raise RuntimeError("controller store writer is draining")
            self._outstanding[key] = self._outstanding.get(key, 0) + 1
        return key

    def complete(self, key: str) -> None:
        with self._condition:
            remaining = self._outstanding.get(key, 0) - 1
            if remaining > 0:
                self._outstanding[key] = remaining
            else:
                self._outstanding.pop(key, None)
            self._condition.notify_all()

    def close(self, db_path: str | Path) -> str:
        key = self._key(db_path)
        with self._condition:
            self._closed.add(key)
        return key

    def wait_idle(self, key: str, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._outstanding.get(key, 0) == 0,
                timeout=timeout,
            )


_WRITER_LIFECYCLE = _WriterLifecycle()


def _thread_store(db_path: str | Path) -> SQLiteControllerStore:
    path = str(Path(db_path))
    stores = getattr(_thread_local, "stores", None)
    if stores is None:
        stores = {}
        _thread_local.stores = stores
    store = stores.get(path)
    if store is None:
        store = SQLiteControllerStore(path)
        stores[path] = store
    return store


def _thread_read_store(db_path: str | Path) -> SQLiteControllerStore:
    path = str(Path(db_path))
    stores = getattr(_read_thread_local, "stores", None)
    if stores is None:
        stores = {}
        _read_thread_local.stores = stores
    store = stores.get(path)
    if store is None:
        store = SQLiteControllerStore(path, read_only=True)
        stores[path] = store
    return store


def _execute_profiled(
    *,
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
    operation_name: str,
    mode: str,
    submitted_at: float,
    ticket: _LoadTicket,
    admitted_at: float | None = None,
    admitted_ticket: _LoadTicket | None = None,
) -> tuple[_T, ControllerStoreCallMetrics]:
    ticket.mark_started()
    if admitted_ticket is not None:
        admitted_ticket.mark_started()
    effective_admitted_at = admitted_at if admitted_at is not None else submitted_at
    started_at = time.perf_counter()
    store: SQLiteControllerStore | None = None
    sql_exec_ms = 0.0
    sql_count = 0
    try:
        store = _thread_store(db_path) if mode == "write" else _thread_read_store(db_path)
        store.begin_operation_profile()
        try:
            result = operation(store)
        finally:
            sql_exec_ms, sql_count = store.end_operation_profile()
        finished_at = time.perf_counter()
        return result, ControllerStoreCallMetrics(
            operation=operation_name,
            mode=mode,
            queue_ms=round((started_at - submitted_at) * 1000.0, 3),
            admission_ms=round((effective_admitted_at - submitted_at) * 1000.0, 3),
            executor_queue_ms=round((started_at - effective_admitted_at) * 1000.0, 3),
            operation_ms=round((finished_at - started_at) * 1000.0, 3),
            exec_ms=round(sql_exec_ms, 3),
            total_ms=round((finished_at - submitted_at) * 1000.0, 3),
            queue_depth=ticket.queue_depth,
            admitted_queue_depth=(admitted_ticket.queue_depth if admitted_ticket is not None else ticket.queue_depth),
            sql_count=sql_count,
        )
    except BaseException:
        failed_at = time.perf_counter()
        logger.warning(
            "controller DB operation failed",
            extra={
                "store_operation": operation_name,
                "store_mode": mode,
                "store_queue_ms": round((started_at - submitted_at) * 1000.0, 3),
                "store_admission_ms": round((effective_admitted_at - submitted_at) * 1000.0, 3),
                "store_executor_queue_ms": round((started_at - effective_admitted_at) * 1000.0, 3),
                "store_operation_ms": round((failed_at - started_at) * 1000.0, 3),
                "store_exec_ms": round(sql_exec_ms, 3),
                "store_sql_execute_ms": round(sql_exec_ms, 3),
                "store_exec_scope": "execute_and_transaction_excluding_fetch",
                "store_total_ms": round((failed_at - submitted_at) * 1000.0, 3),
                "store_queue_depth": ticket.queue_depth,
                "store_admitted_queue_depth": (
                    admitted_ticket.queue_depth if admitted_ticket is not None else ticket.queue_depth
                ),
                "store_sql_count": sql_count,
            },
            exc_info=True,
        )
        raise
    finally:
        ticket.complete()
        if admitted_ticket is not None:
            admitted_ticket.complete()


async def controller_store_call_profiled(
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
    *,
    operation_name: str,
    priority: ControllerStorePriority = ControllerStorePriority.NORMAL,
) -> tuple[_T, ControllerStoreCallMetrics]:
    """Run one serialized write-capable operation and return queue metrics."""

    lifecycle_key = _WRITER_LIFECYCLE.offer(db_path)
    submitted_at = time.perf_counter()
    ticket = _WRITER_LOAD.submit()
    try:
        admission = await _WRITER_ADMISSION.acquire(priority)
    except BaseException:
        ticket.complete()
        _WRITER_LIFECYCLE.complete(lifecycle_key)
        raise
    admitted_at = time.perf_counter()
    admitted_ticket = _WRITER_EXECUTOR_LOAD.submit()
    loop = asyncio.get_running_loop()
    try:
        concurrent_future = _EXECUTOR.submit(
            lambda: _execute_profiled(
                db_path=db_path,
                operation=operation,
                operation_name=operation_name,
                mode="write",
                submitted_at=submitted_at,
                ticket=ticket,
                admitted_at=admitted_at,
                admitted_ticket=admitted_ticket,
            ),
            priority=priority,
        )
    except BaseException:
        ticket.complete()
        admitted_ticket.complete()
        admission.release()
        _WRITER_LIFECYCLE.complete(lifecycle_key)
        raise
    concurrent_future.add_done_callback(lambda _completed: admission.release())
    concurrent_future.add_done_callback(lambda _completed: _WRITER_LIFECYCLE.complete(lifecycle_key))
    concurrent_future.add_done_callback(
        lambda completed: (
            (ticket.cancel_before_start(), admitted_ticket.cancel_before_start()) if completed.cancelled() else None
        )
    )
    return await asyncio.wrap_future(concurrent_future, loop=loop)


def controller_store_submit_profiled(
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
    *,
    operation_name: str,
    priority: ControllerStorePriority = ControllerStorePriority.BACKGROUND,
) -> Future[tuple[_T, ControllerStoreCallMetrics]]:
    """Submit one non-event-loop writer operation to the shared SQLite lane.

    This is intentionally reserved for bounded background producers that
    cannot await the async admission gate. Callers must keep at most one
    returned future in flight.
    """

    lifecycle_key = _WRITER_LIFECYCLE.offer(db_path)
    submitted_at = time.perf_counter()
    ticket = _WRITER_LOAD.submit()
    admitted_ticket = _WRITER_EXECUTOR_LOAD.submit()
    try:
        concurrent_future = _EXECUTOR.submit(
            lambda: _execute_profiled(
                db_path=db_path,
                operation=operation,
                operation_name=operation_name,
                mode="write",
                submitted_at=submitted_at,
                ticket=ticket,
                admitted_at=submitted_at,
                admitted_ticket=admitted_ticket,
            ),
            priority=priority,
        )
    except BaseException:
        ticket.complete()
        admitted_ticket.complete()
        _WRITER_LIFECYCLE.complete(lifecycle_key)
        raise
    concurrent_future.add_done_callback(lambda _completed: _WRITER_LIFECYCLE.complete(lifecycle_key))
    concurrent_future.add_done_callback(
        lambda completed: (
            (ticket.cancel_before_start(), admitted_ticket.cancel_before_start()) if completed.cancelled() else None
        )
    )
    return concurrent_future


async def controller_store_read_profiled(
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
    *,
    operation_name: str,
) -> tuple[_T, ControllerStoreCallMetrics]:
    """Run one query-only operation on the bounded read pool with metrics."""

    submitted_at = time.perf_counter()
    ticket = _READER_LOAD.submit()
    loop = asyncio.get_running_loop()
    try:
        concurrent_future = _READ_EXECUTOR.submit(
            lambda: _execute_profiled(
                db_path=db_path,
                operation=operation,
                operation_name=operation_name,
                mode="read",
                submitted_at=submitted_at,
                ticket=ticket,
            ),
        )
    except BaseException:
        ticket.complete()
        raise
    concurrent_future.add_done_callback(
        lambda completed: ticket.cancel_before_start() if completed.cancelled() else None
    )
    return await asyncio.wrap_future(concurrent_future, loop=loop)


async def controller_store_call(
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
    *,
    operation_name: str = "controller_store_call",
    priority: ControllerStorePriority = ControllerStorePriority.NORMAL,
) -> _T:
    """Run one controller SQLite operation off the event loop.

    Args:
        db_path: Controller SQLite path.
        operation: Function that receives a thread-local store and returns a
            result. It must not call event-loop APIs or touch websocket state.

    Returns:
        The operation result.
    """

    result, _metrics = await controller_store_call_profiled(
        db_path,
        operation,
        operation_name=operation_name,
        priority=priority,
    )
    return result


def controller_store_open(db_path: str | Path) -> None:
    """Open a controller DB writer lifecycle before accepting websocket work."""

    _WRITER_LIFECYCLE.open(db_path)


async def controller_store_drain(db_path: str | Path) -> None:
    """Reject new writes and wait for offered, admitted, and running work."""

    lifecycle_key = _WRITER_LIFECYCLE.close(db_path)
    await asyncio.to_thread(_WRITER_LIFECYCLE.wait_idle, lifecycle_key)


async def controller_store_read(
    db_path: str | Path,
    operation: Callable[[SQLiteControllerStore], _T],
) -> _T:
    """Run one query-only operation off the serialized writer queue."""

    result, _metrics = await controller_store_read_profiled(
        db_path,
        operation,
        operation_name="controller_store_read",
    )
    return result


__all__ = [
    "ControllerStoreCallMetrics",
    "ControllerStorePriority",
    "controller_store_call",
    "controller_store_call_profiled",
    "controller_store_drain",
    "controller_store_open",
    "controller_store_read",
    "controller_store_read_profiled",
    "controller_store_submit_profiled",
]
