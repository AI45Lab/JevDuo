"""Safe SQLite write operations for batch maintenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.batch.preparation import PreparedSandboxStatus
from northcua.batch.state.sqlite_helpers import (
    _create_schema,
    _sanitized_state_payload,
    _SQLiteTransaction,
    _state_update_values,
    _valid_recovery_action_from_values,
)
from northcua.telemetry.contracts import FailureCategory, utc_now_iso
from northcua_protocol.capabilities import JsonObject

_PRE_CONTROLLER_STALE_STATUSES = {
    BatchTaskStatus.STARTING_SANDBOX,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE,
    BatchTaskStatus.WAITING_DEVICE,
}
_IN_FLIGHT_STATUSES = {
    *_PRE_CONTROLLER_STALE_STATUSES,
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
}
_TERMINAL_STATUSES = {
    BatchTaskStatus.SUCCEEDED,
    BatchTaskStatus.EVALUATION_PENDING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
    BatchTaskStatus.CANCELED,
}
_PREPARED_TERMINATION_RESOLVABLE_STATUSES = {
    PreparedSandboxStatus.ASSIGNED.value,
    PreparedSandboxStatus.CANCELED.value,
    PreparedSandboxStatus.CLEANUP_DEFERRED.value,
    PreparedSandboxStatus.CLEANUP_FAILED.value,
    PreparedSandboxStatus.EXPIRED.value,
    PreparedSandboxStatus.FAILED.value,
    PreparedSandboxStatus.PREPARED.value,
}


class SQLiteMaintenanceError(RuntimeError):
    """Raised when a requested maintenance write is unsafe."""


class SQLiteMaintenanceStore:
    """Lease-aware maintenance facade for the batch SQLite database."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"batch SQLite state not found: {path}")
        self.path = path
        self._connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        _create_schema(self._connection)

    def all_states(self) -> list[BatchTaskState]:
        """Return all task states in manifest order.

        Returns:
            Latest task states ordered by manifest position.
        """

        rows = self._connection.execute("SELECT * FROM batch_tasks ORDER BY order_index ASC").fetchall()
        return [_state_from_row(row) for row in rows]

    def pre_resume_states(self, *, case_id: str = "") -> list[BatchTaskState]:
        """Return only states that can affect pre-resume convergence.

        Fresh pending rows have no controller-side work yet and should not
        trigger deterministic controller-id lookups. Large batches can contain
        tens of thousands of those rows, so pre-resume cleanup keeps them out
        of the controller DB hot path while still checking retry-pending rows
        and all rows that may hold stale local/controller ownership.
        """

        statuses = {
            *_IN_FLIGHT_STATUSES,
            BatchTaskStatus.FAILED,
            BatchTaskStatus.TIMED_OUT,
            BatchTaskStatus.CANCELED,
        }
        status_values = [status.value for status in sorted(statuses, key=lambda item: item.value)]
        params: list[object] = [*status_values, BatchTaskStatus.PENDING.value]
        case_clause = ""
        if case_id:
            case_clause = " AND task_id=?"
            params.append(case_id)
        rows = self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks
            WHERE (
                status IN ({",".join("?" for _ in status_values)})
                OR (status=? AND (controller_task_id<>'' OR attempts>0))
            )
            {case_clause}
            ORDER BY order_index ASC
            """,
            tuple(params),
        ).fetchall()
        return [_state_from_row(row) for row in rows]

    def get_state(self, case_id: str) -> BatchTaskState | None:
        """Return one task state by case id.

        Args:
            case_id: Batch task id.

        Returns:
            Latest task state, or ``None`` when the case is unknown.
        """

        row = self._connection.execute("SELECT * FROM batch_tasks WHERE task_id=?", (case_id,)).fetchone()
        return None if row is None else _state_from_row(row)

    def row_details(self, case_id: str) -> JsonObject:
        """Return the raw lease/projection details needed for diagnostics.

        Args:
            case_id: Batch task id.

        Returns:
            JSON-safe row payload containing task state and lease metadata.
        """

        row = self._row(case_id)
        return self._row_payload(row)

    def prepared_row_details(self, *, case_id: str, attempt: int = 0) -> JsonObject | None:
        """Return prepared-sandbox row details for maintenance decisions.

        Args:
            case_id: Batch task id associated with provider metadata.
            attempt: Optional one-based attempt from provider metadata.

        Returns:
            Prepared row details, newest first when attempt is omitted, or
            ``None`` when the prepared-sandbox table/row is absent.
        """

        if not _table_exists(self._connection, "prepared_sandboxes"):
            return None
        where = "task_id=?"
        params: tuple[object, ...] = (case_id,)
        if attempt > 0:
            where += " AND attempt=?"
            params = (case_id, attempt)
        row = self._connection.execute(
            f"""
            SELECT task_id, attempt, status, runner_id, sandbox_id, device_id,
                   updated_at, expires_at, error
            FROM prepared_sandboxes
            WHERE {where}
            ORDER BY attempt DESC, updated_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return None if row is None else {key: row[key] for key in row.keys()}

    def prepared_resource_sandbox_rows(self) -> list[JsonObject]:
        """Return prepared rows that still reference provider resources.

        Returns:
            JSON-safe rows for prepared sandboxes that still reference provider
            resources. Callers still perform task/lease/TTL safety checks before
            terminating anything.
        """

        if not _table_exists(self._connection, "prepared_sandboxes"):
            return []
        rows = self._connection.execute(
            """
            SELECT task_id, attempt, status, provider, template, sandbox_id,
                   device_id, lease_id, runner_id, updated_at, expires_at, error
            FROM prepared_sandboxes
            WHERE status IN ({placeholders}) AND sandbox_id<>''
            ORDER BY updated_at ASC
            """.format(placeholders=",".join("?" for _ in _PREPARED_TERMINATION_RESOLVABLE_STATUSES)),
            tuple(sorted(_PREPARED_TERMINATION_RESOLVABLE_STATUSES)),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def mark_prepared_cleanup_resolved(
        self,
        *,
        case_id: str,
        attempt: int,
        sandbox_id: str,
    ) -> tuple[JsonObject, JsonObject] | None:
        """Mark a prepared row resolved after sandbox termination.

        Args:
            case_id: Batch task id associated with the prepared sandbox.
            attempt: One-based prepared attempt.
            sandbox_id: Provider sandbox id that was terminated.

        Returns:
            Before/after prepared row payloads, or ``None`` when no resolvable
            row matched the terminated sandbox.
        """

        if attempt <= 0 or not _table_exists(self._connection, "prepared_sandboxes"):
            return None
        now = time.time()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT *
                FROM prepared_sandboxes
                WHERE task_id=? AND attempt=? AND status IN ({placeholders}) AND (sandbox_id=? OR sandbox_id='')
                """.format(placeholders=",".join("?" for _ in _PREPARED_TERMINATION_RESOLVABLE_STATUSES)),
                (
                    case_id,
                    attempt,
                    *sorted(_PREPARED_TERMINATION_RESOLVABLE_STATUSES),
                    sandbox_id,
                ),
            ).fetchone()
            if row is None:
                return None
            before = {key: row[key] for key in row.keys()}
            self._connection.execute(
                """
                UPDATE prepared_sandboxes
                SET status=?, released_at=?, updated_at=?, error=?, sandbox_id=?
                WHERE task_id=? AND attempt=? AND status IN ({placeholders}) AND (sandbox_id=? OR sandbox_id='')
                """.format(placeholders=",".join("?" for _ in _PREPARED_TERMINATION_RESOLVABLE_STATUSES)),
                (
                    PreparedSandboxStatus.RELEASED.value,
                    now,
                    now,
                    "cleanup resolved by maintenance terminate-sandboxes",
                    sandbox_id,
                    case_id,
                    attempt,
                    *sorted(_PREPARED_TERMINATION_RESOLVABLE_STATUSES),
                    sandbox_id,
                ),
            )
            after_row = self._connection.execute(
                "SELECT * FROM prepared_sandboxes WHERE task_id=? AND attempt=?",
                (case_id, attempt),
            ).fetchone()
            after = {key: after_row[key] for key in after_row.keys()} if after_row is not None else {}
        return before, after

    def stale_in_flight_states(self, *, older_than_seconds: float, case_id: str = "") -> list[BatchTaskState]:
        """Return expired in-flight rows eligible for maintenance planning.

        Args:
            older_than_seconds: Minimum row age based on ``updated_at``.
            case_id: Optional single case id.

        Returns:
            Pre-controller task states with no active lease and old
            ``updated_at``.
        """

        return self.stale_in_flight_states_for_statuses(
            older_than_seconds=older_than_seconds,
            statuses=_PRE_CONTROLLER_STALE_STATUSES,
            case_id=case_id,
        )

    def stale_in_flight_states_for_statuses(
        self,
        *,
        older_than_seconds: float,
        statuses: set[BatchTaskStatus],
        case_id: str = "",
    ) -> list[BatchTaskState]:
        """Return expired in-flight rows for the requested local statuses.

        This is used by pre-resume cleanup where the controller DB is available
        as an authority for submitted work. The default ``repair-stale`` path
        intentionally remains narrower and only resets pre-controller rows.

        Args:
            older_than_seconds: Minimum age based on ``updated_at``.
            statuses: Local task statuses that are eligible for stale repair.
            case_id: Optional single case id filter.

        Returns:
            Matching stale task states ordered by manifest order.
        """

        now = time.time()
        cutoff = now - older_than_seconds
        params: list[object] = [status.value for status in sorted(statuses, key=lambda item: item.value)]
        if not params:
            return []
        where = [
            "status IN ({})".format(",".join("?" for _ in params)),
            "(status<>? OR controller_task_id<>'' OR attempts>0)",
            "updated_at <= ?",
            "(lease_token='' OR lease_expires_at <= ?)",
        ]
        params.extend([BatchTaskStatus.PENDING.value, cutoff, now])
        if case_id:
            where.append("task_id=?")
            params.append(case_id)
        rows = self._connection.execute(
            f"SELECT * FROM batch_tasks WHERE {' AND '.join(where)} ORDER BY order_index ASC",
            tuple(params),
        ).fetchall()
        return [_state_from_row(row) for row in rows]

    def unleased_states_for_statuses(
        self,
        *,
        statuses: set[BatchTaskStatus],
        case_id: str = "",
        controller_task_id_required: bool | None = None,
    ) -> list[BatchTaskState]:
        """Return rows in requested statuses that no live runner currently owns.

        Pre-resume cleanup uses this broader query after a runner has been
        stopped. The active lease check is the safety boundary; controller
        terminal snapshots should be reconciled immediately rather than waiting
        for a wall-clock stale threshold and later colliding with deterministic
        task ids.

        Args:
            statuses: Local task statuses eligible for repair.
            case_id: Optional single case id filter.
            controller_task_id_required: When set, restricts the query to rows
                with or without a projected controller id. Pre-resume cleanup
                uses this to keep the deterministic-id hard barrier on small
                partial indexes in large batch tables.
        """

        now = time.time()
        if not statuses:
            return []
        if controller_task_id_required is True:
            rows = self._unleased_rows_for_statuses(
                statuses=statuses,
                now=now,
                case_id=case_id,
                controller_clause="AND controller_task_id<>''",
                pending_history_clause="AND (status<>? OR controller_task_id<>'' OR attempts>0)",
                extra_params=(BatchTaskStatus.PENDING.value,),
                empty_index_hint="INDEXED BY idx_batch_tasks_pre_resume_active_controller_empty_lease",
                expired_index_hint="INDEXED BY idx_batch_tasks_pre_resume_active_controller_expired_lease",
            )
        elif controller_task_id_required is False:
            non_pending_statuses = {status for status in statuses if status != BatchTaskStatus.PENDING}
            rows = self._unleased_rows_for_statuses(
                statuses=non_pending_statuses,
                now=now,
                case_id=case_id,
                controller_clause="AND controller_task_id=''",
            )
            if BatchTaskStatus.PENDING in statuses:
                rows.extend(self._unleased_pending_history_without_controller_rows(now=now, case_id=case_id))
        else:
            rows = self._unleased_rows_for_statuses(
                statuses=statuses,
                now=now,
                case_id=case_id,
                pending_history_clause="AND (status<>? OR controller_task_id<>'' OR attempts>0)",
                extra_params=(BatchTaskStatus.PENDING.value,),
            )
        rows.sort(key=lambda row: int(row["order_index"] or 0))
        return [_state_from_row(row) for row in rows]

    def live_leased_states_for_statuses(
        self,
        *,
        statuses: set[BatchTaskStatus],
        case_id: str = "",
        controller_task_id_required: bool | None = None,
        include_fresh_pending: bool = False,
    ) -> list[BatchTaskState]:
        """Return rows in requested statuses that still have an unexpired lease.

        This is intentionally separate from ``unleased_states_for_statuses``.
        Normal maintenance must treat an unexpired lease as a hard safety
        boundary. Pre-resume cleanup, however, runs only after the previous
        runner has been stopped; it must still see those rows so the hard
        barrier can converge dead-runner leases before a new scheduler starts.
        """

        now = time.time()
        if not statuses:
            return []
        if controller_task_id_required is True:
            rows = self._live_leased_rows_for_statuses(
                statuses=statuses,
                now=now,
                case_id=case_id,
                controller_clause="AND controller_task_id<>''",
                pending_history_clause="AND (status<>? OR controller_task_id<>'' OR attempts>0)",
                extra_params=(BatchTaskStatus.PENDING.value,),
                index_hint="INDEXED BY idx_batch_tasks_pre_resume_active_controller_live_lease",
            )
        elif controller_task_id_required is False:
            non_pending_statuses = {status for status in statuses if status != BatchTaskStatus.PENDING}
            rows = self._live_leased_rows_for_statuses(
                statuses=non_pending_statuses,
                now=now,
                case_id=case_id,
                controller_clause="AND controller_task_id=''",
                index_hint="INDEXED BY idx_batch_tasks_pre_resume_active_live_lease",
            )
            if BatchTaskStatus.PENDING in statuses:
                rows.extend(
                    self._live_leased_pending_without_controller_rows(
                        now=now,
                        case_id=case_id,
                        include_fresh_pending=include_fresh_pending,
                    )
                )
        else:
            rows = self._live_leased_rows_for_statuses(
                statuses=statuses,
                now=now,
                case_id=case_id,
                pending_history_clause="AND (status<>? OR controller_task_id<>'' OR attempts>0)",
                extra_params=(BatchTaskStatus.PENDING.value,),
                index_hint="INDEXED BY idx_batch_tasks_pre_resume_active_live_lease",
            )
        rows.sort(key=lambda row: int(row["order_index"] or 0))
        return [_state_from_row(row) for row in rows]

    def _unleased_rows_for_statuses(
        self,
        *,
        statuses: set[BatchTaskStatus],
        now: float,
        case_id: str,
        controller_clause: str = "",
        pending_history_clause: str = "",
        extra_params: tuple[object, ...] = (),
        empty_index_hint: str = "",
        expired_index_hint: str = "",
    ) -> list[sqlite3.Row]:
        params: list[object] = [status.value for status in sorted(statuses, key=lambda item: item.value)]
        if not params:
            return []
        status_where = "status IN ({})".format(",".join("?" for _ in params))
        case_clause = "AND task_id=?" if case_id else ""
        case_params: tuple[object, ...] = (case_id,) if case_id else ()
        empty_rows = self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks {empty_index_hint}
            WHERE {status_where} {pending_history_clause} AND lease_token='' {controller_clause} {case_clause}
            ORDER BY order_index ASC
            """,
            (*params, *extra_params, *case_params),
        ).fetchall()
        expired_rows = self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks {expired_index_hint}
            WHERE {status_where} {pending_history_clause} AND lease_token<>'' AND lease_expires_at<=? {controller_clause} {case_clause}
            ORDER BY order_index ASC
            """,
            (*params, *extra_params, now, *case_params),
        ).fetchall()
        return [*empty_rows, *expired_rows]

    def _live_leased_rows_for_statuses(
        self,
        *,
        statuses: set[BatchTaskStatus],
        now: float,
        case_id: str,
        controller_clause: str = "",
        pending_history_clause: str = "",
        extra_params: tuple[object, ...] = (),
        index_hint: str = "",
    ) -> list[sqlite3.Row]:
        params: list[object] = [status.value for status in sorted(statuses, key=lambda item: item.value)]
        if not params:
            return []
        status_where = "status IN ({})".format(",".join("?" for _ in params))
        case_clause = "AND task_id=?" if case_id else ""
        case_params: tuple[object, ...] = (case_id,) if case_id else ()
        return self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks {index_hint}
            WHERE {status_where} {pending_history_clause}
              AND lease_token<>'' AND lease_expires_at>? {controller_clause} {case_clause}
            ORDER BY order_index ASC
            """,
            (*params, *extra_params, now, *case_params),
        ).fetchall()

    def _unleased_pending_history_without_controller_rows(self, *, now: float, case_id: str) -> list[sqlite3.Row]:
        case_clause = "AND task_id=?" if case_id else ""
        case_params: tuple[object, ...] = (case_id,) if case_id else ()
        empty_rows = self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks INDEXED BY idx_batch_tasks_pre_resume_pending_history_empty_id
            WHERE status='pending' AND controller_task_id='' AND attempts>0 AND lease_token='' {case_clause}
            ORDER BY order_index ASC
            """,
            case_params,
        ).fetchall()
        expired_rows = self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks INDEXED BY idx_batch_tasks_pre_resume_pending_history_empty_id
            WHERE status='pending' AND controller_task_id='' AND attempts>0
              AND lease_token<>'' AND lease_expires_at<=? {case_clause}
            ORDER BY order_index ASC
            """,
            (now, *case_params),
        ).fetchall()
        return [*empty_rows, *expired_rows]

    def _live_leased_pending_without_controller_rows(
        self,
        *,
        now: float,
        case_id: str,
        include_fresh_pending: bool,
    ) -> list[sqlite3.Row]:
        case_clause = "AND task_id=?" if case_id else ""
        case_params: tuple[object, ...] = (case_id,) if case_id else ()
        history_clause = "" if include_fresh_pending else "AND attempts>0"
        index_hint = (
            "idx_batch_tasks_pre_resume_pending_live_lease"
            if include_fresh_pending
            else "idx_batch_tasks_pre_resume_pending_history_live_lease"
        )
        return self._connection.execute(
            f"""
            SELECT *
            FROM batch_tasks INDEXED BY {index_hint}
            WHERE status='pending' AND controller_task_id='' {history_clause}
              AND lease_token<>'' AND lease_expires_at>? {case_clause}
            ORDER BY order_index ASC
            """,
            (now, *case_params),
        ).fetchall()

    def reset_stale_pre_controller(
        self,
        *,
        case_id: str,
        reason: str,
        expected_row: JsonObject | None = None,
        older_than_seconds: float | None = None,
    ) -> tuple[JsonObject, JsonObject]:
        """Mark an expired pre-controller in-flight row failed and retryable.

        Args:
            case_id: Batch task id to repair.
            reason: Audit/error message explaining the repair.
            expected_row: Optional row details captured during planning. When
                present, generation/status/attempt/controller id/updated_at must
                still match and the row must not have an active lease.
            older_than_seconds: Optional stale cutoff to re-evaluate inside the
                write transaction.

        Returns:
            Pair of before/after task-state payloads.

        Raises:
            SQLiteMaintenanceError: If the row is terminal, actively leased, or
                already has a controller task id that must be reconciled first.
        """

        now = time.time()
        with self._transaction():
            row = self._row(case_id)
            before_state = _state_from_row(row)
            if expected_row is not None:
                self._assert_row_unchanged(row, expected_row=expected_row, action_name="reset stale row")
            if before_state.status in _TERMINAL_STATUSES:
                raise SQLiteMaintenanceError(f"refusing to reset terminal row {case_id}: {before_state.status.value}")
            if before_state.status not in _PRE_CONTROLLER_STALE_STATUSES:
                raise SQLiteMaintenanceError(
                    f"refusing to reset non pre-controller row {case_id}: {before_state.status.value}"
                )
            if before_state.controller_task_id:
                raise SQLiteMaintenanceError(
                    f"refusing to reset {case_id}; controller_task_id exists, run maintenance reconcile first"
                )
            if self._has_active_lease(row, now=now):
                raise SQLiteMaintenanceError(f"refusing to reset {case_id}; lease is still active")
            if older_than_seconds is not None and float(row["updated_at"] or 0.0) > now - older_than_seconds:
                raise SQLiteMaintenanceError(f"refusing to reset {case_id}; row is no longer stale")
            after_state = before_state.model_copy(
                update={
                    "status": BatchTaskStatus.FAILED,
                    "error": reason,
                    "last_failure_category": _category_for_stale_status(before_state.status),
                    "last_reconciled_at": utc_now_iso(),
                    "last_reconciled_status": "local_stale_reset",
                    "recovery_action": None,
                }
            )
            self._write_state(after_state, clear_lease=True, updated_at=now)
        return before_state.model_dump(mode="json"), after_state.model_dump(mode="json")

    def reset_stale_in_flight(
        self,
        *,
        case_id: str,
        reason: str,
        allowed_statuses: set[BatchTaskStatus],
        expected_row: JsonObject | None = None,
        older_than_seconds: float | None = None,
        clear_controller_task_id: bool = False,
        last_reconciled_status: str = "local_stale_reset",
        next_attempt_after: float | None = None,
        allow_active_lease: bool = False,
    ) -> tuple[JsonObject, JsonObject]:
        """Mark an expired in-flight row failed so normal retry policy can own it.

        Unlike ``reset_stale_pre_controller``, this method can be used by the
        DB-backed pre-resume cleanup after the controller DB has already been
        consulted. That allows stale ``submitted`` rows or rows with controller
        ids missing from the local controller DB to be released before the
        scheduler starts filling concurrency again.

        Args:
            case_id: Batch task id to repair.
            reason: Audit/error message explaining the repair.
            allowed_statuses: Statuses eligible for this broader stale reset.
            expected_row: Optional row details captured during planning.
            older_than_seconds: Optional stale cutoff to re-evaluate inside the
                write transaction.
            clear_controller_task_id: Whether to clear the local controller id
                after controller DB authority says it is not recoverable.
            last_reconciled_status: Marker written to the local task state.
            next_attempt_after: Optional retry backoff timestamp.

        Returns:
            Pair of before/after task-state payloads.

        Raises:
            SQLiteMaintenanceError: If the row changed, is terminal, is actively
                leased, is not in an allowed status, or is no longer stale.
        """

        now = time.time()
        with self._transaction():
            row = self._row(case_id)
            before_state = _state_from_row(row)
            if expected_row is not None:
                self._assert_row_unchanged(
                    row,
                    expected_row=expected_row,
                    action_name="reset stale in-flight row",
                    allow_active_lease=allow_active_lease,
                )
            if before_state.status in _TERMINAL_STATUSES:
                raise SQLiteMaintenanceError(f"refusing to reset terminal row {case_id}: {before_state.status.value}")
            if before_state.status not in allowed_statuses:
                raise SQLiteMaintenanceError(
                    f"refusing to reset unsupported stale row {case_id}: {before_state.status.value}"
                )
            if self._has_active_lease(row, now=now) and not allow_active_lease:
                raise SQLiteMaintenanceError(f"refusing to reset {case_id}; lease is still active")
            if older_than_seconds is not None and float(row["updated_at"] or 0.0) > now - older_than_seconds:
                raise SQLiteMaintenanceError(f"refusing to reset {case_id}; row is no longer stale")
            updates = {
                "status": BatchTaskStatus.FAILED,
                "error": reason,
                "last_failure_category": _category_for_stale_status(before_state.status),
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": last_reconciled_status,
                "recovery_action": None,
                "next_attempt_after": next_attempt_after if next_attempt_after is not None else 0.0,
            }
            if clear_controller_task_id:
                updates.update(
                    {
                        "controller_task_id": "",
                        "trace_id": "",
                        "session_id": "",
                        "device_id": "",
                        "terminal_state": None,
                        "terminal_decision": {},
                    }
                )
            after_state = before_state.model_copy(update=updates)
            self._write_state(after_state, clear_lease=True, updated_at=now)
        return before_state.model_dump(mode="json"), after_state.model_dump(mode="json")

    def persist_reconciled_state(
        self,
        state: BatchTaskState,
        *,
        expected_row: JsonObject | None = None,
        allow_active_lease: bool = False,
        clear_active_lease: bool = False,
    ) -> tuple[JsonObject, JsonObject]:
        """Persist a controller-authoritative task state.

        Args:
            state: Task state projected from controller status.
            expected_row: Optional row details captured during planning. When
                present, generation/status/attempt/controller id must still
                match and the row must not have an active lease.

        Returns:
            Pair of before/after JSON-safe task-state payloads.
        """

        with self._transaction():
            row = self._row(state.task_id)
            before = _state_from_row(row)
            if expected_row is not None:
                self._assert_row_unchanged(
                    row,
                    expected_row=expected_row,
                    action_name="reconcile",
                    allow_active_lease=allow_active_lease,
                )
            self._write_state(
                state,
                clear_lease=clear_active_lease or state.status in _TERMINAL_STATUSES,
                updated_at=time.time(),
            )
        return before.model_dump(mode="json"), state.model_dump(mode="json")

    def clear_active_lease(
        self,
        *,
        case_id: str,
        reason: str,
        expected_row: JsonObject | None = None,
        last_reconciled_status: str = "dead_runner_lease_pre_resume",
    ) -> tuple[JsonObject, JsonObject]:
        """Clear a dead runner lease while preserving the task business state."""

        _ = reason, last_reconciled_status
        now = time.time()
        with self._transaction():
            row = self._row(case_id)
            before = _state_from_row(row)
            if expected_row is not None:
                self._assert_row_unchanged(
                    row,
                    expected_row=expected_row,
                    action_name="clear active lease",
                    allow_active_lease=True,
                )
            if not self._has_active_lease(row, now=now):
                raise SQLiteMaintenanceError(f"refusing to clear {case_id}; lease is not active")
            after = before
            self._write_state(after, clear_lease=True, updated_at=now)
        return before.model_dump(mode="json"), after.model_dump(mode="json")

    def mark_case_canceled(
        self,
        *,
        case_id: str,
        reason: str,
        expected_row: JsonObject | None = None,
    ) -> tuple[JsonObject, JsonObject]:
        """Mark one case canceled without deleting trace/session/sandbox ids.

        Args:
            case_id: Batch task id to cancel locally.
            reason: Operator-facing cancellation reason.
            expected_row: Optional row details captured during planning. When
                present, generation/status/attempt/controller id/updated_at must
                still match and the row must not have an active lease.

        Returns:
            Pair of before/after JSON-safe task-state payloads.

        Raises:
            SQLiteMaintenanceError: If the case is unknown or already
                succeeded.
        """

        now = time.time()
        with self._transaction():
            row = self._row(case_id)
            before = _state_from_row(row)
            if expected_row is not None:
                self._assert_row_unchanged(row, expected_row=expected_row, action_name="mark case canceled")
            if before.status in _TERMINAL_STATUSES:
                raise SQLiteMaintenanceError(f"refusing to cancel terminal row {case_id}: {before.status.value}")
            if before.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
                raise SQLiteMaintenanceError(
                    f"refusing to locally cancel submitted row {case_id}: {before.status.value}"
                )
            if before.controller_task_id:
                raise SQLiteMaintenanceError(
                    f"refusing to locally cancel {case_id}; controller_task_id exists, run maintenance cancel/reconcile"
                )
            after = before.model_copy(
                update={
                    "status": BatchTaskStatus.CANCELED,
                    "error": reason,
                    "last_reconciled_at": utc_now_iso(),
                    "last_reconciled_status": "operator_canceled",
                }
            )
            self._write_state(after, clear_lease=True, cancel_requested=True, updated_at=now)
        return before.model_dump(mode="json"), after.model_dump(mode="json")

    def _require_state(self, case_id: str) -> BatchTaskState:
        state = self.get_state(case_id)
        if state is None:
            raise SQLiteMaintenanceError(f"unknown batch case: {case_id}")
        return state

    def _row(self, case_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM batch_tasks WHERE task_id=?", (case_id,)).fetchone()
        if row is None:
            raise SQLiteMaintenanceError(f"unknown batch case: {case_id}")
        return row

    def _write_state(
        self,
        state: BatchTaskState,
        *,
        clear_lease: bool,
        updated_at: float,
        cancel_requested: bool | None = None,
    ) -> None:
        values = _state_update_values(state, updated_at=updated_at)
        lease_update = (
            ", lease_owner='', lease_token='', lease_expires_at=0, generation=generation+1" if clear_lease else ""
        )
        cancel_update = "" if cancel_requested is None else ", cancel_requested=?"
        params: tuple[object, ...]
        if cancel_requested is None:
            params = (*values, state.task_id)
        else:
            params = (*values, 1 if cancel_requested else 0, state.task_id)
        self._connection.execute(
            f"""
            UPDATE batch_tasks
            SET state_json=?, status=?, attempts=?, controller_task_id=?,
                trace_id=?, session_id=?, sandbox_id=?, device_id=?,
                terminal_state=?, error=?, recovery_action=?,
                last_failure_category=?, last_reconciled_at=?,
                last_reconciled_status=?, cleanup_status=?,
                retry_budget_remaining=?, next_attempt_after=?, updated_at=?
                {lease_update}
                {cancel_update}
            WHERE task_id=?
            """,
            params,
        )

    def assert_row_unchanged(
        self,
        case_id: str,
        *,
        expected_row: JsonObject | None,
        action_name: str,
    ) -> None:
        """Raise when a maintenance plan no longer matches the SQLite row.

        Args:
            case_id: Batch task id to validate.
            expected_row: Row details captured during planning.
            action_name: Human-readable action name used in errors.

        Raises:
            SQLiteMaintenanceError: If the plan is missing expected row data,
                the row has an active lease, or the row generation/status/
                attempt/controller id/updated_at changed after planning.
        """

        if expected_row is None:
            raise SQLiteMaintenanceError(f"{action_name} plan is missing expected row")
        row = self._row(case_id)
        self._assert_row_unchanged(row, expected_row=expected_row, action_name=action_name)

    def _assert_row_unchanged(
        self,
        row: sqlite3.Row,
        *,
        expected_row: JsonObject,
        action_name: str,
        allow_active_lease: bool = False,
    ) -> None:
        expected_state = expected_row.get("state")
        if not isinstance(expected_state, dict):
            raise SQLiteMaintenanceError(f"{action_name} plan is missing expected row state")
        if self._has_active_lease(row, now=time.time()) and not allow_active_lease:
            raise SQLiteMaintenanceError(f"refusing to {action_name} {row['task_id']}; lease is still active")
        checks = {
            "generation": _int_value(expected_row.get("generation"), default=-1),
            "status": str(expected_state.get("status", "")),
            "attempts": _int_value(expected_state.get("attempts"), default=-1),
            "controller_task_id": str(expected_state.get("controller_task_id", "")),
            "updated_at": _float_value(expected_row.get("updated_at"), default=-1.0),
        }
        actual = {
            "generation": int(row["generation"] or 0),
            "status": str(row["status"] or ""),
            "attempts": int(row["attempts"] or 0),
            "controller_task_id": str(row["controller_task_id"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }
        if allow_active_lease:
            checks.update(
                {
                    "lease_owner": str(expected_row.get("lease_owner", "")),
                    "lease_token_present": bool(expected_row.get("lease_token_present", False)),
                    "lease_token_sha12": str(expected_row.get("lease_token_sha12", "")),
                    "lease_expires_at": _float_value(expected_row.get("lease_expires_at"), default=-1.0),
                }
            )
            actual.update(
                {
                    "lease_owner": str(row["lease_owner"] or ""),
                    "lease_token_present": bool(str(row["lease_token"] or "")),
                    "lease_token_sha12": _sha12(str(row["lease_token"] or "")),
                    "lease_expires_at": float(row["lease_expires_at"] or 0.0),
                }
            )
        if actual != checks:
            raise SQLiteMaintenanceError(
                f"refusing to {action_name} {row['task_id']}; row changed since maintenance plan was built"
            )

    @staticmethod
    def _row_payload(row: sqlite3.Row) -> JsonObject:
        state = _state_from_row(row)
        return {
            "state": state.model_dump(mode="json"),
            "lease_owner": str(row["lease_owner"] or ""),
            "lease_token_present": bool(str(row["lease_token"] or "")),
            "lease_token_sha12": _sha12(str(row["lease_token"] or "")),
            "lease_expires_at": float(row["lease_expires_at"] or 0.0),
            "generation": int(row["generation"] or 0),
            "cancel_requested": bool(int(row["cancel_requested"] or 0)),
            "updated_at": float(row["updated_at"] or 0.0),
            "max_attempts": int(row["max_attempts"] or 0) if "max_attempts" in row.keys() else 0,
            "max_retries": int(row["max_retries"] or 0) if "max_retries" in row.keys() else 0,
        }

    @staticmethod
    def _has_active_lease(row: sqlite3.Row, *, now: float) -> bool:
        return bool(str(row["lease_token"] or "")) and float(row["lease_expires_at"] or 0.0) > now

    def _transaction(self) -> _SQLiteTransaction:
        return _SQLiteTransaction(self._connection)


def _category_for_stale_status(status: BatchTaskStatus) -> FailureCategory:
    if status == BatchTaskStatus.STARTING_SANDBOX:
        return FailureCategory.SANDBOX_CREATE
    if status == BatchTaskStatus.BOOTSTRAPPING_DEVICE:
        return FailureCategory.DEVICE_BOOTSTRAP
    if status == BatchTaskStatus.WAITING_DEVICE:
        return FailureCategory.READINESS_TIMEOUT
    if status in {BatchTaskStatus.PENDING, BatchTaskStatus.SUBMITTED}:
        return FailureCategory.CONTROLLER_SUBMIT
    return FailureCategory.MAIN_AGENT


def _state_from_row(row: sqlite3.Row) -> BatchTaskState:
    """Return task state with SQLite projection columns as authority.

    Some recovery paths can clear indexed columns such as ``controller_task_id``
    before an older ``state_json`` payload is rewritten. Maintenance planning
    must use the same projection columns that its compare-and-swap guard checks,
    otherwise pre-resume cleanup can repeatedly plan stale controller work that
    has already been cleared.
    """

    try:
        payload = json.loads(str(row["state_json"] or "{}"))
    except json.JSONDecodeError:
        payload = {"task_id": str(row["task_id"] or "")}
    payload = _sanitized_state_payload(payload, fallback_task_id=str(row["task_id"] or ""))
    keys = set(row.keys())

    def value(name: str, default: object = "") -> object:
        if name not in keys:
            return default
        raw = row[name]
        return default if raw is None else raw

    projected = {
        **payload,
        "task_id": str(value("task_id", payload.get("task_id", ""))),
        "status": str(value("status", payload.get("status", BatchTaskStatus.PENDING.value))),
        "attempts": _int_value(value("attempts", payload.get("attempts", 0)), default=0),
        "controller_task_id": str(value("controller_task_id", "")),
        "trace_id": str(value("trace_id", "")),
        "session_id": str(value("session_id", "")),
        "sandbox_id": str(value("sandbox_id", "")),
        "device_id": str(value("device_id", "")),
        "terminal_state": value("terminal_state", None) or None,
        "error": str(value("error", "")),
        "recovery_action": _valid_recovery_action_from_values(
            value("recovery_action", None), payload.get("recovery_action")
        ),
        "last_failure_category": value("last_failure_category", None) or None,
        "last_reconciled_at": str(value("last_reconciled_at", "")),
        "last_reconciled_status": str(value("last_reconciled_status", "")),
        "cleanup_status": str(value("cleanup_status", "")),
        "retry_budget_remaining": _int_value(
            value("retry_budget_remaining", payload.get("retry_budget_remaining", 0)),
            default=0,
        ),
        "next_attempt_after": _float_value(
            value("next_attempt_after", payload.get("next_attempt_after", 0.0)),
            default=0.0,
        ),
        "post_run_results": _list_value(payload.get("post_run_results")),
        "reconcile_unknown_count": _int_value(payload.get("reconcile_unknown_count", 0), default=0),
        "terminal_decision": _dict_value(payload.get("terminal_decision")),
    }
    return BatchTaskState.model_validate(projected)


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list_value(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_value(value: object, *, default: int) -> int:
    if isinstance(value, int | float | str):
        return int(value)
    return default


def _float_value(value: object, *, default: float) -> float:
    if isinstance(value, int | float | str):
        return float(value)
    return default


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _sha12(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
