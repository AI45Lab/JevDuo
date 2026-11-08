"""Controller-backed telemetry sink.

This module is intentionally tiny: the storage mixin owns SQL persistence,
while this sink adapts the generic telemetry sink protocol to a controller
SQLite database path for managed experiment and batch runs.
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.telemetry.contracts import CaseTelemetrySnapshot, PhaseTiming, TelemetryEvent

logger = logging.getLogger(__name__)

DEFAULT_ASYNC_TELEMETRY_BUSY_TIMEOUT_MS = 30_000
DEFAULT_SYNC_TELEMETRY_BUSY_TIMEOUT_MS = 1_000
DEFAULT_TELEMETRY_BATCH_INTERVAL_SECONDS = 0.1
DEFAULT_TELEMETRY_BATCH_SIZE = 256
DEFAULT_TELEMETRY_QUEUE_MAX_SIZE = 50_000
DEFAULT_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS = 30.0

_DROP_LOG_EVERY = 1_000
_TELEMETRY_BUSY_TIMEOUT_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_BUSY_TIMEOUT_MS"
_TELEMETRY_ASYNC_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_ASYNC"
_TELEMETRY_BATCH_INTERVAL_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_BATCH_INTERVAL_SECONDS"
_TELEMETRY_BATCH_SIZE_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_BATCH_SIZE"
_TELEMETRY_QUEUE_MAX_SIZE_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_QUEUE_MAX_SIZE"
_TELEMETRY_SHUTDOWN_TIMEOUT_ENV = "NORTHCUA_CONTROLLER_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS"

_TelemetryOperation = Literal["append_event", "upsert_snapshot", "record_phase"]
_TelemetryPayload = TelemetryEvent | CaseTelemetrySnapshot | PhaseTiming


@dataclass(frozen=True)
class _TelemetryWrite:
    operation: _TelemetryOperation
    payload: _TelemetryPayload


@dataclass(frozen=True)
class _TelemetryStop:
    done: threading.Event


_TelemetryQueueItem = _TelemetryWrite | _TelemetryStop


class ControllerTelemetrySink:
    """Persist telemetry projections into a controller SQLite DB.

    Telemetry is intentionally best-effort. By default writes are queued and
    committed by one background writer thread so SQLite/GPFS lock contention
    cannot stall the controller event loop or worker heartbeats.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_ms: int | None = None,
        async_writes: bool | None = None,
        batch_interval_seconds: float | None = None,
        batch_size: int | None = None,
        queue_max_size: int | None = None,
        shutdown_timeout_seconds: float | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._async_writes = (
            _resolve_bool_env(_TELEMETRY_ASYNC_ENV, default=True) if async_writes is None else async_writes
        )
        self._busy_timeout_ms = _resolve_busy_timeout_ms(busy_timeout_ms, async_writes=self._async_writes)
        self._batch_interval_seconds = _resolve_float_env(
            _TELEMETRY_BATCH_INTERVAL_ENV,
            DEFAULT_TELEMETRY_BATCH_INTERVAL_SECONDS,
            batch_interval_seconds,
            minimum=0.001,
        )
        self._batch_size = _resolve_int_env(
            _TELEMETRY_BATCH_SIZE_ENV,
            DEFAULT_TELEMETRY_BATCH_SIZE,
            batch_size,
            minimum=1,
        )
        self._shutdown_timeout_seconds = _resolve_float_env(
            _TELEMETRY_SHUTDOWN_TIMEOUT_ENV,
            DEFAULT_TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS,
            shutdown_timeout_seconds,
            minimum=0.0,
        )
        self._closed = False
        self._accepted_writes = 0
        self._dropped_writes = 0
        self._failed_batches = 0
        self._flushed_batches = 0
        self._flushed_writes = 0
        self._writer_failed = False
        self._lock = threading.Lock()
        self._store: SQLiteControllerStore | None = None
        self._queue: queue.Queue[_TelemetryQueueItem] | None = None
        self._writer_thread: threading.Thread | None = None
        self._stop_done: threading.Event | None = None
        self._stop_enqueued = False

        if self._async_writes:
            max_size = _resolve_int_env(
                _TELEMETRY_QUEUE_MAX_SIZE_ENV,
                DEFAULT_TELEMETRY_QUEUE_MAX_SIZE,
                queue_max_size,
                minimum=1,
            )
            self._queue = queue.Queue(maxsize=max_size)
            self._writer_thread = threading.Thread(
                target=self._writer_main,
                name="northcua-controller-telemetry-writer",
                daemon=True,
            )
            self._writer_thread.start()
        else:
            self._store = self._open_store()

    def append_event(self, event: TelemetryEvent) -> None:
        """Append one telemetry event.

        Args:
            event: Typed event to persist.
        """

        self._submit(_TelemetryWrite("append_event", event))

    def upsert_snapshot(self, snapshot: CaseTelemetrySnapshot) -> None:
        """Persist a case snapshot.

        Args:
            snapshot: Latest case projection.
        """

        self._submit(_TelemetryWrite("upsert_snapshot", snapshot))

    def record_phase(self, timing: PhaseTiming) -> None:
        """Persist one phase timing.

        Args:
            timing: Measured case phase timing.
        """

        self._submit(_TelemetryWrite("record_phase", timing))

    def flush(self) -> None:
        """Flush and close the controller store connection."""

        if not self._async_writes:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                store = self._store
                self._store = None
            if store is not None:
                store.close()
            return

        with self._lock:
            self._closed = True
            telemetry_queue = self._queue
            writer_thread = self._writer_thread
            if self._stop_done is None:
                self._stop_done = threading.Event()
            done = self._stop_done
            stop_enqueued = self._stop_enqueued

        if telemetry_queue is None or writer_thread is None:
            return

        deadline = time.monotonic() + self._shutdown_timeout_seconds
        if not stop_enqueued:
            with self._lock:
                if not self._stop_enqueued:
                    self._stop_enqueued = True
                    should_enqueue_stop = True
                else:
                    should_enqueue_stop = False
            if should_enqueue_stop:
                while True:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        telemetry_queue.put(_TelemetryStop(done), timeout=remaining)
                        break
                    except queue.Full:
                        if remaining <= 0.0:
                            with self._lock:
                                self._stop_enqueued = False
                            logger.warning("timed out while enqueuing controller telemetry shutdown")
                            return

        remaining = max(0.0, deadline - time.monotonic())
        if not done.wait(timeout=remaining):
            logger.warning("timed out while flushing controller telemetry writer")
            return

        remaining = max(0.0, deadline - time.monotonic())
        writer_thread.join(timeout=remaining)

    def stats(self) -> dict[str, int | bool]:
        """Return lightweight sink counters for tests and diagnostics.

        Returns:
            Snapshot of enqueue/drop/write counters and queue state.
        """

        telemetry_queue = self._queue
        with self._lock:
            return {
                "async_writes": self._async_writes,
                "closed": self._closed,
                "accepted_writes": self._accepted_writes,
                "dropped_writes": self._dropped_writes,
                "failed_batches": self._failed_batches,
                "flushed_batches": self._flushed_batches,
                "flushed_writes": self._flushed_writes,
                "writer_failed": self._writer_failed,
                "queued_writes": 0 if telemetry_queue is None else telemetry_queue.qsize(),
            }

    def _submit(self, item: _TelemetryWrite) -> None:
        if not self._async_writes:
            self._write_sync(item)
            return

        telemetry_queue = self._queue
        if telemetry_queue is None:
            return

        with self._lock:
            if self._closed:
                return
            try:
                telemetry_queue.put_nowait(item)
                self._accepted_writes += 1
            except queue.Full:
                self._dropped_writes += 1
                if self._dropped_writes == 1 or self._dropped_writes % _DROP_LOG_EVERY == 0:
                    logger.warning(
                        "dropping controller telemetry because async queue is full",
                        extra={"dropped_writes": self._dropped_writes, "operation": item.operation},
                    )

    def _write_sync(self, item: _TelemetryWrite) -> None:
        with self._lock:
            if self._closed or self._store is None:
                return
            store = self._store
            self._accepted_writes += 1
        try:
            _apply_write(store, item)
        except sqlite3.ProgrammingError as exc:
            if "closed database" in str(exc).lower():
                with self._lock:
                    self._closed = True
                logger.debug("dropping controller telemetry after store shutdown", extra={"operation": item.operation})
                return
            raise

    def _writer_main(self) -> None:
        store: SQLiteControllerStore | None = None
        batch: list[_TelemetryWrite] = []
        telemetry_queue = self._queue
        if telemetry_queue is None:
            return
        try:
            store = self._open_store()
            while True:
                try:
                    item = telemetry_queue.get(timeout=self._batch_interval_seconds)
                except queue.Empty:
                    self._flush_batch(store, batch)
                    batch.clear()
                    continue

                try:
                    if isinstance(item, _TelemetryStop):
                        self._flush_batch(store, batch)
                        batch.clear()
                        item.done.set()
                        return
                    batch.append(item)
                    self._drain_ready_items(telemetry_queue, batch)
                    if len(batch) >= self._batch_size:
                        self._flush_batch(store, batch)
                        batch.clear()
                finally:
                    telemetry_queue.task_done()
        except Exception:
            with self._lock:
                self._closed = True
                self._writer_failed = True
            logger.warning("controller telemetry writer crashed", exc_info=True)
        finally:
            if store is not None:
                store.close()

    def _drain_ready_items(
        self, telemetry_queue: queue.Queue[_TelemetryQueueItem], batch: list[_TelemetryWrite]
    ) -> None:
        while len(batch) < self._batch_size:
            try:
                item = telemetry_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _TelemetryStop):
                    telemetry_queue.put_nowait(item)
                    return
                batch.append(item)
            finally:
                telemetry_queue.task_done()

    def _flush_batch(self, store: SQLiteControllerStore, batch: list[_TelemetryWrite]) -> None:
        if not batch:
            return
        try:
            with store.connection:
                for item in batch:
                    _apply_write(store, item, defer_commit=True)
        except sqlite3.OperationalError:
            self._record_failed_batch(len(batch))
            logger.warning("dropping controller telemetry batch after SQLite write failure", exc_info=True)
        except Exception:
            self._record_failed_batch(len(batch))
            logger.warning("dropping controller telemetry batch after unexpected write failure", exc_info=True)
        else:
            with self._lock:
                self._flushed_batches += 1
                self._flushed_writes += len(batch)

    def _open_store(self) -> SQLiteControllerStore:
        store = SQLiteControllerStore(self._db_path)
        store.connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return store

    def _record_failed_batch(self, write_count: int) -> None:
        with self._lock:
            self._failed_batches += 1
            self._dropped_writes += write_count


def _apply_write(store: SQLiteControllerStore, item: _TelemetryWrite, *, defer_commit: bool = False) -> None:
    if item.operation == "append_event":
        event = item.payload
        if not isinstance(event, TelemetryEvent):
            raise TypeError("append_event payload must be a TelemetryEvent")
        if defer_commit:
            store._insert_telemetry_event(event)  # noqa: SLF001
        else:
            store.append_telemetry_event(event)
        return
    if item.operation == "upsert_snapshot":
        snapshot = item.payload
        if not isinstance(snapshot, CaseTelemetrySnapshot):
            raise TypeError("upsert_snapshot payload must be a CaseTelemetrySnapshot")
        if defer_commit:
            store._upsert_case_snapshot(snapshot)  # noqa: SLF001
        else:
            store.upsert_case_snapshot(snapshot)
        return
    if item.operation == "record_phase":
        timing = item.payload
        if not isinstance(timing, PhaseTiming):
            raise TypeError("record_phase payload must be a PhaseTiming")
        if defer_commit:
            store._insert_case_phase_timing(timing)  # noqa: SLF001
        else:
            store.record_case_phase_timing(timing)
        return
    raise ValueError(f"unknown telemetry operation: {item.operation}")


def _resolve_busy_timeout_ms(value: int | None, *, async_writes: bool) -> int:
    if value is not None:
        return max(0, value)
    raw = os.getenv(_TELEMETRY_BUSY_TIMEOUT_ENV, "").strip()
    if not raw:
        if async_writes:
            return DEFAULT_ASYNC_TELEMETRY_BUSY_TIMEOUT_MS
        return DEFAULT_SYNC_TELEMETRY_BUSY_TIMEOUT_MS
    return max(0, int(raw))


def _resolve_bool_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _resolve_int_env(name: str, default: int, value: int | None, *, minimum: int) -> int:
    resolved = value if value is not None else int(os.getenv(name, "") or default)
    return max(minimum, int(resolved))


def _resolve_float_env(name: str, default: float, value: float | None, *, minimum: float) -> float:
    resolved = value if value is not None else float(os.getenv(name, "") or default)
    return max(minimum, float(resolved))
