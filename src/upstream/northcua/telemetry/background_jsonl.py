"""Background JSONL writers for best-effort hot-path telemetry."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

_DROP_LOG_EVERY = 1_000
_DEFAULT_QUEUE_SIZE = 100_000
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_DEFAULT_FLUSH_INTERVAL_SECONDS = 2.0
_DEFAULT_FLUSH_ROW_COUNT = 512


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class BackgroundJsonlStats:
    """Counters for background JSONL persistence.

    Attributes:
        accepted_rows: Rows accepted into the background queue.
        written_rows: Rows successfully appended to disk.
        dropped_rows: Rows rejected because the queue was full or closed.
        failed_rows: Rows the writer thread failed to append.
        queue_size: Approximate number of queued rows at snapshot time.
        closed: Whether the writer has been closed.
    """

    accepted_rows: int = 0
    written_rows: int = 0
    dropped_rows: int = 0
    failed_rows: int = 0
    queue_size: int = 0
    closed: bool = False


@dataclass
class _WriteItem:
    path: Path
    text: str


@dataclass
class _DrainItem:
    done: threading.Event


_QueueItem = _WriteItem | _DrainItem | None


class BackgroundJsonlWriter:
    """Append JSONL rows on one daemon thread.

    The writer is best-effort by default: hot-path producers enqueue without
    blocking and drop diagnostic rows if the bounded queue is full. Callers that
    need deterministic test visibility can call ``flush`` after producing rows.
    """

    def __init__(
        self,
        *,
        queue_max_size: int | None = None,
        shutdown_timeout_seconds: float | None = None,
        flush_interval_seconds: float | None = None,
        flush_row_count: int | None = None,
        name: str = "northcua-jsonl-writer",
    ) -> None:
        max_size = (
            _env_int("NORTHCUA_JSONL_WRITER_QUEUE_MAX_SIZE", _DEFAULT_QUEUE_SIZE)
            if queue_max_size is None
            else queue_max_size
        )
        self._shutdown_timeout_seconds = (
            _env_float("NORTHCUA_JSONL_WRITER_SHUTDOWN_TIMEOUT_SECONDS", _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
            if shutdown_timeout_seconds is None
            else shutdown_timeout_seconds
        )
        self._flush_interval_seconds = (
            _env_float("NORTHCUA_JSONL_WRITER_FLUSH_INTERVAL_SECONDS", _DEFAULT_FLUSH_INTERVAL_SECONDS)
            if flush_interval_seconds is None
            else flush_interval_seconds
        )
        self._flush_row_count = (
            _env_int("NORTHCUA_JSONL_WRITER_FLUSH_ROW_COUNT", _DEFAULT_FLUSH_ROW_COUNT)
            if flush_row_count is None
            else flush_row_count
        )
        self._queue: queue.Queue[_QueueItem] = queue.Queue(maxsize=max(1, max_size))
        self._lock = threading.Lock()
        self._closed = False
        self._accepted_rows = 0
        self._written_rows = 0
        self._dropped_rows = 0
        self._failed_rows = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def append_json(self, path: Path, payload: dict[str, object]) -> bool:
        """Queue one JSON row.

        Args:
            path: Target JSONL path.
            payload: JSON-serializable row payload.

        Returns:
            ``True`` when the row was accepted for background writing.
        """

        return self.append_text(path, json.dumps(payload, ensure_ascii=False) + "\n")

    def append_text(self, path: Path, text: str) -> bool:
        """Queue one already-serialized row.

        Args:
            path: Target JSONL path.
            text: Serialized line, including its trailing newline.

        Returns:
            ``True`` when the row was accepted for background writing.
        """

        with self._lock:
            if self._closed:
                return False
        try:
            self._queue.put_nowait(_WriteItem(path=path, text=text))
        except queue.Full:
            self._record_drop(path)
            return False
        with self._lock:
            self._accepted_rows += 1
        return True

    def flush(self) -> None:
        """Wait until all rows queued before this call are persisted."""

        with self._lock:
            closed = self._closed
        if closed and not self._thread.is_alive():
            return
        done = threading.Event()
        self._queue.put(_DrainItem(done=done))
        if not done.wait(timeout=self._shutdown_timeout_seconds):
            logger.warning("background JSONL writer did not drain within timeout")

    def close(self) -> None:
        """Drain queued rows and stop the writer thread."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush()
        self._queue.put(None)
        self._thread.join(timeout=self._shutdown_timeout_seconds)
        if self._thread.is_alive():
            logger.warning("background JSONL writer did not stop within timeout")

    def stats(self) -> BackgroundJsonlStats:
        """Return a snapshot of writer counters.

        Returns:
            Immutable-style stats value with queue and write counters captured
            under the writer lock.
        """

        with self._lock:
            return BackgroundJsonlStats(
                accepted_rows=self._accepted_rows,
                written_rows=self._written_rows,
                dropped_rows=self._dropped_rows,
                failed_rows=self._failed_rows,
                queue_size=self._queue.qsize(),
                closed=self._closed,
            )

    def _record_drop(self, path: Path) -> None:
        with self._lock:
            self._dropped_rows += 1
            dropped = self._dropped_rows
        if dropped == 1 or dropped % _DROP_LOG_EVERY == 0:
            logger.warning(
                "dropping JSONL telemetry row because background queue is full",
                extra={"path": str(path), "dropped_rows": dropped},
            )

    def _run(self) -> None:
        handles: dict[Path, TextIO] = {}
        pending_since_flush = 0
        try:
            while True:
                timeout = (
                    self._flush_interval_seconds if pending_since_flush and self._flush_interval_seconds > 0 else None
                )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    self._flush_handles(handles)
                    pending_since_flush = 0
                    continue
                try:
                    if item is None:
                        return
                    if isinstance(item, _DrainItem):
                        self._flush_handles(handles)
                        pending_since_flush = 0
                        item.done.set()
                        continue
                    self._write_item(handles, item)
                    pending_since_flush += 1
                    if self._flush_row_count > 0 and pending_since_flush >= self._flush_row_count:
                        self._flush_handles(handles)
                        pending_since_flush = 0
                finally:
                    self._queue.task_done()
        finally:
            self._flush_handles(handles)
            for handle in handles.values():
                try:
                    handle.close()
                except OSError:
                    logger.debug("failed to close background JSONL handle", exc_info=True)

    def _write_item(self, handles: dict[Path, TextIO], item: _WriteItem) -> None:
        try:
            handle = handles.get(item.path)
            if handle is None:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                handle = item.path.open("a", encoding="utf-8")
                handles[item.path] = handle
            handle.write(item.text)
            with self._lock:
                self._written_rows += 1
        except Exception:
            with self._lock:
                self._failed_rows += 1
            logger.warning("background JSONL write failed", extra={"path": str(item.path)}, exc_info=True)

    @staticmethod
    def _flush_handles(handles: dict[Path, TextIO]) -> None:
        for handle in handles.values():
            try:
                handle.flush()
            except OSError:
                logger.debug("failed to flush background JSONL handle", exc_info=True)
