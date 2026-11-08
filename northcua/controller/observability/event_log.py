"""Controller observability event helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from northcua.controller.storage.async_store import (
    ControllerStorePriority,
    controller_store_submit_profiled,
)
from northcua.controller.storage.events import ControllerEventRow
from northcua.controller.storage.identifiers import utc_now_iso
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_runtime_shared.redaction import redact_for_log

CommandStageEventMode = Literal["raw", "sample", "aggregate", "off"]
logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


_EVENT_WRITER_QUEUE_MAX = _positive_int_env("NORTHCUA_CONTROLLER_EVENT_WRITER_QUEUE_MAX", 1024)
_EVENT_WRITER_MAX_RETRY_ATTEMPTS = _positive_int_env(
    "NORTHCUA_CONTROLLER_EVENT_WRITER_MAX_RETRY_ATTEMPTS",
    3,
)

_COMMAND_STAGE_EVENT_TYPES = frozenset(
    {
        "command_dispatch_claimed",
        "command_device_result_wait_resolved",
        "command_device_send_done",
        "command_gui_resource_waiting",
        "command_queued",
        "command_running",
        "command_sent",
        "command_timeout_floor_applied",
        "command_started",
        "command_result_ready",
        "command_result_sent",
        "device_command_received",
        "device_command_dequeued",
        "device_command_prepare_started",
        "device_command_prepare_finished",
        "device_command_timeout_capped",
        "device_driver_call_finished",
        "device_driver_call_started",
        "device_result_json_serialize_started",
        "device_result_json_serialize_finished",
        "device_websocket_send_finished",
        "device_websocket_send_started",
    }
)


class ControllerEventLog:
    """Append-only controller event log."""

    def __init__(
        self,
        store: SQLiteControllerStore,
        *,
        command_stage_event_mode: CommandStageEventMode = "raw",
        command_stage_event_sample_rate: float = 1.0,
        event_write_batch_size: int = 1,
        event_write_flush_interval_seconds: float = 0.0,
    ) -> None:
        self.store = store
        self.command_stage_event_mode = _normalize_command_stage_event_mode(command_stage_event_mode)
        self.command_stage_event_sample_rate = min(1.0, max(0.0, float(command_stage_event_sample_rate)))
        self.event_write_batch_size = max(1, int(event_write_batch_size))
        self.event_write_flush_interval_seconds = max(0.0, float(event_write_flush_interval_seconds))
        self._pending_events: list[ControllerEventRow] = []
        self._command_stage_counts: Counter[str] = Counter()
        self._command_stage_by_tool: Counter[str] = Counter()
        self._command_stage_by_source: Counter[str] = Counter()
        self._command_stage_raw_sampled = 0
        self._flush_handle: asyncio.TimerHandle | None = None
        self._async_writer = (
            _AsyncEventWriter(store.path)
            if isinstance(store, SQLiteControllerStore)
            and (self.event_write_batch_size > 1 or self.event_write_flush_interval_seconds > 0)
            else None
        )
        self._closed = False

    def write(self, event_type: str, *, trace_id: str, entity_type: str, entity_id: str, **payload: object) -> None:
        """Write one trace-linked controller event.

        Args:
            event_type: Stable event name.
            trace_id: Trace id shared by task, command, and device events.
            entity_type: Entity category, such as task, session, or command.
            entity_id: Entity identifier within the category.
            **payload: Additional redacted event metadata.
        """

        redacted_payload = redact_for_log(payload)
        payload_dict = redacted_payload if isinstance(redacted_payload, dict) else {}
        if self._handle_command_stage_event(
            event_type,
            trace_id=trace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload_dict,
        ):
            return
        self._write_row(
            ControllerEventRow(
                trace_id=trace_id,
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                payload=payload_dict,
            )
        )

    def write_redacted(
        self,
        event_type: str,
        *,
        trace_id: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object],
    ) -> None:
        """Write an already-redacted event payload.

        Args:
            event_type: Stable event name.
            trace_id: Trace id shared by task, command, and device events.
            entity_type: Entity category.
            entity_id: Entity identifier.
            payload: Redacted payload metadata.
        """

        if self._handle_command_stage_event(
            event_type,
            trace_id=trace_id,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        ):
            return
        self._write_row(
            ControllerEventRow(
                trace_id=trace_id,
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                payload=payload,
            )
        )

    def flush(self, *, wait: bool = True) -> None:
        """Flush buffered controller events and command-stage aggregates.

        Args:
            wait: Whether to wait for the background writer to persist queued rows.
        """

        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._closed:
            return
        rows = self._rows_for_flush()
        if rows:
            try:
                if self._async_writer is None:
                    self.store.append_events(rows)
                else:
                    self._async_writer.append(rows, wait=wait)
            except Exception:
                logger.warning("controller event flush failed; buffered events retained", exc_info=True)
                return
            self._clear_flushed_rows()
        elif wait and self._async_writer is not None:
            try:
                self._async_writer.drain()
            except Exception:
                logger.warning("controller event writer drain failed", exc_info=True)

    def close(self) -> None:
        """Flush and close this event log facade."""

        self.flush(wait=True)
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._async_writer is not None:
            self._async_writer.close()
        self._closed = True

    def snapshot(self) -> dict[str, int]:
        """Return bounded background event writer counters."""

        if self._async_writer is None:
            return {
                "pending_batches": 0,
                "in_flight_batches": 0,
                "in_flight_rows": 0,
                "in_flight_age_ms": 0,
                "queued_rows": 0,
                "persisted_rows": 0,
                "dropped_rows": 0,
                "failures": 0,
                "retries": 0,
                "queue_age_ms_max": 0,
                "last_success_age_ms": -1,
                "accepting": 0,
                "active_producers": 0,
                "thread_alive": 0,
            }
        return self._async_writer.snapshot()

    def _handle_command_stage_event(
        self,
        event_type: str,
        *,
        trace_id: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object],
    ) -> bool:
        if not _is_command_stage_event(event_type, entity_type=entity_type, payload=payload):
            return False
        mode = self.command_stage_event_mode
        if mode == "raw":
            return False
        if mode == "off":
            return True
        if mode == "aggregate":
            self._record_command_stage_aggregate(event_type, payload=payload)
        if mode in {"sample", "aggregate"} and self.command_stage_event_sample_rate > 0:
            if _should_sample_command_stage_event(
                event_type,
                trace_id=trace_id,
                entity_id=entity_id,
                payload=payload,
                sample_rate=self.command_stage_event_sample_rate,
            ):
                sampled_payload = dict(payload)
                sampled_payload["controller_command_stage_sampled"] = True
                sampled_payload["controller_command_stage_sample_rate"] = self.command_stage_event_sample_rate
                self._command_stage_raw_sampled += 1
                self._write_row(
                    ControllerEventRow(
                        trace_id=trace_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        event_type=event_type,
                        payload=sampled_payload,
                    )
                )
        if mode == "aggregate" and self._command_stage_event_count() >= self.event_write_batch_size:
            self.flush(wait=not _has_running_event_loop())
        else:
            self._schedule_flush()
        return True

    def _record_command_stage_aggregate(self, event_type: str, *, payload: dict[str, object]) -> None:
        self._command_stage_counts[event_type] += 1
        tool_id = payload.get("tool_id")
        if isinstance(tool_id, str) and tool_id:
            self._command_stage_by_tool[tool_id] += 1
        source = payload.get("source")
        if isinstance(source, str) and source:
            self._command_stage_by_source[source] += 1

    def _write_row(self, row: ControllerEventRow) -> None:
        if self._closed:
            return
        if self.event_write_batch_size <= 1 and self.event_write_flush_interval_seconds <= 0:
            self.store.append_event(
                trace_id=row.trace_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                event_type=row.event_type,
                payload=row.payload,
            )
            return
        self._pending_events.append(row)
        if len(self._pending_events) >= self.event_write_batch_size:
            self.flush(wait=not _has_running_event_loop())
            return
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self.event_write_flush_interval_seconds <= 0 or self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_handle = loop.call_later(self.event_write_flush_interval_seconds, lambda: self.flush(wait=False))

    def _rows_for_flush(self) -> list[ControllerEventRow]:
        rows = list(self._pending_events)
        if self._command_stage_counts:
            rows.append(self._command_stage_aggregate_row())
        return rows

    def _clear_flushed_rows(self) -> None:
        self._pending_events = []
        if self._command_stage_counts:
            self._command_stage_counts = Counter()
            self._command_stage_by_tool = Counter()
            self._command_stage_by_source = Counter()
            self._command_stage_raw_sampled = 0

    def _command_stage_aggregate_row(self) -> ControllerEventRow:
        total = sum(self._command_stage_counts.values())
        return ControllerEventRow(
            trace_id=uuid.uuid4().hex,
            entity_type="controller",
            entity_id="command_stage_events",
            event_type="command_stage_events_aggregated",
            payload={
                "mode": self.command_stage_event_mode,
                "sample_rate": self.command_stage_event_sample_rate,
                "total": total,
                "sampled_raw_events": self._command_stage_raw_sampled,
                "counts_by_event_type": dict(sorted(self._command_stage_counts.items())),
                "counts_by_tool_id": dict(self._command_stage_by_tool.most_common(20)),
                "counts_by_source": dict(sorted(self._command_stage_by_source.items())),
            },
        )

    def _command_stage_event_count(self) -> int:
        return sum(self._command_stage_counts.values()) + len(self._pending_events)


def _normalize_command_stage_event_mode(mode: str) -> CommandStageEventMode:
    normalized = str(mode or "raw").strip().lower()
    if normalized not in {"raw", "sample", "aggregate", "off"}:
        raise ValueError("command_stage_event_mode must be raw, sample, aggregate, or off")
    return normalized  # type: ignore[return-value]


def _is_command_stage_event(event_type: str, *, entity_type: str, payload: dict[str, object]) -> bool:
    if event_type not in _COMMAND_STAGE_EVENT_TYPES:
        return False
    if entity_type == "command":
        return True
    return bool(payload.get("command_id"))


def _should_sample_command_stage_event(
    event_type: str,
    *,
    trace_id: str,
    entity_id: str,
    payload: dict[str, object],
    sample_rate: float,
) -> bool:
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    sample_key = "|".join(
        str(payload.get(key) or "")
        for key in (
            "source",
            "device_id",
            "session_id",
            "lease_id",
            "command_id",
            "tool_id",
            "stage",
            "device_timestamp",
            "device_event_timestamp",
        )
    )
    digest = hashlib.blake2s(
        f"{event_type}|{entity_id}|{trace_id}|{sample_key}".encode("utf-8"),
        digest_size=8,
    ).digest()
    bucket = int.from_bytes(digest, "big") / float(2**64 - 1)
    return bucket < sample_rate


class _AsyncEventWriter:
    """Bounded producer that feeds events through the shared SQLite writer."""

    _MAX_RETRY_SLEEP_SECONDS = 1.0

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._queue: queue.Queue[_EventWriteItem | None] = queue.Queue(maxsize=_EVENT_WRITER_QUEUE_MAX)
        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        self._accepting = True
        self._closing = False
        self._active_producers = 0
        self._fatal_error: BaseException | None = None
        self._metrics_lock = threading.Lock()
        self._queued_rows = 0
        self._persisted_rows = 0
        self._dropped_rows = 0
        self._failures = 0
        self._retries = 0
        self._queue_age_ms_max = 0
        self._in_flight_rows = 0
        self._in_flight_started_at: float | None = None
        self._last_success_at: float | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="northcua-controller-event-writer",
            daemon=True,
        )
        self._thread.start()

    def append(self, rows: list[ControllerEventRow], *, wait: bool) -> None:
        """Queue controller event rows for background SQLite persistence.

        Args:
            rows: Event rows to persist.
            wait: Whether to block until this batch is written.

        Raises:
            RuntimeError: If the background writer reports a write failure.
        """

        if not rows:
            return
        done = threading.Event() if wait else None
        item = _EventWriteItem(rows=list(rows), done=done, queued_at=time.monotonic())
        admitted = False
        try:
            with self._state_changed:
                if not self._accepting:
                    raise RuntimeError("controller event writer is closing")
                self._active_producers += 1
                admitted = True
            if wait:
                self._put_waiting(item)
            else:
                self._queue.put_nowait(item)
        except (queue.Full, RuntimeError) as exc:
            with self._metrics_lock:
                self._failures += 1
                if not wait:
                    self._dropped_rows += len(rows)
            if wait:
                if isinstance(exc, queue.Full):
                    raise TimeoutError("controller event writer queue remained full") from exc
                raise
            logger.warning(
                "controller event writer unavailable; dropping observability rows",
                extra={"row_count": len(rows), "queue_max": _EVENT_WRITER_QUEUE_MAX},
            )
            return
        finally:
            if admitted:
                with self._state_changed:
                    self._active_producers -= 1
                    if self._active_producers == 0:
                        self._state_changed.notify_all()
        with self._metrics_lock:
            self._queued_rows += len(rows)
        if done is None:
            return
        done.wait()
        if item.error is not None:
            raise RuntimeError("controller event writer failed") from item.error

    def drain(self) -> None:
        """Wait until previously queued event rows have been written.

        Raises:
            RuntimeError: If the background writer reports a write failure.
        """

        done = threading.Event()
        item = _EventWriteItem(rows=[], done=done, queued_at=time.monotonic())
        admitted = False
        try:
            with self._state_changed:
                if not self._accepting:
                    raise RuntimeError("controller event writer is closing")
                self._active_producers += 1
                admitted = True
            self._put_waiting(item)
        finally:
            if admitted:
                with self._state_changed:
                    self._active_producers -= 1
                    if self._active_producers == 0:
                        self._state_changed.notify_all()
        done.wait()
        if item.error is not None:
            raise RuntimeError("controller event writer failed") from item.error

    def close(self) -> None:
        """Stop the background writer thread after queued work drains."""

        with self._state_changed:
            if self._closing:
                should_signal = False
            else:
                self._accepting = False
                self._closing = True
                should_signal = True
                while self._active_producers > 0:
                    self._state_changed.wait()
        if should_signal:
            if self._thread.is_alive():
                self._queue.put(None)
        self._thread.join()
        if self._fatal_error is not None:
            self._release_pending_waiters(self._fatal_error)

    def _run(self) -> None:
        current_item: _EventWriteItem | None = None
        current_item_completed = False
        try:
            while True:
                current_item = self._queue.get()
                current_item_completed = False
                try:
                    if current_item is None:
                        return
                    self._mark_in_flight(current_item)
                    try:
                        self._write_item(current_item)
                        current_item_completed = True
                        if current_item.done is not None:
                            current_item.done.set()
                    finally:
                        self._clear_in_flight()
                finally:
                    self._queue.task_done()
                current_item = None
        except BaseException as exc:
            logger.warning("controller async event writer stopped unexpectedly", exc_info=True)
            with self._state_changed:
                self._accepting = False
                self._closing = True
                self._fatal_error = exc
                self._state_changed.notify_all()
            if current_item is not None:
                current_item.error = exc
                if current_item.done is not None:
                    current_item.done.set()
                elif not current_item_completed:
                    with self._metrics_lock:
                        self._dropped_rows += len(current_item.rows)
            with self._metrics_lock:
                self._failures += 1
                self._in_flight_rows = 0
                self._in_flight_started_at = None
            self._release_pending_waiters(exc)

    def _write_item(self, item: "_EventWriteItem") -> None:
        if not item.rows:
            item.error = None
            return
        while item.attempts < _EVENT_WRITER_MAX_RETRY_ATTEMPTS:
            try:
                future = controller_store_submit_profiled(
                    self.db_path,
                    lambda store: _append_event_rows(store.connection, item.rows),
                    operation_name="controller_event_batch",
                    priority=ControllerStorePriority.BACKGROUND,
                )
                future.result()
                item.error = None
                queue_age_ms = int(max(0.0, time.monotonic() - item.queued_at) * 1000)
                with self._metrics_lock:
                    self._persisted_rows += len(item.rows)
                    self._queue_age_ms_max = max(self._queue_age_ms_max, queue_age_ms)
                    self._last_success_at = time.monotonic()
                return
            except BaseException as exc:
                item.error = exc
                item.attempts += 1
                with self._metrics_lock:
                    self._failures += 1
                    if item.attempts < _EVENT_WRITER_MAX_RETRY_ATTEMPTS:
                        self._retries += 1
                if item.attempts < _EVENT_WRITER_MAX_RETRY_ATTEMPTS:
                    logger.warning(
                        "controller async event write failed; retrying",
                        extra={"attempt": item.attempts, "row_count": len(item.rows)},
                        exc_info=True,
                    )
                    time.sleep(min(self._MAX_RETRY_SLEEP_SECONDS, 0.05 * item.attempts))
        if item.done is None:
            with self._metrics_lock:
                self._dropped_rows += len(item.rows)
        logger.warning(
            "controller async event write retries exhausted; dropping observability rows",
            extra={"attempts": item.attempts, "row_count": len(item.rows)},
        )

    def snapshot(self) -> dict[str, int]:
        now = time.monotonic()
        with self._state_lock:
            accepting = self._accepting
            active_producers = self._active_producers
        with self._metrics_lock:
            in_flight_age_ms = (
                int(max(0.0, now - self._in_flight_started_at) * 1000) if self._in_flight_started_at is not None else 0
            )
            last_success_age_ms = (
                int(max(0.0, now - self._last_success_at) * 1000) if self._last_success_at is not None else -1
            )
            return {
                "pending_batches": self._queue.qsize(),
                "in_flight_batches": int(self._in_flight_rows > 0),
                "in_flight_rows": self._in_flight_rows,
                "in_flight_age_ms": in_flight_age_ms,
                "queued_rows": self._queued_rows,
                "persisted_rows": self._persisted_rows,
                "dropped_rows": self._dropped_rows,
                "failures": self._failures,
                "retries": self._retries,
                "queue_age_ms_max": self._queue_age_ms_max,
                "last_success_age_ms": last_success_age_ms,
                "accepting": int(accepting),
                "active_producers": active_producers,
                "thread_alive": int(self._thread.is_alive()),
            }

    def _put_waiting(self, item: "_EventWriteItem") -> None:
        while True:
            try:
                self._queue.put(item, timeout=0.1)
                break
            except queue.Full:
                with self._state_lock:
                    fatal_error = self._fatal_error
                if fatal_error is not None or not self._thread.is_alive():
                    raise RuntimeError("controller event writer stopped before enqueue") from fatal_error
        with self._state_lock:
            fatal_error = self._fatal_error
        if fatal_error is not None:
            self._release_pending_waiters(fatal_error)
            raise RuntimeError("controller event writer stopped after enqueue") from fatal_error

    def _mark_in_flight(self, item: "_EventWriteItem") -> None:
        if not item.rows:
            return
        with self._metrics_lock:
            self._in_flight_rows = len(item.rows)
            self._in_flight_started_at = time.monotonic()

    def _clear_in_flight(self) -> None:
        with self._metrics_lock:
            self._in_flight_rows = 0
            self._in_flight_started_at = None

    def _release_pending_waiters(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if item is not None:
                    item.error = error
                    if item.done is None:
                        with self._metrics_lock:
                            self._dropped_rows += len(item.rows)
                    if item.done is not None:
                        item.done.set()
            finally:
                self._queue.task_done()


@dataclass
class _EventWriteItem:
    rows: list[ControllerEventRow]
    done: threading.Event | None
    queued_at: float
    error: BaseException | None = None
    attempts: int = 0


def _append_event_rows(connection: sqlite3.Connection, events: list[ControllerEventRow]) -> None:
    rows = [
        (
            event.trace_id,
            event.entity_type,
            event.entity_id,
            event.event_type,
            json.dumps(event.payload, ensure_ascii=False),
            utc_now_iso(),
        )
        for event in events
    ]
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO events(trace_id, entity_type, entity_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
