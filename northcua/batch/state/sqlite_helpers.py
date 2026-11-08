"""SQLite schema and row helpers for batch state storage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.batch.retry_policy import terminal_decision_blocks_retry
from northcua.reliability import FailureAction

_FAILURE_ACTION_VALUES = {action.value for action in FailureAction}


class _SQLiteTransaction:
    """Small context manager that runs a BEGIN IMMEDIATE transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS batch_tasks (
            task_id TEXT PRIMARY KEY,
            order_index INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            controller_task_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sandbox_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            terminal_state TEXT,
            error TEXT NOT NULL,
            recovery_action TEXT,
            last_failure_category TEXT,
            last_reconciled_at TEXT NOT NULL DEFAULT '',
            last_reconciled_status TEXT NOT NULL DEFAULT '',
            cleanup_status TEXT NOT NULL DEFAULT '',
            retry_budget_remaining INTEGER NOT NULL DEFAULT 0,
            next_attempt_after REAL NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 1,
            lease_owner TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            lease_expires_at REAL NOT NULL,
            generation INTEGER NOT NULL,
            cancel_requested INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """)
    _ensure_task_projection_columns(connection)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_batch_tasks_claim ON batch_tasks(status, order_index)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_claim_order ON batch_tasks(cancel_requested, order_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_maintenance_status_lease "
        "ON batch_tasks(status, lease_expires_at, order_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_controller_task_id ON batch_tasks(controller_task_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_status_updated ON batch_tasks(status, updated_at, order_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_maintenance_unleased_empty "
        "ON batch_tasks(status, order_index) WHERE lease_token=''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_maintenance_unleased_expired "
        "ON batch_tasks(status, lease_expires_at, order_index) WHERE lease_token<>''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_empty_lease "
        "ON batch_tasks(status, controller_task_id, order_index) "
        "WHERE controller_task_id<>'' AND lease_token=''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_expired_lease "
        "ON batch_tasks(status, controller_task_id, lease_expires_at, order_index) "
        "WHERE controller_task_id<>'' AND lease_token<>''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_live_lease "
        "ON batch_tasks(status, lease_expires_at, order_index) "
        "WHERE lease_token<>''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_active_controller_live_lease "
        "ON batch_tasks(status, controller_task_id, lease_expires_at, order_index) "
        "WHERE controller_task_id<>'' AND lease_token<>''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history "
        "ON batch_tasks(status, attempts, controller_task_id, order_index) "
        "WHERE status='pending' AND (controller_task_id<>'' OR attempts>0)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history_empty_id "
        "ON batch_tasks(status, attempts, order_index) "
        "WHERE status='pending' AND controller_task_id='' AND attempts>0"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_history_live_lease "
        "ON batch_tasks(status, attempts, lease_expires_at, order_index) "
        "WHERE status='pending' AND controller_task_id='' AND attempts>0 AND lease_token<>''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_tasks_pre_resume_pending_live_lease "
        "ON batch_tasks(status, lease_expires_at, order_index) "
        "WHERE status='pending' AND controller_task_id='' AND lease_token<>''"
    )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS batch_control (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """)
    _ensure_batch_control_columns(connection)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS batch_control_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """)


def _ensure_task_projection_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute("PRAGMA table_info(batch_tasks)").fetchall()
    }
    columns = {
        "recovery_action": "TEXT",
        "last_failure_category": "TEXT",
        "last_reconciled_at": "TEXT NOT NULL DEFAULT ''",
        "last_reconciled_status": "TEXT NOT NULL DEFAULT ''",
        "cleanup_status": "TEXT NOT NULL DEFAULT ''",
        "retry_budget_remaining": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_after": "REAL NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE batch_tasks ADD COLUMN {name} {definition}")


def _ensure_batch_control_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute("PRAGMA table_info(batch_control)").fetchall()
    }
    columns = {
        "updated_by": "TEXT NOT NULL DEFAULT ''",
        "reason": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE batch_control ADD COLUMN {name} {definition}")


def _control_flags(connection: sqlite3.Connection) -> dict[str, bool]:
    rows = connection.execute("SELECT name, value FROM batch_control").fetchall()
    return {str(row["name"]): str(row["value"]) == "1" for row in rows}


def _load_legacy_states(path: Path) -> dict[str, BatchTaskState]:
    states: dict[str, BatchTaskState] = {}
    if not path.exists():
        return states
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        state = _state_from_json_text(line)
        states[state.task_id] = state
    return states


def _valid_recovery_action(value: object) -> str | None:
    """Return a persisted recovery action only when this code can parse it.

    Older recovery tooling wrote transient action labels that were never part
    of the public ``FailureAction`` enum. Production maintenance must tolerate
    those rows and continue converging resume state instead of failing while
    decoding historical bookkeeping.
    """

    if value is None or value == "":
        return None
    raw = value.value if isinstance(value, FailureAction) else str(value)
    return raw if raw in _FAILURE_ACTION_VALUES else None


def _valid_recovery_action_from_values(*values: object) -> str | None:
    """Return the first parseable recovery action from newest to oldest value."""

    for value in values:
        action = _valid_recovery_action(value)
        if action is not None:
            return action
    return None


def _sanitized_state_payload(payload: object, *, fallback_task_id: str = "") -> dict[str, object]:
    """Return a task-state payload with legacy recovery actions ignored."""

    if not isinstance(payload, dict):
        sanitized: dict[str, object] = {"task_id": fallback_task_id}
    else:
        sanitized = dict(payload)
        if fallback_task_id and not sanitized.get("task_id"):
            sanitized["task_id"] = fallback_task_id
    if "recovery_action" in sanitized:
        sanitized["recovery_action"] = _valid_recovery_action(sanitized.get("recovery_action"))
    if "terminal_decision" in sanitized and not isinstance(sanitized.get("terminal_decision"), dict):
        sanitized["terminal_decision"] = {}
    if "post_run_results" in sanitized and not isinstance(sanitized.get("post_run_results"), list):
        sanitized["post_run_results"] = []
    if "reconcile_unknown_count" in sanitized:
        sanitized["reconcile_unknown_count"] = _int_payload_value(
            sanitized.get("reconcile_unknown_count"),
            default=0,
        )
    return sanitized


def _state_from_json_text(state_json: str, *, fallback_task_id: str = "") -> BatchTaskState:
    """Decode persisted task JSON while tolerating legacy recovery actions."""

    try:
        payload = json.loads(state_json or "{}")
    except json.JSONDecodeError:
        payload = {"task_id": fallback_task_id}
    return BatchTaskState.model_validate(_sanitized_state_payload(payload, fallback_task_id=fallback_task_id))


def _int_payload_value(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _first_claimable_row(
    rows: list[sqlite3.Row],
    *,
    max_attempts_by_task: dict[str, int] | None,
    now: float = 0.0,
) -> sqlite3.Row | None:
    """Return the first row still within its retry budget.

    Args:
        rows: Candidate rows already filtered by status and lease ownership.
        max_attempts_by_task: Optional total-attempt budget per task.

    Returns:
        First claimable row, or ``None`` when all failed/timed-out rows have
        exhausted their retry budget.
    """

    budgets = max_attempts_by_task or {}
    for row in rows:
        status = str(row["status"])
        if status == BatchTaskStatus.PENDING.value:
            if _row_has_retry_budget(row, max_attempts_by_task=budgets):
                return row
            continue
        if _row_claim_delayed(row, now=now):
            continue
        if _row_needs_controller_reconciliation(row, max_attempts_by_task=budgets):
            return row
        if _row_blocks_retry(row):
            continue
        if _row_needs_local_recovery(row):
            return row
        if _row_has_retry_budget(row, max_attempts_by_task=budgets):
            return row
    return None


def _next_retry_after(
    rows: list[sqlite3.Row],
    *,
    max_attempts_by_task: dict[str, int] | None,
    now: float,
) -> float | None:
    """Return the earliest delayed retry timestamp among runnable rows."""

    budgets = max_attempts_by_task or {}
    candidates: list[float] = []
    for row in rows:
        status = str(row["status"])
        if status == BatchTaskStatus.PENDING.value:
            continue
        next_attempt_after = float(row["next_attempt_after"] or 0.0)
        if next_attempt_after <= now:
            continue
        if _row_has_active_controller_reconciliation(row):
            candidates.append(next_attempt_after)
            continue
        if _row_blocks_retry(row):
            continue
        if _row_has_retry_budget(row, max_attempts_by_task=budgets):
            candidates.append(next_attempt_after)
    return min(candidates) if candidates else None


def _row_claim_delayed(row: sqlite3.Row, *, now: float) -> bool:
    if now <= 0:
        return False
    status = str(row["status"])
    if status == BatchTaskStatus.PENDING.value:
        return False
    return float(row["next_attempt_after"] or 0.0) > now


def _row_needs_controller_reconciliation(
    row: sqlite3.Row,
    *,
    max_attempts_by_task: dict[str, int] | None = None,
) -> bool:
    if _row_has_active_controller_reconciliation(row):
        return True
    status = str(row["status"])
    if status in {BatchTaskStatus.FAILED.value, BatchTaskStatus.TIMED_OUT.value}:
        return (
            bool(str(row["controller_task_id"] or ""))
            and str(row["last_reconciled_status"] or "") == "unknown"
            and not _row_blocks_retry(row)
            and _row_has_retry_budget(row, max_attempts_by_task=max_attempts_by_task or {})
        )
    return False


def _row_has_active_controller_reconciliation(row: sqlite3.Row) -> bool:
    status = str(row["status"])
    return status in {
        BatchTaskStatus.SUBMITTED.value,
        BatchTaskStatus.RUNNING.value,
    } and bool(str(row["controller_task_id"] or ""))


def _row_has_retry_budget(
    row: sqlite3.Row,
    *,
    max_attempts_by_task: dict[str, int] | None,
) -> bool:
    budgets = max_attempts_by_task or {}
    budget = budgets.get(str(row["task_id"]))
    if budget is None and "max_attempts" in row.keys():
        budget = int(row["max_attempts"] or 0) or None
    return budget is None or int(row["attempts"] or 0) < budget


def _row_needs_local_recovery(row: sqlite3.Row) -> bool:
    """Return whether an in-flight row has no controller task to reconcile.

    Such rows can exhaust their attempt budget before controller submission
    exists. Reclaiming them lets a new runner clean stale leases, emit final
    telemetry, or retry deterministic setup instead of leaving the case
    permanently incomplete.
    """

    status = str(row["status"])
    if status not in {
        BatchTaskStatus.STARTING_SANDBOX.value,
        BatchTaskStatus.BOOTSTRAPPING_DEVICE.value,
        BatchTaskStatus.WAITING_DEVICE.value,
        BatchTaskStatus.SUBMITTED.value,
        BatchTaskStatus.RUNNING.value,
    }:
        return False
    return not bool(str(row["controller_task_id"] or ""))


def _row_blocks_retry(row: sqlite3.Row) -> bool:
    """Return whether persisted task state explicitly forbids retry.

    Args:
        row: SQLite task row containing serialized ``BatchTaskState``.

    Returns:
        ``True`` when recovery action or terminal decision marks the task as a
        non-retryable terminal failure.
    """

    try:
        payload = json.loads(str(row["state_json"] or "{}"))
    except json.JSONDecodeError:
        return False
    return terminal_decision_blocks_retry(
        status=str(row["status"]),
        terminal_decision=payload.get("terminal_decision"),
        last_failure_category=payload.get("last_failure_category") or row["last_failure_category"],
        recovery_action=_valid_recovery_action_from_values(payload.get("recovery_action"), row["recovery_action"]),
    )


def _state_row_values(state: BatchTaskState, *, order_index: int, updated_at: float) -> tuple[object, ...]:
    terminal_state = state.terminal_state.value if state.terminal_state is not None else None
    recovery_action = state.recovery_action.value if state.recovery_action is not None else None
    last_failure_category = state.last_failure_category.value if state.last_failure_category is not None else None
    return (
        state.task_id,
        order_index,
        state.model_dump_json(),
        state.status.value,
        state.attempts,
        state.controller_task_id,
        state.trace_id,
        state.session_id,
        state.sandbox_id,
        state.device_id,
        terminal_state,
        state.error,
        recovery_action,
        last_failure_category,
        state.last_reconciled_at,
        state.last_reconciled_status,
        state.cleanup_status,
        state.retry_budget_remaining,
        state.next_attempt_after,
        updated_at,
    )


def _state_update_values(state: BatchTaskState, *, updated_at: float) -> tuple[object, ...]:
    terminal_state = state.terminal_state.value if state.terminal_state is not None else None
    recovery_action = state.recovery_action.value if state.recovery_action is not None else None
    last_failure_category = state.last_failure_category.value if state.last_failure_category is not None else None
    return (
        state.model_dump_json(),
        state.status.value,
        state.attempts,
        state.controller_task_id,
        state.trace_id,
        state.session_id,
        state.sandbox_id,
        state.device_id,
        terminal_state,
        state.error,
        recovery_action,
        last_failure_category,
        state.last_reconciled_at,
        state.last_reconciled_status,
        state.cleanup_status,
        state.retry_budget_remaining,
        state.next_attempt_after,
        updated_at,
    )
