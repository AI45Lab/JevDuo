"""Read controller task status snapshots directly from a local controller DB."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError

from northcua.batch.evaluator_outcome import evaluator_report_from_result
from northcua.controller.execution.task_status import TaskStatusReader
from northcua_protocol.payloads import TaskFinished, TaskStatusFilter, TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)


class LocalControllerDBStatusReader:
    """Read-only facade for controller task status snapshots.

    This is intended for managed local-controller batch resumes. It avoids
    opening additional submitter websockets just to reconcile task state while
    the controller is already under ingress pressure.
    """

    def __init__(self, path: Path | str | None, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = Path(path) if path is not None else None
        self.busy_timeout_ms = max(int(busy_timeout_ms), 0)

    def statuses(self, task_ids: list[str]) -> list[TaskStatusSnapshot] | None:
        """Return snapshots for explicit task ids, or ``None`` if DB is unavailable.

        Args:
            task_ids: Controller task ids to read from the local DB.

        Returns:
            Matching snapshots in request order, an empty list for empty input,
            or ``None`` when the DB cannot be used safely.
        """

        if not task_ids:
            return []
        reader = self._reader()
        if reader is None:
            return None
        try:
            return reader.statuses_for_ids(task_ids)
        except Exception:
            logger.warning(
                "local controller DB status lookup failed; falling back to controller websocket",
                extra={"controller_db_path": str(self.path or ""), "task_count": len(task_ids)},
                exc_info=True,
            )
            return None
        finally:
            reader.close()

    def maintenance_statuses(self, task_ids: list[str]) -> list[TaskStatusSnapshot] | None:
        """Return lightweight snapshots for pre-resume maintenance repair.

        The normal controller status projection includes full task request and
        result payloads. Long-running batches can accumulate large result JSON,
        but pre-resume repair only needs lifecycle state, task metadata,
        terminal error/category evidence, and evaluator verdict summaries.
        This projection avoids carrying bulky prompt/result/artifact bodies
        into maintenance snapshots while preserving the fields used by the
        maintenance planner and state projector.
        """

        if not task_ids:
            return []
        reader = self._reader()
        if reader is None:
            return None
        try:
            return reader.maintenance_statuses_for_ids(task_ids)
        except Exception:
            logger.warning(
                "local controller DB lightweight status lookup failed; falling back to full status lookup",
                extra={"controller_db_path": str(self.path or ""), "task_count": len(task_ids)},
                exc_info=True,
            )
            return None
        finally:
            reader.close()

    def list_batch(self, *, batch_id: str, limit: int = 0) -> list[TaskStatusSnapshot] | None:
        """Return snapshots tagged with a batch id, or ``None`` if DB is unavailable.

        Args:
            batch_id: Batch id stored in controller task metadata.
            limit: Maximum rows to return; zero keeps the protocol default.

        Returns:
            Batch task snapshots, an empty list for an empty batch id, or
            ``None`` when the DB cannot be used safely.
        """

        if not batch_id:
            return []
        reader = self._reader()
        if reader is None:
            return None
        try:
            return reader.list_tasks(TaskStatusFilter(batch_id=batch_id, limit=limit))
        except Exception:
            logger.warning(
                "local controller DB batch status lookup failed; falling back to controller websocket",
                extra={"controller_db_path": str(self.path or ""), "batch_id": batch_id},
                exc_info=True,
            )
            return None
        finally:
            reader.close()

    def available(self) -> bool:
        """Return whether the controller DB can be opened and has the expected tables.

        Returns:
            ``True`` when read-only status queries can use the local DB.
        """

        reader = self._reader()
        if reader is None:
            return False
        try:
            reader.validate_schema()
            return True
        except Exception:
            logger.warning(
                "local controller DB status reader is unavailable",
                extra={"controller_db_path": str(self.path or "")},
                exc_info=True,
            )
            return False
        finally:
            reader.close()

    def _reader(self) -> "_ReadOnlyTaskStatusReader | None":
        if self.path is None or not self.path.exists():
            return None
        return _ReadOnlyTaskStatusReader(self.path, busy_timeout_ms=self.busy_timeout_ms)


class _ReadOnlyTaskStatusReader(TaskStatusReader):
    def __init__(self, path: Path, *, busy_timeout_ms: int = 30_000) -> None:
        self._store = _ReadOnlyControllerStore(path, busy_timeout_ms=busy_timeout_ms)
        super().__init__(self._store)

    def statuses_for_ids(self, task_ids: list[str]) -> list[TaskStatusSnapshot]:
        """Return snapshots for explicit task ids using batched DB lookups."""

        if not task_ids:
            return []
        rows_by_id: dict[str, sqlite3.Row] = {}
        for chunk in _chunks(task_ids, 500):
            for row in self._store.task_rows_by_ids(chunk):
                rows_by_id.setdefault(str(row["task_id"]), row)
        return [self._snapshot(rows_by_id[task_id]) for task_id in task_ids if task_id in rows_by_id]

    def maintenance_statuses_for_ids(self, task_ids: list[str]) -> list[TaskStatusSnapshot]:
        """Return lightweight snapshots for explicit pre-resume status lookups."""

        if not task_ids:
            return []
        rows_by_id: dict[str, sqlite3.Row] = {}
        for chunk in _chunks(task_ids, 500):
            for row in self._store.task_projection_rows_by_ids(chunk):
                rows_by_id.setdefault(str(row["task_id"]), row)
        return [_maintenance_snapshot(rows_by_id[task_id]) for task_id in task_ids if task_id in rows_by_id]

    def close(self) -> None:
        """Close the underlying read-only SQLite connection."""

        self._store.close()

    def validate_schema(self) -> None:
        """Verify that required controller status tables are readable."""

        self._store.connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
        self._store.connection.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()


class _ReadOnlyControllerStore:
    """Minimal store adapter consumed by ``TaskStatusReader``."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 30_000) -> None:
        uri_path = quote(str(path), safe="/:")
        timeout_seconds = max(int(busy_timeout_ms), 0) / 1000.0
        self.connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=timeout_seconds)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={max(int(busy_timeout_ms), 0)}")
        self.connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        """Close the read-only controller DB connection."""

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
        """Return matching task rows for explicit status-id lookup.

        Args:
            task_ids: Controller task ids to match.

        Returns:
            Read-only controller task rows. Callers should impose any required
            order; explicit-id status lookup restores request order itself.
        """

        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE task_id IN ({placeholders})
                """,
                tuple(task_ids),
            )
        )

    def task_projection_rows_by_ids(self, task_ids: list[str]) -> list[sqlite3.Row]:
        """Return compactly projected status rows for pre-resume repair."""

        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        return list(
            self.connection.execute(
                f"""
                SELECT
                    tasks.task_id,
                    tasks.trace_id,
                    tasks.agent_kind,
                    tasks.status,
                    tasks.created_at,
                    tasks.finished_at,
                    tasks.request_json,
                    tasks.result_json AS result_json_projection,
                    sessions.session_id AS session_id,
                    sessions.device_id AS session_device_id,
                    sessions.lease_id AS session_lease_id
                FROM tasks
                LEFT JOIN sessions ON sessions.task_id = tasks.task_id
                WHERE tasks.task_id IN ({placeholders})
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


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _maintenance_snapshot(row: sqlite3.Row) -> TaskStatusSnapshot:
    task_id = str(row["task_id"])
    request_payload = _json_object(row["request_json"], field="request_json", required=True)
    request_metadata = request_payload.get("metadata") if isinstance(request_payload.get("metadata"), dict) else {}
    result_payload = _json_object(row["result_json_projection"], field="result_json", required=False)
    finished = _task_finished(result_payload)
    raw_result = finished.result if finished is not None else result_payload
    terminal_decision = _dict_value(finished.terminal_decision) if finished is not None else {}
    result = _evaluation_result(raw_result)
    metadata: dict[str, object] = {}
    if isinstance(request_metadata, dict):
        if request_metadata.get("batch_id"):
            metadata["batch_id"] = str(request_metadata["batch_id"])
        if request_metadata.get("batch_task_id"):
            metadata["batch_task_id"] = str(request_metadata["batch_task_id"])
        if request_metadata.get("batch_attempt") not in (None, ""):
            metadata["batch_attempt"] = _json_int(request_metadata.get("batch_attempt"))
    return TaskStatusSnapshot(
        task_id=task_id,
        trace_id=str(row["trace_id"] or ""),
        session_id=str((finished.session_id if finished is not None else "") or row["session_id"] or ""),
        agent_kind=str(row["agent_kind"] or ""),
        state=TaskState(str(row["status"])),
        device_id=str((finished.device_id if finished is not None else "") or row["session_device_id"] or ""),
        lease_id=str((finished.lease_id if finished is not None else "") or row["session_lease_id"] or ""),
        result=result,
        artifacts=[],
        error=str((finished.error if finished is not None else result_payload.get("error")) or ""),
        terminal_decision=terminal_decision,
        request="",
        metadata=metadata,
        created_at=str(row["created_at"] or ""),
        finished_at=str(row["finished_at"] or ""),
    )


class _LightweightProjectionError(ValueError):
    """Raised when a compact status row cannot preserve required semantics."""


def _json_object(raw: object, *, field: str, required: bool) -> dict[str, object]:
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if not raw:
        if required:
            raise _LightweightProjectionError(f"{field} is empty")
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise _LightweightProjectionError(f"{field} is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise _LightweightProjectionError(f"{field} is not a JSON object")
    return {str(key): value for key, value in parsed.items()}


def _dict_value(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _task_finished(raw: dict[str, object]) -> TaskFinished | None:
    if not raw:
        return None
    try:
        return TaskFinished.model_validate(raw)
    except ValidationError:
        return None


def _json_int(raw: object) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _evaluation_result(raw: object) -> dict[str, object] | None:
    report = evaluator_report_from_result(raw)
    if not isinstance(report, dict):
        return None
    compact = {str(key): value for key, value in report.items() if key in {"overall_verdict", "controllability"}}
    return {"evaluation_report": compact} if compact else None
