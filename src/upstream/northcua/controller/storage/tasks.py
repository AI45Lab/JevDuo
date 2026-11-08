"""Task CRUD helpers for controller persistence."""

from __future__ import annotations

import sqlite3

from northcua.controller.storage.identifiers import utc_now_iso
from northcua_protocol.payloads import TaskRequest
from northcua_protocol.state import SessionState, TaskState

_TERMINAL_TASK_STATE_VALUES = (
    TaskState.FINISHED.value,
    TaskState.FAILED.value,
    TaskState.INTERRUPTED.value,
)


class TaskRepositoryMixin:
    """Persist and query task rows."""

    connection: sqlite3.Connection

    def create_task(
        self,
        request: TaskRequest,
        *,
        session_id: str,
        submitter_id: str,
        status: TaskState = TaskState.ACCEPTED,
    ) -> None:
        """Persist a new accepted task and pending session.

        Args:
            request: Accepted task request payload.
            session_id: Controller session id created for the task.
            submitter_id: Submitter identity for ownership/audit.
            status: Initial task state.
        """

        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO tasks(
                task_id,
                trace_id,
                agent_kind,
                submitter_id,
                request_json,
                status,
                result_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                request.task_id,
                request.trace_id,
                request.agent_kind,
                submitter_id,
                request.model_dump_json(),
                status.value,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO sessions(session_id, task_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, request.task_id, SessionState.PENDING.value, now, now),
        )
        self.connection.commit()

    def task_rows(self) -> list[sqlite3.Row]:
        """Return all task rows.

        Returns:
            Task rows ordered by creation time descending.
        """

        return list(self.connection.execute("SELECT * FROM tasks ORDER BY created_at DESC"))

    def task_rows_latest(
        self,
        *,
        limit: int,
        before_created_at: str = "",
        before_task_id: str = "",
    ) -> list[sqlite3.Row]:
        """Return a bounded page of task rows in status-list order.

        Args:
            limit: Maximum rows to return.
            before_created_at: Optional keyset cursor from the last row of a
                previous page.
            before_task_id: Task id component for the keyset cursor.

        Returns:
            Rows ordered by newest task first, then task id for deterministic
            pagination.
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
        """Return matching task rows in status-list order.

        Args:
            task_ids: Controller task ids to match.

        Returns:
            Matching rows ordered newest-first to preserve the historical
            ``TASK_LIST`` semantics when an explicit id filter is present.
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

    def task_row(self, task_id: str) -> sqlite3.Row | None:
        """Return one task row by id.

        Args:
            task_id: Task id to look up.

        Returns:
            Matching task row, or `None`.
        """

        return self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def schedulable_task_rows(self) -> list[sqlite3.Row]:
        """Return accepted tasks whose sessions are still waiting for a lease.

        Returns:
            Accepted task/session rows ordered FIFO for retry scheduling.
        """

        return list(
            self.connection.execute(
                """
                SELECT tasks.*, sessions.session_id
                FROM tasks
                JOIN sessions ON sessions.task_id = tasks.task_id
                WHERE tasks.status=? AND sessions.status=?
                ORDER BY tasks.created_at ASC
                """,
                (TaskState.ACCEPTED.value, SessionState.PENDING.value),
            )
        )

    def update_task_status(
        self,
        task_id: str,
        status: TaskState,
        *,
        result_json: str | None = None,
        finished: bool = False,
    ) -> None:
        """Update task lifecycle state and optional terminal result.

        Args:
            task_id: Task row to update.
            status: New task lifecycle state.
            result_json: Optional serialized terminal result.
            finished: Whether to stamp `finished_at`.
        """

        if finished:
            self.connection.execute(
                "UPDATE tasks SET status=?, result_json=?, finished_at=? WHERE task_id=?",
                (status.value, result_json, utc_now_iso(), task_id),
            )
        else:
            self.connection.execute("UPDATE tasks SET status=? WHERE task_id=?", (status.value, task_id))
        self.connection.commit()

    def finish_task_if_nonterminal(self, task_id: str, status: TaskState, *, result_json: str) -> bool:
        """Persist one terminal result unless the task is already terminal.

        Args:
            task_id: Task row to complete.
            status: Terminal status to persist.
            result_json: Serialized terminal result payload.

        Returns:
            ``True`` when this call performed the terminal transition.
            Concurrent replays return ``False`` and leave the first result
            untouched.
        """

        placeholders = ",".join("?" for _ in _TERMINAL_TASK_STATE_VALUES)
        cursor = self.connection.execute(
            f"""
            UPDATE tasks
            SET status=?, result_json=?, finished_at=?
            WHERE task_id=?
              AND status NOT IN ({placeholders})
            """,
            (status.value, result_json, utc_now_iso(), task_id, *_TERMINAL_TASK_STATE_VALUES),
        )
        self.connection.commit()
        return cursor.rowcount > 0
