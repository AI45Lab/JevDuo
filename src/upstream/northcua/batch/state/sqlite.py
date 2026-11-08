"""SQLite-backed batch state repository with local task leases."""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, TypeVar

from northcua.batch.contracts import BatchManifest, BatchTaskState, BatchTaskStatus
from northcua.batch.selected_retry import SelectedRetryPlan
from northcua.batch.state.contracts import BatchStateRepository, BatchTaskLease, LeasePolicy
from northcua.batch.state.sqlite_cancel import mark_cancel_requested
from northcua.batch.state.sqlite_helpers import (
    _control_flags,
    _create_schema,
    _first_claimable_row,
    _load_legacy_states,
    _next_retry_after,
    _row_needs_controller_reconciliation,
    _SQLiteTransaction,
    _state_from_json_text,
    _state_row_values,
    _state_update_values,
)

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_RepoT = TypeVar("_RepoT", bound="SQLiteBatchStateRepository")


def _synchronized(
    method: Callable[Concatenate[_RepoT, _P], _R],
) -> Callable[Concatenate[_RepoT, _P], _R]:
    """Serialize SQLite repository access on the connection lock.

    Args:
        method: Repository method that touches the shared SQLite connection.

    Returns:
        Wrapped method that holds the repository re-entrant lock while it runs.
    """

    @wraps(method)
    def wrapper(self: _RepoT, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Run the wrapped repository method under the connection lock.

        Returns:
            Value returned by the wrapped method.
        """

        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_CLAIM_SCAN_LIMIT = max(_env_int("NORTHCUA_BATCH_CLAIM_SCAN_LIMIT", 2048), 1)
_CLAIM_SLOW_LOG_SECONDS = max(_env_float("NORTHCUA_BATCH_CLAIM_SLOW_LOG_SECONDS", 1.0), 0.0)
_CLAIM_SLOW_LOG_INTERVAL_SECONDS = max(
    _env_float("NORTHCUA_BATCH_CLAIM_SLOW_LOG_INTERVAL_SECONDS", 30.0),
    0.0,
)
_CLAIM_ROW_COLUMNS = """
    task_id, state_json, status, attempts, controller_task_id,
    last_reconciled_status, recovery_action, last_failure_category,
    retry_budget_remaining, next_attempt_after, max_attempts,
    lease_token, lease_expires_at, generation, order_index
"""

_CLAIMABLE_STATUSES = {
    BatchTaskStatus.PENDING.value,
    BatchTaskStatus.FAILED.value,
    BatchTaskStatus.TIMED_OUT.value,
    BatchTaskStatus.STARTING_SANDBOX.value,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE.value,
    BatchTaskStatus.WAITING_DEVICE.value,
    BatchTaskStatus.SUBMITTED.value,
    BatchTaskStatus.RUNNING.value,
}

_ACTIVE_CLAIM_STATUSES = {
    BatchTaskStatus.SUBMITTED.value,
    BatchTaskStatus.RUNNING.value,
}

_ACTIVE_PREPARED_STATUSES = {
    "reserved",
    "preparing",
    "prepared",
}


class SQLiteBatchStateRepository(BatchStateRepository):
    """SQLite coordination store for one local batch output directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        _create_schema(self._connection)
        self._last_claim_slow_log_at = 0.0

    def run_locked(self, operation: Callable[[], _R]) -> _R:
        """Run a synchronous repository operation under the connection lock.

        Args:
            operation: Zero-argument callable that uses this repository's
                SQLite connection.

        Returns:
            Value returned by ``operation``.
        """

        with self._lock:
            return operation()

    @_synchronized
    def initialize_manifest(self, manifest: BatchManifest, *, legacy_jsonl_path: str = "") -> None:
        """Seed task rows and optionally import explicit legacy JSONL state.

        Args:
            manifest: Manifest being executed.
            legacy_jsonl_path: Optional one-time pre-SQLite JSONL migration path.
                Callers should pass this only while creating a missing SQLite
                state database; after initialization, SQLite remains the only
                scheduling and resume state source.
        """

        legacy_states = _load_legacy_states(Path(legacy_jsonl_path)) if legacy_jsonl_path else {}
        now = time.time()
        with self._transaction():
            for index, task in enumerate(manifest.tasks):
                max_retries = manifest.defaults.max_retries if task.max_retries < 0 else task.max_retries
                max_attempts = max_retries + 1
                state = legacy_states.get(task.task_id) or BatchTaskState(task_id=task.task_id)
                state = state.model_copy(update={"retry_budget_remaining": max(max_attempts - state.attempts, 0)})
                state_values = _state_row_values(state, order_index=index, updated_at=now)
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO batch_tasks(
                        task_id, order_index, state_json, status, attempts,
                        controller_task_id, trace_id, session_id, sandbox_id,
                        device_id, terminal_state, error, recovery_action,
                        last_failure_category, last_reconciled_at,
                        last_reconciled_status, cleanup_status,
                        retry_budget_remaining, next_attempt_after,
                        max_retries, max_attempts, lease_owner,
                        lease_token, lease_expires_at, generation,
                        cancel_requested, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, 0, 0, ?)
                    """,
                    (*state_values[:-1], max_retries, max_attempts, state_values[-1]),
                )
                self._connection.execute(
                    """
                    UPDATE batch_tasks
                    SET order_index=?,
                        max_retries=?,
                        max_attempts=?,
                        retry_budget_remaining=CASE
                            WHEN status IN (?, ?) THEN retry_budget_remaining
                            WHEN ? - attempts > 0 THEN ? - attempts
                            ELSE 0
                        END,
                        updated_at=?
                    WHERE task_id=?
                    """,
                    (
                        index,
                        max_retries,
                        max_attempts,
                        BatchTaskStatus.SUCCEEDED.value,
                        BatchTaskStatus.CANCELED.value,
                        max_attempts,
                        max_attempts,
                        now,
                        task.task_id,
                    ),
                )

    @_synchronized
    def reset_selected_for_retry(
        self,
        plan: SelectedRetryPlan,
        *,
        actor: str = "batch resume",
    ) -> dict[str, int | bool]:
        """Atomically requeue exactly the terminal tasks named by a retry plan."""

        control_name = f"selected_retry_plan:{plan.plan_id}"
        now = time.time()
        source_counts = {BatchTaskStatus.FAILED.value: 0, BatchTaskStatus.CANCELED.value: 0}
        with self._transaction():
            marker = self._connection.execute(
                "SELECT value FROM batch_control WHERE name=?",
                (control_name,),
            ).fetchone()
            if marker is not None:
                if str(marker["value"]) != plan.sha256:
                    raise RuntimeError(
                        f"selected retry plan id was already used with different content: {plan.plan_id}"
                    )
                return {
                    "selected": len(plan.targets),
                    "source_failed": 0,
                    "source_canceled": 0,
                    "already_applied": True,
                }

            flags = _control_flags(self._connection)
            if not flags.get("drain_requested"):
                raise RuntimeError("selected retry reset requires drain_requested=true")
            if flags.get("cancel_requested"):
                raise RuntimeError("selected retry reset requires cancel_requested=false")

            placeholders = ",".join("?" for _target in plan.targets)
            rows = self._connection.execute(
                f"SELECT * FROM batch_tasks WHERE task_id IN ({placeholders})",  # noqa: S608 - placeholders only.
                tuple(target.task_id for target in plan.targets),
            ).fetchall()
            rows_by_id = {str(row["task_id"]): row for row in rows}
            missing = [target.task_id for target in plan.targets if target.task_id not in rows_by_id]
            if missing:
                raise RuntimeError(f"selected retry tasks missing from database: {missing[:5]}")

            for target in plan.targets:
                row = rows_by_id[target.task_id]
                actual_status = str(row["status"])
                actual_attempts = int(row["attempts"] or 0)
                actual_budget = int(row["retry_budget_remaining"] or 0)
                actual_category = str(row["last_failure_category"] or "")
                actual_error_sha256 = hashlib.sha256(str(row["error"] or "").encode("utf-8")).hexdigest()
                if (
                    actual_status != target.expected_status.value
                    or actual_attempts != target.expected_attempts
                    or actual_budget != target.expected_retry_budget_remaining
                    or actual_category != target.expected_failure_category
                    or (target.expected_error_sha256 and actual_error_sha256 != target.expected_error_sha256)
                ):
                    raise RuntimeError(
                        "selected retry precondition drift for "
                        f"{target.task_id}: status={actual_status!r} attempts={actual_attempts} "
                        f"budget={actual_budget} category={actual_category!r}"
                    )
                if str(row["lease_token"] or ""):
                    raise RuntimeError(f"selected retry target still has a live lease token: {target.task_id}")
                source_counts[actual_status] += 1

            for target in plan.targets:
                row = rows_by_id[target.task_id]
                prior = _state_from_row(row)
                state = prior.model_copy(
                    update={
                        "status": BatchTaskStatus.PENDING,
                        "controller_task_id": "",
                        "trace_id": "",
                        "session_id": "",
                        "sandbox_id": "",
                        "device_id": "",
                        "terminal_state": None,
                        "error": "",
                        "recovery_action": None,
                        "last_failure_category": None,
                        "last_reconciled_at": "",
                        "last_reconciled_status": "",
                        "cleanup_status": f"selected_retry_reset:{plan.plan_id}",
                        "post_run_results": [],
                        "retry_budget_remaining": plan.extra_attempts,
                        "next_attempt_after": 0.0,
                        "reconcile_unknown_count": 0,
                        "terminal_decision": {},
                        "agent_terminal": {},
                        "evaluation_status": "",
                        "evaluation_error": {},
                        "evaluation_attempts": 0,
                        "evaluation_projection_level": "",
                    }
                )
                values = _state_update_values(state, updated_at=now)
                cursor = self._connection.execute(
                    """
                    UPDATE batch_tasks
                    SET state_json=?, status=?, attempts=?, controller_task_id=?,
                        trace_id=?, session_id=?, sandbox_id=?, device_id=?,
                        terminal_state=?, error=?, recovery_action=?,
                        last_failure_category=?, last_reconciled_at=?,
                        last_reconciled_status=?, cleanup_status=?,
                        retry_budget_remaining=?, next_attempt_after=?,
                        max_retries=?, max_attempts=?, lease_owner='',
                        lease_token='', lease_expires_at=0,
                        generation=generation+1, cancel_requested=0, updated_at=?
                    WHERE task_id=? AND status=? AND attempts=?
                    """,
                    (
                        *values[:-1],
                        target.max_attempts - 1,
                        target.max_attempts,
                        values[-1],
                        target.task_id,
                        target.expected_status.value,
                        target.expected_attempts,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"selected retry target changed during reset: {target.task_id}")

            reason = f"sha256={plan.sha256} targets={len(plan.targets)} extra_attempts={plan.extra_attempts}"
            updated_by = actor or _default_control_actor()
            self._connection.execute(
                """
                INSERT INTO batch_control(name, value, updated_by, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (control_name, plan.sha256, updated_by, reason, now),
            )
            self._connection.execute(
                """
                INSERT INTO batch_control_events(name, value, updated_by, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (control_name, plan.sha256, updated_by, reason, now),
            )
        return {
            "selected": len(plan.targets),
            "source_failed": source_counts[BatchTaskStatus.FAILED.value],
            "source_canceled": source_counts[BatchTaskStatus.CANCELED.value],
            "already_applied": False,
        }

    @_synchronized
    def claim_next(
        self,
        *,
        lease_owner: str,
        policy: LeasePolicy,
        max_attempts_by_task: dict[str, int] | None = None,
        respect_prepared_reservations: bool = False,
        prepared_reservation_stale_after_seconds: float = 0.0,
        prepared_ready_task_attempts: set[tuple[str, int]] | None = None,
    ) -> BatchTaskLease | None:
        """Atomically claim one runnable task for a local runner.

        Args:
            lease_owner: Stable id for the runner process.
            policy: Lease timing policy used to compute expiration.
            max_attempts_by_task: Optional total-attempt budget per task.
            respect_prepared_reservations: Whether this runner should avoid
                tasks currently reserved by another prepared-sandbox manager.
            prepared_reservation_stale_after_seconds: Liveness grace for
                prepared rows owned by other runners.
            prepared_ready_task_attempts: Optional allowlist of current-runner
                task attempts with ready in-memory prepared leases. This is
                used under controller backpressure so workers do not claim
                reserved/preparing rows and then fall back to direct create.

        Returns:
            Lease for the next runnable task, or ``None`` when all tasks are
            terminal or currently owned by a live runner.
        """

        started_at = time.monotonic()
        stats: dict[str, int | bool] = {}
        selected = False
        try:
            now = time.time()
            expires_at = now + policy.lease_timeout_seconds
            lease_token = uuid.uuid4().hex
            with self._transaction():
                flags = _control_flags(self._connection)
                if flags.get("paused") or flags.get("drain_requested"):
                    stats["control_blocked"] = True
                    return None
                blocked_keys: set[tuple[str, int]] = set()
                prepared_ranks: dict[tuple[str, int], int] = {}
                if respect_prepared_reservations:
                    blocked_keys = _prepared_reserved_task_attempts(
                        self._connection,
                        lease_owner=lease_owner,
                        now=now,
                        stale_after_seconds=prepared_reservation_stale_after_seconds,
                    )
                    prepared_ranks = _prepared_owned_task_attempt_ranks(
                        self._connection,
                        lease_owner=lease_owner,
                        now=now,
                    )
                stats["blocked_keys"] = len(blocked_keys)
                stats["prepared_ranks"] = len(prepared_ranks)
                stats["prepared_ready_keys"] = len(prepared_ready_task_attempts or ())
                row = self._first_claimable_candidate(
                    max_attempts_by_task=max_attempts_by_task,
                    now=now,
                    prepared_ready_task_attempts=prepared_ready_task_attempts,
                    blocked_keys=blocked_keys,
                    prepared_ranks=prepared_ranks,
                    stats=stats,
                )
                if row is None:
                    return None
                selected = True
                generation = int(row["generation"]) + 1
                cursor = self._connection.execute(
                    """
                    UPDATE batch_tasks
                    SET lease_owner=?, lease_token=?, lease_expires_at=?,
                        generation=?, updated_at=?
                    WHERE task_id=? AND generation=? AND (
                        lease_token='' OR lease_expires_at <= ?
                    )
                    """,
                    (
                        lease_owner,
                        lease_token,
                        expires_at,
                        generation,
                        now,
                        row["task_id"],
                        row["generation"],
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    stats["lost_update"] = True
                    return None
            state = self.get(str(row["task_id"]))
            if state is None:
                stats["state_missing"] = True
                return None
            return BatchTaskLease(
                task_id=str(row["task_id"]),
                lease_owner=lease_owner,
                lease_token=lease_token,
                generation=generation,
                state=state,
            )
        finally:
            self._maybe_log_claim_latency(
                "claim_next",
                started_at=started_at,
                selected=selected,
                stats=stats,
            )

    @_synchronized
    def next_claim_after(
        self,
        *,
        lease_owner: str,
        max_attempts_by_task: dict[str, int] | None = None,
    ) -> float | None:
        """Return the earliest delayed retry timestamp for this runner.

        Args:
            lease_owner: Runner id requesting the next claim time.
            max_attempts_by_task: Optional per-task retry limits.

        Returns:
            Earliest Unix timestamp when a delayed task becomes claimable.
        """

        started_at = time.monotonic()
        stats: dict[str, int | bool] = {}
        try:
            now = time.time()
            _ = lease_owner
            next_after: float | None = None
            last_order_index = -1
            while True:
                rows = self._claim_candidate_rows(
                    now=now,
                    limit=_CLAIM_SCAN_LIMIT,
                    after_order_index=last_order_index,
                    stats=stats,
                )
                if not rows:
                    return next_after
                candidate = _next_retry_after(rows, max_attempts_by_task=max_attempts_by_task, now=now)
                if candidate is not None:
                    next_after = min(next_after, candidate) if next_after is not None else candidate
                last_order_index = int(rows[-1]["order_index"])
        finally:
            self._maybe_log_claim_latency(
                "next_claim_after",
                started_at=started_at,
                selected=False,
                stats=stats,
            )

    @_synchronized
    def heartbeat(self, lease: BatchTaskLease, *, policy: LeasePolicy) -> bool:
        """Extend a current lease if the caller still owns it.

        Args:
            lease: Lease to refresh.
            policy: Lease timing policy used to compute the new expiration.

        Returns:
            ``True`` when the lease token still matches the current owner.
        """

        now = time.time()
        cursor = self._connection.execute(
            """
            UPDATE batch_tasks
            SET lease_expires_at=?, updated_at=?
            WHERE task_id=? AND lease_token=? AND generation=? AND cancel_requested=0
            """,
            (
                now + policy.lease_timeout_seconds,
                now,
                lease.task_id,
                lease.lease_token,
                lease.generation,
            ),
        )
        return cursor.rowcount == 1

    @_synchronized
    def upsert_state(self, state: BatchTaskState, *, lease_token: str = "") -> bool:
        """Persist latest state when ownership allows the write.

        Args:
            state: Latest task state to store.
            lease_token: Active lease token; empty writes only succeed for
                rows without a current owner.

        Returns:
            ``True`` when the state write was accepted.
        """

        now = time.time()
        values = _state_update_values(state, updated_at=now)
        if lease_token:
            cursor = self._connection.execute(
                """
                UPDATE batch_tasks
                SET state_json=?, status=?, attempts=?, controller_task_id=?,
                    trace_id=?, session_id=?, sandbox_id=?, device_id=?,
                    terminal_state=?, error=?, recovery_action=?,
                    last_failure_category=?, last_reconciled_at=?,
                    last_reconciled_status=?, cleanup_status=?,
                    retry_budget_remaining=?, next_attempt_after=?, updated_at=?
                WHERE task_id=? AND lease_token=? AND (cancel_requested=0 OR ?=?)
                """,
                (*values, state.task_id, lease_token, state.status.value, BatchTaskStatus.CANCELED.value),
            )
            return cursor.rowcount == 1
        cursor = self._connection.execute(
            """
            UPDATE batch_tasks
            SET state_json=?, status=?, attempts=?, controller_task_id=?,
                trace_id=?, session_id=?, sandbox_id=?, device_id=?,
                terminal_state=?, error=?, recovery_action=?,
                last_failure_category=?, last_reconciled_at=?,
                last_reconciled_status=?, cleanup_status=?,
                retry_budget_remaining=?, next_attempt_after=?, updated_at=?
            WHERE task_id=? AND lease_token='' AND (cancel_requested=0 OR ?=?)
            """,
            (*values, state.task_id, state.status.value, BatchTaskStatus.CANCELED.value),
        )
        return cursor.rowcount == 1

    @_synchronized
    def release_claim(self, lease: BatchTaskLease, *, terminal_state: BatchTaskState | None = None) -> bool:
        """Release ownership, optionally storing a final state first.

        Args:
            lease: Active lease being released.
            terminal_state: Optional final state written before clearing owner
                fields.

        Returns:
            ``True`` when the release matched the active lease token.
        """

        now = time.time()
        if terminal_state is not None and not self.upsert_state(terminal_state, lease_token=lease.lease_token):
            return False
        cursor = self._connection.execute(
            """
            UPDATE batch_tasks
            SET lease_owner='', lease_token='', lease_expires_at=0, updated_at=?
            WHERE task_id=? AND lease_token=? AND generation=?
            """,
            (now, lease.task_id, lease.lease_token, lease.generation),
        )
        return cursor.rowcount == 1

    @_synchronized
    def mark_cancel_requested(self) -> None:
        """Cancel unclaimed nonterminal tasks and block future claims."""

        mark_cancel_requested(self._connection)

    @_synchronized
    def cancel_requested(self, task_id: str) -> bool:
        """Return whether a task has an explicit operator cancel marker."""

        row = self._connection.execute(
            """
            SELECT cancel_requested
            FROM batch_tasks
            WHERE task_id=?
            """,
            (task_id,),
        ).fetchone()
        return bool(row is not None and int(row["cancel_requested"] or 0))

    @_synchronized
    def set_control_flag(self, name: str, enabled: bool, *, actor: str = "", reason: str = "") -> None:
        """Persist a scheduler control flag for this batch DB.

        Args:
            name: Stable flag name such as ``paused`` or ``drain_requested``.
            enabled: Whether the scheduler should treat the flag as active.
            actor: Operator or subsystem changing the flag.
            reason: Human-readable reason for audit.
        """

        now = time.time()
        updated_by = actor or _default_control_actor()
        self._connection.execute(
            """
            INSERT INTO batch_control(name, value, updated_by, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value=excluded.value,
                updated_by=excluded.updated_by,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (name, "1" if enabled else "0", updated_by, reason, now),
        )
        self._connection.execute(
            """
            INSERT INTO batch_control_events(name, value, updated_by, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, "1" if enabled else "0", updated_by, reason, now),
        )

    @_synchronized
    def control_flags(self) -> dict[str, bool]:
        """Return current scheduler control flags.

        Returns:
            Mapping of flag name to boolean value.
        """

        return _control_flags(self._connection)

    @_synchronized
    def get(self, task_id: str) -> BatchTaskState | None:
        """Return latest task state by id.

        Args:
            task_id: Manifest task id.

        Returns:
            Latest stored state, or ``None`` when the task is unknown.
        """

        row = self._connection.execute("SELECT * FROM batch_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return _state_from_row(row)

    @_synchronized
    def all(self) -> list[BatchTaskState]:
        """Return latest states in manifest order.

        Returns:
            Latest task states ordered by manifest position.
        """

        rows = self._connection.execute("SELECT * FROM batch_tasks ORDER BY order_index ASC").fetchall()
        return [_state_from_row(row) for row in rows]

    def _transaction(self) -> "_SQLiteTransaction":
        return _SQLiteTransaction(self._connection)

    def _first_claimable_candidate(
        self,
        *,
        max_attempts_by_task: dict[str, int] | None,
        now: float,
        prepared_ready_task_attempts: set[tuple[str, int]] | None,
        blocked_keys: set[tuple[str, int]],
        prepared_ranks: dict[tuple[str, int], int],
        stats: dict[str, int | bool],
    ) -> sqlite3.Row | None:
        if prepared_ranks:
            rows = self._claim_candidate_rows_for_task_ids(
                task_ids=[task_id for task_id, _attempt in prepared_ranks],
                now=now,
                stats=stats,
            )
            rows = self._filter_claim_candidates(
                rows,
                max_attempts_by_task=max_attempts_by_task,
                prepared_ready_task_attempts=prepared_ready_task_attempts,
                blocked_keys=blocked_keys,
            )
            rows = sorted(
                rows,
                key=lambda row: (
                    _claim_priority(row, max_attempts_by_task=max_attempts_by_task),
                    prepared_ranks.get((str(row["task_id"]), int(row["attempts"] or 0) + 1), 99),
                    int(row["order_index"]),
                ),
            )
            row = _first_claimable_row(rows, max_attempts_by_task=max_attempts_by_task, now=now)
            if row is not None:
                return row

        active_rows = self._filter_claim_candidates(
            self._claim_candidate_rows_by_statuses(
                statuses=_ACTIVE_CLAIM_STATUSES,
                now=now,
                limit=_CLAIM_SCAN_LIMIT,
                stats=stats,
            ),
            max_attempts_by_task=max_attempts_by_task,
            prepared_ready_task_attempts=prepared_ready_task_attempts,
            blocked_keys=blocked_keys,
        )
        row = _first_claimable_row(active_rows, max_attempts_by_task=max_attempts_by_task, now=now)
        if row is not None:
            return row

        last_fresh_order_index = -1
        while True:
            fresh_pending_rows = self._claim_fresh_pending_rows(
                now=now,
                limit=_CLAIM_SCAN_LIMIT,
                after_order_index=last_fresh_order_index,
                stats=stats,
            )
            if not fresh_pending_rows:
                break
            last_fresh_order_index = int(fresh_pending_rows[-1]["order_index"])
            fresh_pending_rows = self._filter_claim_candidates(
                fresh_pending_rows,
                max_attempts_by_task=max_attempts_by_task,
                prepared_ready_task_attempts=prepared_ready_task_attempts,
                blocked_keys=blocked_keys,
            )
            row = _first_claimable_row(fresh_pending_rows, max_attempts_by_task=max_attempts_by_task, now=now)
            if row is not None:
                return row

        last_order_index = -1
        while True:
            rows = self._claim_candidate_rows(
                now=now,
                limit=_CLAIM_SCAN_LIMIT,
                after_order_index=last_order_index,
                stats=stats,
            )
            if not rows:
                return None
            last_order_index = int(rows[-1]["order_index"])
            rows = self._filter_claim_candidates(
                rows,
                max_attempts_by_task=max_attempts_by_task,
                prepared_ready_task_attempts=prepared_ready_task_attempts,
                blocked_keys=blocked_keys,
            )
            row = _first_claimable_row(rows, max_attempts_by_task=max_attempts_by_task, now=now)
            if row is not None:
                return row

    def _filter_claim_candidates(
        self,
        rows: list[sqlite3.Row],
        *,
        max_attempts_by_task: dict[str, int] | None,
        prepared_ready_task_attempts: set[tuple[str, int]] | None,
        blocked_keys: set[tuple[str, int]],
    ) -> list[sqlite3.Row]:
        candidates = rows
        if prepared_ready_task_attempts is not None:
            candidates = [
                row
                for row in candidates
                if _row_needs_controller_reconciliation(row, max_attempts_by_task=max_attempts_by_task)
                or (str(row["task_id"]), int(row["attempts"] or 0) + 1) in prepared_ready_task_attempts
            ]
        if blocked_keys:
            candidates = [
                row for row in candidates if (str(row["task_id"]), int(row["attempts"] or 0) + 1) not in blocked_keys
            ]
        return candidates

    def _claim_candidate_rows(
        self,
        *,
        now: float,
        limit: int,
        after_order_index: int,
        stats: dict[str, int | bool] | None = None,
    ) -> list[sqlite3.Row]:
        if stats is not None:
            stats["scan_chunks"] = int(stats.get("scan_chunks", 0)) + 1
            stats["last_order_index"] = after_order_index
        rows = self._connection.execute(
            """
            SELECT {columns}
            FROM batch_tasks INDEXED BY idx_batch_tasks_claim_order
            WHERE cancel_requested=0
              AND order_index > ?
              AND status IN ({placeholders})
              AND (
                lease_token=''
                OR lease_expires_at <= ?
              )
            ORDER BY order_index ASC
            LIMIT ?
            """.format(
                columns=_CLAIM_ROW_COLUMNS,
                placeholders=",".join("?" for _ in _CLAIMABLE_STATUSES),
            ),
            (after_order_index, *sorted(_CLAIMABLE_STATUSES), now, limit),
        ).fetchall()
        if stats is not None:
            stats["scan_rows"] = int(stats.get("scan_rows", 0)) + len(rows)
        return rows

    def _claim_candidate_rows_by_statuses(
        self,
        *,
        statuses: set[str],
        now: float,
        limit: int,
        stats: dict[str, int | bool] | None = None,
    ) -> list[sqlite3.Row]:
        if not statuses:
            return []
        if stats is not None:
            stats["priority_status_lookups"] = int(stats.get("priority_status_lookups", 0)) + 1
        return self._connection.execute(
            """
            SELECT {columns}
            FROM batch_tasks INDEXED BY idx_batch_tasks_claim_order
            WHERE cancel_requested=0
              AND status IN ({placeholders})
              AND controller_task_id<>''
              AND (
                lease_token=''
                OR lease_expires_at <= ?
              )
            ORDER BY order_index ASC
            LIMIT ?
            """.format(
                columns=_CLAIM_ROW_COLUMNS,
                placeholders=",".join("?" for _ in statuses),
            ),
            (*sorted(statuses), now, limit),
        ).fetchall()

    def _claim_fresh_pending_rows(
        self,
        *,
        now: float,
        limit: int,
        after_order_index: int,
        stats: dict[str, int | bool] | None = None,
    ) -> list[sqlite3.Row]:
        if stats is not None:
            stats["fresh_pending_lookup"] = True
            stats["fresh_pending_scan_chunks"] = int(stats.get("fresh_pending_scan_chunks", 0)) + 1
        return self._connection.execute(
            """
            SELECT {columns}
            FROM batch_tasks INDEXED BY idx_batch_tasks_claim_order
            WHERE cancel_requested=0
              AND status=?
              AND attempts=0
              AND order_index > ?
              AND (
                lease_token=''
                OR lease_expires_at <= ?
              )
            ORDER BY order_index ASC
            LIMIT ?
            """.format(columns=_CLAIM_ROW_COLUMNS),
            (BatchTaskStatus.PENDING.value, after_order_index, now, limit),
        ).fetchall()

    def _claim_candidate_rows_for_task_ids(
        self,
        *,
        task_ids: list[str],
        now: float,
        stats: dict[str, int | bool] | None = None,
    ) -> list[sqlite3.Row]:
        if not task_ids:
            return []
        if stats is not None:
            stats["prepared_task_id_lookup"] = True
            stats["prepared_task_ids"] = len(task_ids)
        rows: list[sqlite3.Row] = []
        for task_id in task_ids:
            row = self._connection.execute(
                """
                SELECT {columns}
                FROM batch_tasks
                WHERE task_id=?
                  AND cancel_requested=0
                  AND status IN ({status_placeholders})
                  AND (
                    lease_token=''
                    OR lease_expires_at <= ?
                  )
                """.format(
                    columns=_CLAIM_ROW_COLUMNS,
                    status_placeholders=",".join("?" for _ in _CLAIMABLE_STATUSES),
                ),
                (task_id, *sorted(_CLAIMABLE_STATUSES), now),
            ).fetchone()
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda row: int(row["order_index"]))
        if stats is not None:
            stats["prepared_lookup_rows"] = len(rows)
        return rows

    def _maybe_log_claim_latency(
        self,
        operation: str,
        *,
        started_at: float,
        selected: bool,
        stats: dict[str, int | bool],
    ) -> None:
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds < _CLAIM_SLOW_LOG_SECONDS:
            return
        now = time.monotonic()
        if (
            _CLAIM_SLOW_LOG_INTERVAL_SECONDS > 0
            and now - self._last_claim_slow_log_at < _CLAIM_SLOW_LOG_INTERVAL_SECONDS
        ):
            return
        self._last_claim_slow_log_at = now
        logger.warning(
            "batch SQLite claim path slow",
            extra={
                "operation": operation,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "selected": selected,
                "claim_scan_limit": _CLAIM_SCAN_LIMIT,
                **stats,
            },
        )


def _state_from_row(row: sqlite3.Row) -> BatchTaskState:
    """Return serialized state with retry projection columns applied."""

    state = _state_from_json_text(str(row["state_json"]), fallback_task_id=str(row["task_id"] or ""))
    return state.model_copy(
        update={
            "retry_budget_remaining": int(row["retry_budget_remaining"] or 0),
            "next_attempt_after": float(row["next_attempt_after"] or 0.0),
        }
    )


def _claim_priority(
    row: sqlite3.Row,
    *,
    max_attempts_by_task: dict[str, int] | None,
) -> int:
    if _row_needs_controller_reconciliation(row, max_attempts_by_task=max_attempts_by_task):
        return 0
    if str(row["status"]) == BatchTaskStatus.PENDING.value and int(row["attempts"] or 0) == 0:
        return 1
    return 2


def _prepared_reserved_task_attempts(
    connection: sqlite3.Connection,
    *,
    lease_owner: str,
    now: float,
    stale_after_seconds: float,
) -> set[tuple[str, int]]:
    if not _table_exists(connection, "prepared_sandboxes"):
        return set()
    stale_cutoff = now - stale_after_seconds if stale_after_seconds > 0 else 0
    rows = connection.execute(
        """
        SELECT DISTINCT task_id, attempt
        FROM prepared_sandboxes
        WHERE runner_id!=? AND status IN ({placeholders}) AND expires_at>? AND updated_at>?
        """.format(placeholders=",".join("?" for _ in _ACTIVE_PREPARED_STATUSES)),
        (lease_owner, *sorted(_ACTIVE_PREPARED_STATUSES), now, stale_cutoff),
    ).fetchall()
    return {(str(row["task_id"]), int(row["attempt"])) for row in rows}


def _prepared_owned_task_attempt_ranks(
    connection: sqlite3.Connection,
    *,
    lease_owner: str,
    now: float,
) -> dict[tuple[str, int], int]:
    """Return current-runner prepared task attempts ranked by readiness.

    Ready rows are preferred over in-flight rows so a full prepared pool is
    consumed before generic runnable rows trigger normal sandbox creation.
    """

    if not _table_exists(connection, "prepared_sandboxes"):
        return {}
    status_rank = {"prepared": 0, "preparing": 1, "reserved": 2}
    rows = connection.execute(
        """
        SELECT p.task_id, p.attempt, p.status
        FROM prepared_sandboxes p
        JOIN batch_tasks t ON t.task_id=p.task_id
        WHERE p.runner_id=?
          AND p.status IN ({placeholders})
          AND p.expires_at>?
          AND p.attempt=CAST(t.attempts AS INTEGER)+1
        ORDER BY t.order_index ASC
        """.format(placeholders=",".join("?" for _ in _ACTIVE_PREPARED_STATUSES)),
        (lease_owner, *sorted(_ACTIVE_PREPARED_STATUSES), now),
    ).fetchall()
    ranks: dict[tuple[str, int], int] = {}
    for row in rows:
        key = (str(row["task_id"]), int(row["attempt"]))
        ranks[key] = min(ranks.get(key, 99), status_rank.get(str(row["status"]), 99))
    return ranks


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _default_control_actor() -> str:
    for key in ("NORTHCUA_OPERATOR", "USER", "LOGNAME"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:
        logger.debug("batch control actor lookup failed", exc_info=True)
        return "unknown"
