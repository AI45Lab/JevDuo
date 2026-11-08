"""Read-only controller task status projections."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import ValidationError

from northcua_protocol.payloads import (
    TaskFinished,
    TaskRequest,
    TaskStatusFilter,
    TaskStatusSnapshot,
    task_request_fingerprint_sha256,
)
from northcua_protocol.state import TaskState

DEFAULT_TASK_LIST_LIMIT = 100
TASK_LIST_CHUNK_SIZE = 500


class _TaskStatusStore(Protocol):
    def task_row(self, task_id: str) -> sqlite3.Row | None:
        """Return one task row by controller task id.

        Args:
            task_id: Controller task id.

        Returns:
            Matching task row, or ``None`` when not found.
        """
        ...

    def task_rows(self) -> list[sqlite3.Row]:
        """Return task rows in status-list order.

        Returns:
            Controller task rows available to the status reader.
        """
        ...

    def task_rows_latest(
        self,
        *,
        limit: int,
        before_created_at: str = "",
        before_task_id: str = "",
    ) -> list[sqlite3.Row]:
        """Return one bounded status-list page.

        Args:
            limit: Maximum rows to return.
            before_created_at: Optional keyset cursor timestamp.
            before_task_id: Optional keyset cursor task id.

        Returns:
            Controller task rows in latest-first order.
        """
        ...

    def task_rows_by_ids(self, task_ids: list[str]) -> list[sqlite3.Row]:
        """Return matching task rows in status-list order.

        Args:
            task_ids: Controller task ids to match.

        Returns:
            Matching rows ordered newest-first.
        """
        ...

    def session_row_for_task(self, task_id: str) -> sqlite3.Row | None:
        """Return the session row bound to a controller task id.

        Args:
            task_id: Controller task id.

        Returns:
            Matching session row, or ``None`` when no session is bound.
        """
        ...


class ReadOnlyTaskStatusStore:
    """Read controller task status through an independent SQLite connection."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 30_000) -> None:
        uri_path = quote(str(path), safe="/:")
        timeout_seconds = max(int(busy_timeout_ms), 0) / 1000.0
        self.connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=timeout_seconds)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={max(int(busy_timeout_ms), 0)}")
        self.connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        """Close the read-only SQLite connection."""

        self.connection.close()

    def task_row(self, task_id: str) -> sqlite3.Row | None:
        """Return one task row by controller task id.

        Args:
            task_id: Controller task id.

        Returns:
            Matching task row, or ``None`` when not found.
        """

        return self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def task_rows(self) -> list[sqlite3.Row]:
        """Return task rows ordered from newest to oldest.

        Returns:
            All controller task rows visible in the read-only DB.
        """

        return list(self.connection.execute("SELECT * FROM tasks ORDER BY created_at DESC"))

    def task_rows_latest(
        self,
        *,
        limit: int,
        before_created_at: str = "",
        before_task_id: str = "",
    ) -> list[sqlite3.Row]:
        """Return a bounded page of task rows ordered from newest to oldest.

        Args:
            limit: Maximum rows to return.
            before_created_at: Optional keyset cursor timestamp.
            before_task_id: Optional keyset cursor task id.

        Returns:
            Read-only controller task rows for status-list projection.
        """

        bounded_limit = max(1, min(int(limit), 10_000))
        if before_created_at:
            return list(
                self.connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE created_at < ?
                       OR (created_at = ? AND task_id > ?)
                    ORDER BY created_at DESC, task_id ASC
                    LIMIT ?
                    """,
                    (before_created_at, before_created_at, before_task_id, bounded_limit),
                )
            )
        return list(
            self.connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, task_id ASC LIMIT ?",
                (bounded_limit,),
            )
        )

    def task_rows_by_ids(self, task_ids: list[str]) -> list[sqlite3.Row]:
        """Return matching task rows ordered from newest to oldest.

        Args:
            task_ids: Controller task ids to match.

        Returns:
            Read-only controller task rows in ``TASK_LIST`` projection order.
        """

        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE task_id IN ({placeholders})
                ORDER BY created_at DESC, task_id ASC
                """,
                tuple(task_ids),
            )
        )

    def session_row_for_task(self, task_id: str) -> sqlite3.Row | None:
        """Return the session row bound to a controller task id.

        Args:
            task_id: Controller task id.

        Returns:
            Matching session row, or ``None`` when no session is bound.
        """

        return self.connection.execute("SELECT * FROM sessions WHERE task_id=?", (task_id,)).fetchone()


def statuses_for_ids_from_db(
    path: Path | str,
    task_ids: list[str],
    *,
    timing_observer: Callable[[str, float], None] | None = None,
) -> list[TaskStatusSnapshot]:
    """Read explicit task statuses from an independent SQLite connection.

    Args:
        path: Controller SQLite DB path.
        task_ids: Controller task ids to read.
        timing_observer: Optional observer for per-operation timings.

    Returns:
        Matching status snapshots in request order.
    """

    store = ReadOnlyTaskStatusStore(path)
    try:
        return TaskStatusReader(store, timing_observer=timing_observer).statuses_for_ids(task_ids)
    finally:
        store.close()


def task_list_from_db(
    path: Path | str,
    task_filter: TaskStatusFilter,
    *,
    timing_observer: Callable[[str, float], None] | None = None,
) -> list[TaskStatusSnapshot]:
    """Read a task list from an independent SQLite connection.

    Args:
        path: Controller SQLite DB path.
        task_filter: Protocol filter sent by the submitter.
        timing_observer: Optional observer for per-operation timings.

    Returns:
        Matching task snapshots ordered by status-list semantics.
    """

    store = ReadOnlyTaskStatusStore(path)
    try:
        return TaskStatusReader(store, timing_observer=timing_observer).list_tasks(task_filter)
    finally:
        store.close()


class TaskStatusReader:
    """Build protocol task status snapshots from controller persistence."""

    def __init__(
        self,
        store: _TaskStatusStore,
        *,
        timing_observer: Callable[[str, float], None] | None = None,
    ) -> None:
        self.store = store
        self.timing_observer = timing_observer

    def statuses_for_ids(self, task_ids: list[str]) -> list[TaskStatusSnapshot]:
        """Return status snapshots for explicit task ids.

        Args:
            task_ids: Task ids requested by a submitter.

        Returns:
            Snapshots for known task ids, preserving request order.
        """

        total_started_at = time.perf_counter()
        unique_task_ids = list(dict.fromkeys(task_ids))
        rows = []
        for offset in range(0, len(unique_task_ids), TASK_LIST_CHUNK_SIZE):
            rows_started_at = time.perf_counter()
            rows.extend(self.store.task_rows_by_ids(unique_task_ids[offset : offset + TASK_LIST_CHUNK_SIZE]))
            self._observe_elapsed("task_status.task_rows_by_ids", rows_started_at)
        rows_by_id = {str(row["task_id"]): row for row in rows}
        snapshots = [self._snapshot(rows_by_id[task_id]) for task_id in task_ids if task_id in rows_by_id]
        self._observe_elapsed("task_status.statuses_for_ids_total", total_started_at)
        return snapshots

    def list_tasks(self, task_filter: TaskStatusFilter) -> list[TaskStatusSnapshot]:
        """Return task snapshots matching a filter.

        Args:
            task_filter: Protocol filter sent by the submitter.

        Returns:
            Matching task snapshots ordered by controller task creation time.
        """

        total_started_at = time.perf_counter()
        if task_filter.task_ids:
            rows_started_at = time.perf_counter()
            rows = self.store.task_rows_by_ids(list(task_filter.task_ids))
            self._observe_elapsed("task_status.task_rows_by_ids", rows_started_at)
            filtered = []
            for row in rows:
                request = TaskRequest.model_validate_json(row["request_json"])
                state = TaskState(row["status"])
                if task_filter.states and state not in task_filter.states:
                    continue
                if task_filter.agent_kinds and request.agent_kind not in task_filter.agent_kinds:
                    continue
                if task_filter.batch_id and request.metadata.get("batch_id") != task_filter.batch_id:
                    continue
                filtered.append(self._snapshot(row, request=request, state=state))
            self._observe_elapsed("task_status.list_tasks_total", total_started_at)
            return filtered[: task_filter.limit or DEFAULT_TASK_LIST_LIMIT]
        snapshots: list[TaskStatusSnapshot] = []
        limit = task_filter.limit or DEFAULT_TASK_LIST_LIMIT
        before_created_at = ""
        before_task_id = ""
        while len(snapshots) < limit:
            rows_started_at = time.perf_counter()
            rows = self.store.task_rows_latest(
                limit=TASK_LIST_CHUNK_SIZE,
                before_created_at=before_created_at,
                before_task_id=before_task_id,
            )
            self._observe_elapsed("task_status.task_rows_latest", rows_started_at)
            if not rows:
                break
            before_created_at = str(rows[-1]["created_at"] or "")
            before_task_id = str(rows[-1]["task_id"])
            for row in rows:
                request = TaskRequest.model_validate_json(row["request_json"])
                state = TaskState(row["status"])
                if task_filter.states and state not in task_filter.states:
                    continue
                if task_filter.agent_kinds and request.agent_kind not in task_filter.agent_kinds:
                    continue
                if task_filter.batch_id and request.metadata.get("batch_id") != task_filter.batch_id:
                    continue
                snapshots.append(self._snapshot(row, request=request, state=state))
                if len(snapshots) >= limit:
                    break
        self._observe_elapsed("task_status.list_tasks_total", total_started_at)
        return snapshots

    def _observe_elapsed(self, operation: str, started_at: float) -> None:
        if self.timing_observer is None:
            return
        self.timing_observer(operation, max(0.0, (time.perf_counter() - started_at) * 1000.0))

    @staticmethod
    def _matches_snapshot(snapshot: TaskStatusSnapshot, task_filter: TaskStatusFilter) -> bool:
        if task_filter.states and snapshot.state not in task_filter.states:
            return False
        if task_filter.agent_kinds and snapshot.agent_kind not in task_filter.agent_kinds:
            return False
        return not (task_filter.batch_id and snapshot.metadata.get("batch_id") != task_filter.batch_id)

    def _snapshot(
        self,
        row: sqlite3.Row,
        *,
        request: TaskRequest | None = None,
        state: TaskState | None = None,
    ) -> TaskStatusSnapshot:
        task_id = str(row["task_id"])
        request = request or TaskRequest.model_validate_json(row["request_json"])
        state = state or TaskState(row["status"])
        session = self.store.session_row_for_task(task_id)
        session_id = str(session["session_id"]) if session is not None else ""
        device_id = str(session["device_id"] or "") if session is not None else ""
        lease_id = str(session["lease_id"] or "") if session is not None else ""
        result: dict[str, object] | str | None = None
        artifacts = []
        error = ""
        terminal_decision: dict[str, object] = {}
        result_json = row["result_json"]
        if result_json:
            try:
                finished = TaskFinished.model_validate_json(result_json)
                result = finished.result
                artifacts = list(finished.artifacts)
                error = finished.error or ""
                terminal_decision = dict(finished.terminal_decision)
                device_id = finished.device_id or device_id
                lease_id = finished.lease_id or lease_id
                session_id = finished.session_id or session_id
            except ValidationError:
                parsed = json.loads(result_json)
                if isinstance(parsed, dict):
                    result = parsed
                    error = str(parsed.get("error") or "")
                else:
                    result = str(parsed)
        return TaskStatusSnapshot(
            task_id=task_id,
            trace_id=str(row["trace_id"]),
            session_id=session_id,
            agent_kind=str(row["agent_kind"]),
            state=state,
            device_id=device_id,
            lease_id=lease_id,
            result=result,
            artifacts=artifacts,
            error=error,
            terminal_decision=terminal_decision,
            request=request.request,
            metadata=dict(request.metadata),
            request_fingerprint_sha256=task_request_fingerprint_sha256(request),
            created_at=str(row["created_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
        )
