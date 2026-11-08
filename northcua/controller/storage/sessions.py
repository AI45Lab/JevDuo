"""Session and lease CRUD helpers for controller persistence."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from northcua.controller.storage.identifiers import utc_now_iso
from northcua.controller.storage.tasks import TaskRepositoryMixin
from northcua_protocol.state import SessionState, TaskState

_ACTIVE_SESSION_STATE_VALUES = (
    SessionState.LEASED.value,
    SessionState.RUNNING.value,
)

_TERMINAL_TASK_STATE_VALUES = (
    TaskState.FINISHED.value,
    TaskState.FAILED.value,
    TaskState.INTERRUPTED.value,
)


class SessionRepositoryMixin(TaskRepositoryMixin):
    """Persist and query task, session, and lease rows."""

    connection: sqlite3.Connection

    def create_lease(
        self,
        *,
        lease_id: str,
        session_id: str,
        task_id: str,
        device_id: str,
        timing_observer: Callable[[str, float], None] | None = None,
    ) -> int:
        """Persist a new sticky session lease.

        Args:
            lease_id: Newly generated lease id.
            session_id: Session being leased.
            task_id: Task associated with the session.
            device_id: Device selected by the scheduler.
            timing_observer: Optional low-overhead operation timing sink.

        Returns:
            Monotonic lease generation for stale result protection.
        """

        now = utc_now_iso()
        query_started_at = time.perf_counter()
        previous = self.connection.execute(
            "SELECT MAX(generation) AS generation FROM leases WHERE session_id=?",
            (session_id,),
        ).fetchone()
        _observe_elapsed(timing_observer, "storage.lease_generation_query", query_started_at)
        generation = int(previous["generation"] or 0) + 1
        insert_started_at = time.perf_counter()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO leases(lease_id, session_id, device_id, status, generation)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lease_id, session_id, device_id, SessionState.LEASED.value, generation),
        )
        _observe_elapsed(timing_observer, "storage.lease_insert", insert_started_at)
        session_started_at = time.perf_counter()
        self.connection.execute(
            """
            UPDATE sessions SET device_id=?, lease_id=?, status=?, updated_at=? WHERE session_id=?
            """,
            (device_id, lease_id, SessionState.LEASED.value, now, session_id),
        )
        _observe_elapsed(timing_observer, "storage.lease_update_session", session_started_at)
        task_started_at = time.perf_counter()
        self.connection.execute(
            "UPDATE tasks SET status=? WHERE task_id=?",
            (TaskState.SCHEDULED.value, task_id),
        )
        _observe_elapsed(timing_observer, "storage.lease_update_task", task_started_at)
        commit_started_at = time.perf_counter()
        self.connection.commit()
        _observe_elapsed(timing_observer, "storage.lease_commit", commit_started_at)
        return generation

    def create_lease_if_schedulable(
        self,
        *,
        lease_id: str,
        session_id: str,
        task_id: str,
        device_id: str,
        timing_observer: Callable[[str, float], None] | None = None,
    ) -> int:
        """Persist a lease only while task/session are still schedulable.

        Args:
            lease_id: Newly generated lease id.
            session_id: Session being leased.
            task_id: Task associated with the session.
            device_id: Device selected by the scheduler.
            timing_observer: Optional low-overhead operation timing sink.

        Returns:
            Monotonic lease generation for stale result protection.

        Raises:
            RuntimeError: If another lifecycle transition won the race.
        """

        now = utc_now_iso()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT tasks.task_id
                FROM tasks
                JOIN sessions ON sessions.task_id=tasks.task_id
                WHERE tasks.task_id=? AND sessions.session_id=?
                  AND tasks.status=? AND sessions.status=?
                """,
                (task_id, session_id, TaskState.ACCEPTED.value, SessionState.PENDING.value),
            ).fetchone()
            if row is None:
                raise RuntimeError("task is no longer schedulable")
            query_started_at = time.perf_counter()
            previous = self.connection.execute(
                "SELECT MAX(generation) AS generation FROM leases WHERE session_id=?",
                (session_id,),
            ).fetchone()
            _observe_elapsed(timing_observer, "storage.lease_generation_query", query_started_at)
            generation = int(previous["generation"] or 0) + 1
            insert_started_at = time.perf_counter()
            self.connection.execute(
                """
                INSERT OR REPLACE INTO leases(lease_id, session_id, device_id, status, generation)
                VALUES (?, ?, ?, ?, ?)
                """,
                (lease_id, session_id, device_id, SessionState.LEASED.value, generation),
            )
            _observe_elapsed(timing_observer, "storage.lease_insert", insert_started_at)
            session_started_at = time.perf_counter()
            session_changed = self.connection.execute(
                """
                UPDATE sessions
                SET device_id=?, lease_id=?, status=?, updated_at=?
                WHERE session_id=? AND task_id=? AND status=?
                """,
                (device_id, lease_id, SessionState.LEASED.value, now, session_id, task_id, SessionState.PENDING.value),
            ).rowcount
            _observe_elapsed(timing_observer, "storage.lease_update_session", session_started_at)
            task_started_at = time.perf_counter()
            task_changed = self.connection.execute(
                """
                UPDATE tasks
                SET status=?
                WHERE task_id=? AND status=?
                """,
                (TaskState.SCHEDULED.value, task_id, TaskState.ACCEPTED.value),
            ).rowcount
            _observe_elapsed(timing_observer, "storage.lease_update_task", task_started_at)
            if session_changed <= 0 or task_changed <= 0:
                raise RuntimeError("task changed while creating lease")
        return generation

    def session_row(self, session_id: str) -> sqlite3.Row | None:
        """Return one session row.

        Args:
            session_id: Session id to look up.

        Returns:
            Matching SQLite row, or `None`.
        """

        return self.connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()

    def lease_row(self, lease_id: str) -> sqlite3.Row | None:
        """Return one lease row by id.

        Args:
            lease_id: Lease id to look up.

        Returns:
            Matching SQLite row, or `None`.
        """

        return self.connection.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()

    def session_row_for_task(self, task_id: str) -> sqlite3.Row | None:
        """Return the session row associated with a task id.

        Args:
            task_id: Task id to resolve.

        Returns:
            Matching session row, or `None`.
        """

        return self.connection.execute("SELECT * FROM sessions WHERE task_id=?", (task_id,)).fetchone()

    def session_rows(self) -> list[sqlite3.Row]:
        """Return all session rows.

        Returns:
            Session rows ordered by most recent update.
        """

        return list(self.connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC"))

    def update_session_status(self, session_id: str, status: SessionState) -> None:
        """Update session lifecycle state.

        Args:
            session_id: Session row to update.
            status: New session lifecycle state.
        """

        self.connection.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE session_id=?",
            (status.value, utc_now_iso(), session_id),
        )
        self.connection.commit()

    def mark_task_running_if_leased(self, *, task_id: str, session_id: str) -> bool:
        """Move a scheduled task and leased session to running via CAS.

        Args:
            task_id: Task expected to be scheduled.
            session_id: Session expected to be leased to a device.

        Returns:
            ``True`` when both rows transitioned to running. ``False`` means a
            terminal cleanup or rollback won the race.

        Raises:
            RuntimeError: If the task row changed but the matching leased
                session disappeared before it could be marked running.
        """

        now = utc_now_iso()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT tasks.task_id
                FROM tasks
                JOIN sessions ON sessions.task_id=tasks.task_id
                WHERE tasks.task_id=? AND sessions.session_id=?
                  AND tasks.status=? AND sessions.status=?
                """,
                (task_id, session_id, TaskState.SCHEDULED.value, SessionState.LEASED.value),
            ).fetchone()
            if row is None:
                return False
            task_changed = self.connection.execute(
                "UPDATE tasks SET status=? WHERE task_id=? AND status=?",
                (TaskState.RUNNING.value, task_id, TaskState.SCHEDULED.value),
            ).rowcount
            if task_changed <= 0:
                return False
            session_changed = self.connection.execute(
                "UPDATE sessions SET status=?, updated_at=? WHERE session_id=? AND status=?",
                (SessionState.RUNNING.value, now, session_id, SessionState.LEASED.value),
            ).rowcount
            if session_changed <= 0:
                raise RuntimeError("task running CAS updated task without session")
        return True

    def mark_task_queued_if_pending_session(self, *, task_id: str, session_id: str) -> bool:
        """Move a schedulable task back to accepted without reopening terminals.

        Args:
            task_id: Task to mark queued.
            session_id: Session that must already be pending.

        Returns:
            ``True`` when the task is still eligible to be queued.
        """

        with self.connection:
            row = self.connection.execute(
                """
                SELECT tasks.task_id
                FROM tasks
                JOIN sessions ON sessions.task_id=tasks.task_id
                WHERE tasks.task_id=? AND sessions.session_id=?
                  AND sessions.status=?
                  AND tasks.status IN (?, ?)
                """,
                (
                    task_id,
                    session_id,
                    SessionState.PENDING.value,
                    TaskState.ACCEPTED.value,
                    TaskState.SCHEDULED.value,
                ),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                """
                UPDATE tasks
                SET status=?
                WHERE task_id=? AND status IN (?, ?)
                """,
                (TaskState.ACCEPTED.value, task_id, TaskState.ACCEPTED.value, TaskState.SCHEDULED.value),
            )
        return True

    def interrupt_pending_task_session(self, *, task_id: str, session_id: str, result_json: str) -> bool:
        """Atomically interrupt an accepted task whose session is still pending.

        Args:
            task_id: Accepted task to terminalize.
            session_id: Pending session associated with the task.
            result_json: Serialized terminal task result.

        Returns:
            ``True`` only when this call changed an accepted+pending pair.
        """

        now = utc_now_iso()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT tasks.task_id
                FROM tasks
                JOIN sessions ON sessions.task_id=tasks.task_id
                WHERE tasks.task_id=? AND sessions.session_id=?
                  AND tasks.status=? AND sessions.status=?
                """,
                (task_id, session_id, TaskState.ACCEPTED.value, SessionState.PENDING.value),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                """
                UPDATE tasks
                SET status=?, result_json=?, finished_at=?
                WHERE task_id=? AND status=?
                """,
                (TaskState.INTERRUPTED.value, result_json, now, task_id, TaskState.ACCEPTED.value),
            )
            self.connection.execute(
                """
                UPDATE sessions
                SET status=?, updated_at=?
                WHERE session_id=? AND status=?
                """,
                (SessionState.INTERRUPTED.value, now, session_id, SessionState.PENDING.value),
            )
        return True

    def reset_session_to_pending(self, session_id: str) -> None:
        """Clear undelivered lease metadata so a task can be rescheduled.

        Args:
            session_id: Session whose undelivered lease should be rolled back.
        """

        self.connection.execute(
            "UPDATE sessions SET device_id=NULL, lease_id=NULL, status=?, updated_at=? WHERE session_id=?",
            (SessionState.PENDING.value, utc_now_iso(), session_id),
        )
        self.connection.commit()

    def rollback_undelivered_lease_if_active(
        self,
        *,
        lease_id: str,
        session_id: str,
        task_id: str,
    ) -> tuple[bool, bool]:
        """Undo one undelivered lease only if it still owns the session.

        Args:
            lease_id: Lease row being rolled back.
            session_id: Session row that should still point at the lease.
            task_id: Task row that should still be scheduled.

        Returns:
            ``(lease_released, task_requeued)``. ``task_requeued`` is false
            when a newer terminal lifecycle transition already won.
        """

        now = utc_now_iso()
        lease_released = False
        with self.connection:
            row = self.connection.execute(
                """
                SELECT sessions.session_id
                FROM sessions
                JOIN leases ON leases.lease_id=sessions.lease_id
                JOIN tasks ON tasks.task_id=sessions.task_id
                WHERE sessions.session_id=?
                  AND sessions.task_id=?
                  AND sessions.lease_id=?
                  AND leases.status=?
                  AND sessions.status=?
                  AND tasks.status=?
                """,
                (
                    session_id,
                    task_id,
                    lease_id,
                    SessionState.LEASED.value,
                    SessionState.LEASED.value,
                    TaskState.SCHEDULED.value,
                ),
            ).fetchone()
            lease_row = self.connection.execute(
                "SELECT status FROM leases WHERE lease_id=? AND session_id=?",
                (lease_id, session_id),
            ).fetchone()
            if lease_row is not None and str(lease_row["status"]) == SessionState.LEASED.value:
                lease_released = (
                    self.connection.execute(
                        "UPDATE leases SET status=? WHERE lease_id=? AND status=?",
                        (SessionState.RELEASED.value, lease_id, SessionState.LEASED.value),
                    ).rowcount
                    > 0
                )
            if row is None:
                if lease_released:
                    self.connection.execute(
                        """
                        UPDATE sessions
                        SET status=?, updated_at=?
                        WHERE session_id=? AND lease_id=? AND status=?
                        """,
                        (SessionState.RELEASED.value, now, session_id, lease_id, SessionState.LEASED.value),
                    )
                return (lease_released, False)
            session_changed = self.connection.execute(
                """
                UPDATE sessions
                SET device_id=NULL, lease_id=NULL, status=?, updated_at=?
                WHERE session_id=? AND lease_id=? AND status=?
                """,
                (SessionState.PENDING.value, now, session_id, lease_id, SessionState.LEASED.value),
            ).rowcount
            task_changed = self.connection.execute(
                """
                UPDATE tasks
                SET status=?
                WHERE task_id=? AND status=?
                """,
                (TaskState.ACCEPTED.value, task_id, TaskState.SCHEDULED.value),
            ).rowcount
            if session_changed <= 0 or task_changed <= 0:
                return (lease_released, False)
        return (lease_released, True)

    def update_lease_status(self, lease_id: str, status: SessionState) -> None:
        """Update lease lifecycle state.

        Args:
            lease_id: Lease row to update.
            status: New lease lifecycle state.
        """

        self.connection.execute("UPDATE leases SET status=? WHERE lease_id=?", (status.value, lease_id))
        self.connection.commit()

    def interrupt_sessions_for_device(self, device_id: str, *, reason: str) -> list[sqlite3.Row]:
        """Mark active sessions interrupted for a device.

        Args:
            device_id: Device whose active sessions should be interrupted.
            reason: Interruption reason retained by callers for task CAS.

        Returns:
            Rows for sessions/tasks that were active before interruption.
        """

        rows = list(
            self.connection.execute(
                "SELECT session_id, task_id FROM sessions WHERE device_id=? AND status IN (?, ?)",
                (device_id, SessionState.LEASED.value, SessionState.RUNNING.value),
            )
        )
        now = utc_now_iso()
        self.connection.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE device_id=? AND status IN (?, ?)",
            (SessionState.INTERRUPTED.value, now, device_id, SessionState.LEASED.value, SessionState.RUNNING.value),
        )
        self.connection.commit()
        return rows


def _observe_elapsed(
    timing_observer: Callable[[str, float], None] | None,
    operation: str,
    started_at: float,
) -> None:
    if timing_observer is None:
        return
    timing_observer(operation, max(0.0, (time.perf_counter() - started_at) * 1000.0))
