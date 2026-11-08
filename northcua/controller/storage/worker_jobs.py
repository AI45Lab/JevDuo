"""Worker job persistence helpers for process executor runs."""

from __future__ import annotations

import json
import logging
import sqlite3

from northcua.controller.execution.processes.contracts import (
    WorkerArtifactManifest,
    WorkerJobFailureKind,
    WorkerJobStatus,
)
from northcua.controller.storage.identifiers import utc_now_iso
from northcua.controller.storage.result_compaction import (
    compact_command_result_for_sqlite,
    compact_task_finished_for_sqlite,
)
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.payloads import CommandRequest, CommandResult, TaskFinished
from northcua_protocol.state import CommandState, SessionState, TaskState

logger = logging.getLogger(__name__)

_NON_TERMINAL_STATUSES = {
    WorkerJobStatus.QUEUED.value,
    WorkerJobStatus.STARTING.value,
    WorkerJobStatus.RUNNING.value,
    WorkerJobStatus.FINISHING.value,
}
_STATUS_PREDECESSORS = {
    WorkerJobStatus.QUEUED: {WorkerJobStatus.QUEUED.value},
    WorkerJobStatus.STARTING: {
        WorkerJobStatus.QUEUED.value,
        WorkerJobStatus.STARTING.value,
    },
    WorkerJobStatus.RUNNING: {
        WorkerJobStatus.QUEUED.value,
        WorkerJobStatus.STARTING.value,
        WorkerJobStatus.RUNNING.value,
    },
    WorkerJobStatus.FINISHING: {
        WorkerJobStatus.QUEUED.value,
        WorkerJobStatus.STARTING.value,
        WorkerJobStatus.RUNNING.value,
        WorkerJobStatus.FINISHING.value,
    },
}
for _terminal_status in WorkerJobStatus:
    if _terminal_status.value not in _NON_TERMINAL_STATUSES:
        _STATUS_PREDECESSORS[_terminal_status] = _NON_TERMINAL_STATUSES | {_terminal_status.value}
_TERMINAL_TASK_STATUSES = {
    TaskState.FINISHED.value,
    TaskState.FAILED.value,
    TaskState.INTERRUPTED.value,
}


class WorkerJobRepositoryMixin:
    """Persist process executor worker jobs."""

    connection: sqlite3.Connection

    def create_worker_job(
        self,
        *,
        job_id: str,
        task_id: str,
        attempt_id: str,
        session_id: str,
        lease_id: str,
        device_id: str,
        agent_kind: str,
    ) -> None:
        """Create a durable queued worker job row.

        Args:
            job_id: Unique worker job id.
            task_id: Controller task id.
            attempt_id: Batch/controller attempt id.
            session_id: Controller session id.
            lease_id: Controller lease id.
            device_id: Device id assigned by the controller.
            agent_kind: Agent kind executed by the worker.
        """

        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO worker_jobs(
                job_id, task_id, attempt_id, session_id, lease_id, device_id,
                agent_kind, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                task_id,
                attempt_id,
                session_id,
                lease_id,
                device_id,
                agent_kind,
                WorkerJobStatus.QUEUED.value,
                now,
                now,
            ),
        )
        self.connection.commit()

    def try_create_worker_job(
        self,
        *,
        job_id: str,
        task_id: str,
        attempt_id: str,
        session_id: str,
        lease_id: str,
        device_id: str,
        agent_kind: str,
    ) -> bool:
        """Create a worker job if this task attempt has not been claimed.

        Args:
            job_id: Unique worker job id.
            task_id: Controller task id.
            attempt_id: Batch/controller attempt id.
            session_id: Controller session id.
            lease_id: Controller lease id.
            device_id: Device id assigned by the controller.
            agent_kind: Agent kind executed by the worker.

        Returns:
            ``True`` when the row was inserted; ``False`` when the task attempt
            was already claimed.
        """

        try:
            self.create_worker_job(
                job_id=job_id,
                task_id=task_id,
                attempt_id=attempt_id,
                session_id=session_id,
                lease_id=lease_id,
                device_id=device_id,
                agent_kind=agent_kind,
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def mark_worker_job_status(
        self,
        *,
        job_id: str,
        status: WorkerJobStatus,
        worker_pid: int = 0,
        failure_kind: WorkerJobFailureKind = WorkerJobFailureKind.NONE,
        error: str = "",
        artifact_manifest: WorkerArtifactManifest | None = None,
    ) -> None:
        """Update one worker job lifecycle row.

        Args:
            job_id: Worker job row to update.
            status: New durable worker job status.
            worker_pid: Optional process id to persist when known.
            failure_kind: Failure category for terminal failed states.
            error: Human-readable terminal error.
            artifact_manifest: Optional artifact manifest to persist.
        """

        now = utc_now_iso()
        finished_at = now if status.value not in _NON_TERMINAL_STATUSES else None
        artifact_manifest_json = artifact_manifest.model_dump_json() if artifact_manifest is not None else None
        allowed_predecessors = sorted(_STATUS_PREDECESSORS[status])
        predecessor_placeholders = ", ".join("?" for _ in allowed_predecessors)
        cursor = self.connection.execute(
            f"""
            UPDATE worker_jobs
            SET status=?,
                worker_pid=CASE WHEN ? > 0 THEN ? ELSE worker_pid END,
                heartbeat_at=CASE WHEN ? IN (?, ?) THEN ? ELSE heartbeat_at END,
                finished_at=COALESCE(?, finished_at),
                failure_kind=?,
                error=?,
                artifact_manifest_json=COALESCE(?, artifact_manifest_json),
                updated_at=?
            WHERE job_id=?
              AND status IN ({predecessor_placeholders})
            """,
            (
                status.value,
                worker_pid,
                worker_pid,
                status.value,
                WorkerJobStatus.STARTING.value,
                WorkerJobStatus.RUNNING.value,
                now,
                finished_at,
                failure_kind.value,
                error,
                artifact_manifest_json,
                now,
                job_id,
                *allowed_predecessors,
            ),
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            logger.debug(
                "ignored stale worker job status transition",
                extra={"job_id": job_id, "requested_status": status.value},
            )

    def heartbeat_worker_job(
        self,
        *,
        job_id: str,
        worker_pid: int,
        heartbeat_payload: dict[str, object] | None = None,
    ) -> None:
        """Refresh worker job heartbeat and optional diagnostics.

        Args:
            job_id: Worker job row to update.
            worker_pid: Current worker process id.
            heartbeat_payload: JSON-safe resource and phase diagnostics.
        """

        now = utc_now_iso()
        payload = json.dumps(heartbeat_payload or {}, ensure_ascii=False)
        self.connection.execute(
            """
            UPDATE worker_jobs
            SET status=?,
                worker_pid=?,
                heartbeat_at=?,
                heartbeat_payload_json=?,
                updated_at=?
            WHERE job_id=?
              AND status IN (?, ?, ?)
            """,
            (
                WorkerJobStatus.RUNNING.value,
                worker_pid,
                now,
                payload,
                now,
                job_id,
                WorkerJobStatus.QUEUED.value,
                WorkerJobStatus.STARTING.value,
                WorkerJobStatus.RUNNING.value,
            ),
        )
        self.connection.commit()

    def heartbeat_worker_jobs_batch(
        self,
        updates: list[tuple[str, int, dict[str, object]]],
    ) -> int:
        """Persist latest worker heartbeats in one SQLite transaction.

        Terminal rows are deliberately excluded so a delayed heartbeat can
        never move a finishing, failed, or canceled worker back to running.

        Args:
            updates: ``(job_id, worker_pid, heartbeat_payload)`` tuples.

        Returns:
            Number of rows updated by the batch.
        """

        if not updates:
            return 0
        now = utc_now_iso()
        before = self.connection.total_changes
        self.connection.executemany(
            """
            UPDATE worker_jobs
            SET status=?,
                worker_pid=?,
                heartbeat_at=?,
                heartbeat_payload_json=?,
                updated_at=?
            WHERE job_id=?
              AND status IN (?, ?, ?)
            """,
            [
                (
                    WorkerJobStatus.RUNNING.value,
                    worker_pid,
                    now,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    job_id,
                    WorkerJobStatus.QUEUED.value,
                    WorkerJobStatus.STARTING.value,
                    WorkerJobStatus.RUNNING.value,
                )
                for job_id, worker_pid, payload in updates
            ],
        )
        self.connection.commit()
        return self.connection.total_changes - before

    def finalize_worker_job_from_task_result(self, *, job_id: str, payload: TaskFinished) -> None:
        """Mark a worker job terminal after task completion is durable.

        Args:
            job_id: Worker job associated with the completed task.
            payload: Canonical terminal task payload.
        """

        status, failure_kind, error = _worker_job_terminal_projection(payload)
        self.mark_worker_job_status(
            job_id=job_id,
            status=status,
            failure_kind=failure_kind,
            error=error,
        )

    def worker_job_row(self, job_id: str) -> sqlite3.Row | None:
        """Return one worker job row by id.

        Args:
            job_id: Worker job id to fetch.

        Returns:
            SQLite row when present, otherwise ``None``.
        """

        return self.connection.execute("SELECT * FROM worker_jobs WHERE job_id=?", (job_id,)).fetchone()

    def non_terminal_worker_job_rows(self) -> list[sqlite3.Row]:
        """Return worker jobs that did not reach a terminal lifecycle state.

        Returns:
            Non-terminal worker job rows ordered by creation time.
        """

        placeholders = ",".join("?" for _ in _NON_TERMINAL_STATUSES)
        return list(
            self.connection.execute(
                f"SELECT * FROM worker_jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                tuple(sorted(_NON_TERMINAL_STATUSES)),
            )
        )

    def fail_non_terminal_worker_jobs(
        self,
        *,
        failure_kind: WorkerJobFailureKind,
        error: str,
    ) -> int:
        """Mark leftover non-terminal worker jobs retryable-failed on startup.

        Process executor workers are not replayed after a controller restart
        because a half-completed GUI task may already have produced visible side
        effects. Batch retry owns starting a fresh attempt.

        Args:
            failure_kind: Failure category to stamp on stale jobs.
            error: Human-readable recovery error.

        Returns:
            Number of worker jobs updated.
        """

        now = utc_now_iso()
        placeholders = ",".join("?" for _ in _NON_TERMINAL_STATUSES)
        cursor = self.connection.execute(
            f"""
            UPDATE worker_jobs
            SET status=?,
                failure_kind=?,
                error=?,
                finished_at=?,
                updated_at=?
            WHERE status IN ({placeholders})
            """,
            (
                WorkerJobStatus.FAILED_RETRYABLE.value,
                failure_kind.value,
                error,
                now,
                now,
                *tuple(sorted(_NON_TERMINAL_STATUSES)),
            ),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def fail_non_terminal_worker_jobs_and_tasks(
        self,
        *,
        failure_kind: WorkerJobFailureKind,
        error: str,
    ) -> int:
        """Close stale worker jobs and their task/session/lease state.

        This is used during process-executor startup after a controller crash.
        Existing worker jobs are not replayed because the prior worker may have
        already produced GUI side effects; batch retry should submit a fresh
        attempt.

        Args:
            failure_kind: Failure category to stamp on stale jobs.
            error: Human-readable recovery error.

        Returns:
            Number of worker jobs reconciled.
        """

        rows = self.non_terminal_worker_job_rows()
        if not rows:
            return 0
        now = utc_now_iso()
        for row in rows:
            task_id = str(row["task_id"])
            session_id = str(row["session_id"])
            lease_id = str(row["lease_id"])
            device_id = str(row["device_id"])
            task_row = self.connection.execute(
                "SELECT trace_id, status, result_json FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task_row is not None and str(task_row["status"]) in _TERMINAL_TASK_STATUSES:
                terminal_status, terminal_failure_kind, terminal_error = _worker_job_status_from_terminal_task(
                    str(task_row["status"]),
                    str(task_row["result_json"] or ""),
                )
                self.connection.execute(
                    """
                    UPDATE worker_jobs
                    SET status=?,
                        failure_kind=?,
                        error=?,
                        finished_at=?,
                        updated_at=?
                    WHERE job_id=?
                    """,
                    (
                        terminal_status.value,
                        terminal_failure_kind.value,
                        terminal_error,
                        now,
                        now,
                        str(row["job_id"]),
                    ),
                )
                self._release_worker_job_resources(
                    session_id=session_id,
                    lease_id=lease_id,
                    task_status=TaskState(str(task_row["status"])),
                    error=error,
                    now=now,
                )
                continue
            trace_id = str(task_row["trace_id"] or "") if task_row is not None else ""
            terminal = TaskFinished(
                task_id=task_id,
                trace_id=trace_id,
                session_id=session_id,
                device_id=device_id,
                lease_id=lease_id,
                state=TaskState.FAILED,
                error=error,
                terminal_decision=AgentTerminalDecision.error(
                    reason=AgentTerminalReason.RUNTIME_ERROR,
                    final_answer=error,
                    summary="The controller restarted before the process worker returned a terminal payload.",
                    retryable=True,
                    metadata={
                        "worker_job_id": str(row["job_id"]),
                        "worker_failure_kind": failure_kind.value,
                        "failure_category": "controller_submit",
                    },
                ).to_json(),
            )
            self.connection.execute(
                "UPDATE tasks SET status=?, result_json=?, finished_at=? WHERE task_id=?",
                (
                    TaskState.FAILED.value,
                    compact_task_finished_for_sqlite(terminal).model_dump_json(),
                    now,
                    task_id,
                ),
            )
            self._release_worker_job_resources(
                session_id=session_id,
                lease_id=lease_id,
                task_status=TaskState.FAILED,
                error=error,
                now=now,
            )
        placeholders = ",".join("?" for _ in _NON_TERMINAL_STATUSES)
        self.connection.execute(
            f"""
            UPDATE worker_jobs
            SET status=?,
                failure_kind=?,
                error=?,
                finished_at=?,
                updated_at=?
            WHERE status IN ({placeholders})
            """,
            (
                WorkerJobStatus.FAILED_RETRYABLE.value,
                failure_kind.value,
                error,
                now,
                now,
                *tuple(sorted(_NON_TERMINAL_STATUSES)),
            ),
        )
        self.connection.commit()
        return len(rows)

    def _release_worker_job_resources(
        self,
        *,
        session_id: str,
        lease_id: str,
        task_status: TaskState,
        error: str,
        now: str,
    ) -> None:
        """Release durable session/lease/command rows after stale worker recovery."""

        session_status = _session_status_from_task_status(task_status)
        self.connection.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE session_id=? AND status NOT IN (?, ?, ?, ?)",
            (
                session_status.value,
                now,
                session_id,
                SessionState.COMPLETED.value,
                SessionState.FAILED.value,
                SessionState.INTERRUPTED.value,
                SessionState.RELEASED.value,
            ),
        )
        self.connection.execute(
            "UPDATE leases SET status=? WHERE lease_id=?",
            (SessionState.RELEASED.value, lease_id),
        )
        active_rows = self.connection.execute(
            "SELECT * FROM commands WHERE session_id=? AND status IN (?, ?, ?)",
            (
                session_id,
                CommandState.QUEUED.value,
                CommandState.SENT.value,
                CommandState.RUNNING.value,
            ),
        ).fetchall()
        for command_row in active_rows:
            request = _command_request_from_row(command_row)
            result = CommandResult(
                command_id=(
                    request.command_id
                    if request is not None
                    else str(command_row["protocol_command_id"] or command_row["command_id"])
                ),
                session_id=request.session_id if request is not None else str(command_row["session_id"]),
                lease_id=request.lease_id if request is not None else str(command_row["lease_id"]),
                device_id=request.device_id if request is not None else str(command_row["device_id"]),
                lease_generation=1 if request is None else request.lease_generation,
                status=CommandState.CANCELLED,
                error=error or "command cancelled during stale worker recovery",
            )
            compacted = compact_command_result_for_sqlite(result)
            self.connection.execute(
                "UPDATE commands SET status=?, result_json=?, updated_at=? WHERE command_id=?",
                (
                    CommandState.CANCELLED.value,
                    compacted.model_dump_json(),
                    now,
                    str(command_row["command_id"]),
                ),
            )


def _worker_job_terminal_projection(payload: TaskFinished) -> tuple[WorkerJobStatus, WorkerJobFailureKind, str]:
    if payload.state == TaskState.FINISHED:
        return WorkerJobStatus.FINISHED, WorkerJobFailureKind.NONE, ""
    if payload.state == TaskState.INTERRUPTED:
        return WorkerJobStatus.CANCELED, WorkerJobFailureKind.CANCELED, payload.error or ""
    terminal_decision = payload.terminal_decision if isinstance(payload.terminal_decision, dict) else {}
    raw_metadata = terminal_decision.get("metadata")
    metadata: dict[str, object] = raw_metadata if isinstance(raw_metadata, dict) else {}
    failure_kind = _coerce_failure_kind(str(metadata.get("worker_failure_kind") or ""))
    retryable = terminal_decision.get("retryable")
    return (
        WorkerJobStatus.FAILED_RETRYABLE if retryable is not False else WorkerJobStatus.FAILED_TERMINAL,
        failure_kind if failure_kind is not WorkerJobFailureKind.NONE else WorkerJobFailureKind.TASK_FAILED,
        payload.error or "",
    )


def _worker_job_status_from_terminal_task(
    status: str, result_json: str = ""
) -> tuple[WorkerJobStatus, WorkerJobFailureKind, str]:
    if result_json:
        try:
            return _worker_job_terminal_projection(TaskFinished.model_validate_json(result_json))
        except Exception:
            logger.debug("failed to parse terminal task result during worker job recovery", exc_info=True)
    if status == TaskState.FINISHED.value:
        return WorkerJobStatus.FINISHED, WorkerJobFailureKind.NONE, ""
    if status == TaskState.INTERRUPTED.value:
        return WorkerJobStatus.CANCELED, WorkerJobFailureKind.CANCELED, "task already interrupted"
    return WorkerJobStatus.FAILED_RETRYABLE, WorkerJobFailureKind.WORKER_CRASH, "task already terminal"


def _session_status_from_task_status(status: TaskState) -> SessionState:
    if status == TaskState.FINISHED:
        return SessionState.COMPLETED
    if status == TaskState.INTERRUPTED:
        return SessionState.INTERRUPTED
    return SessionState.FAILED


def _command_request_from_row(row: sqlite3.Row) -> CommandRequest | None:
    raw = str(row["request_json"] or "")
    if not raw:
        return None
    try:
        return CommandRequest.model_validate_json(raw)
    except Exception:
        logger.debug("failed to parse command request during worker job recovery", exc_info=True)
        return None


def _coerce_failure_kind(value: str) -> WorkerJobFailureKind:
    try:
        return WorkerJobFailureKind(value)
    except ValueError:
        return WorkerJobFailureKind.NONE
