"""Cheap repair for stale batch active rows during resume."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig, BatchTaskState, BatchTaskStatus
from northcua.batch.controller_db_status import LocalControllerDBStatusReader
from northcua.batch.runner.resume import (
    _unknown_reconcile_backoff_seconds,
    reconciled_state_from_controller_snapshot,
)
from northcua.batch.state import BatchStateRepository, LeasePolicy
from northcua.batch.state.sqlite_helpers import _state_from_json_text, _state_update_values
from northcua.reliability import FailureAction
from northcua.telemetry.contracts import FailureCategory, utc_now_iso
from northcua_protocol.payloads import TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)

_ACTIVE_BATCH_STATUSES = {
    BatchTaskStatus.STARTING_SANDBOX.value,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE.value,
    BatchTaskStatus.WAITING_DEVICE.value,
    BatchTaskStatus.SUBMITTED.value,
    BatchTaskStatus.RUNNING.value,
}
_TERMINAL_CONTROLLER_STATES = {
    TaskState.FINISHED,
    TaskState.FAILED,
    TaskState.INTERRUPTED,
}
_DEFAULT_STALE_ACTIVE_REPAIR_INTERVAL_SECONDS = 30.0
_DEFAULT_STALE_ACTIVE_REPAIR_LIMIT = 500
_DEFAULT_FOREIGN_LIVE_LOCAL_SETUP_STALE_GRACE_SECONDS = 60.0


@dataclass(frozen=True)
class StaleActiveRepairResult:
    """Summary of one stale-active repair pass.

    Attributes:
        controller_terminal: Rows repaired from authoritative terminal
            controller snapshots.
        no_controller_id_expired: Rows without controller ids whose local lease
            had expired or disappeared.
        foreign_live_no_controller_id_stale: Rows owned by another runner that
            never submitted to controller and stopped heartbeating.
        missing_controller_task: Rows whose controller task id is absent from a
            readable controller DB and no live lease protects them.
        skipped_current_owner: Rows retained because the current runner still
            owns them.
        skipped_live_lease: Rows retained because a live lease still protects
            the attempt.
        update_conflicts: Rows skipped after optimistic update conflicts.
    """

    controller_terminal: int = 0
    no_controller_id_expired: int = 0
    foreign_live_no_controller_id_stale: int = 0
    missing_controller_task: int = 0
    skipped_current_owner: int = 0
    skipped_live_lease: int = 0
    update_conflicts: int = 0

    @property
    def repaired(self) -> int:
        """Return the number of rows actually converted out of active status.

        Returns:
            Sum of repair counters that update batch state rows.
        """

        return (
            self.controller_terminal
            + self.no_controller_id_expired
            + self.foreign_live_no_controller_id_stale
            + self.missing_controller_task
        )


@dataclass(frozen=True)
class StaleActiveRepairLoop:
    """Handle for a cooperative stale-active repair loop.

    Attributes:
        task: Background task that periodically repairs stale active rows.
        stop_event: Cooperative stop signal checked between repair passes.
    """

    task: asyncio.Task[None]
    stop_event: asyncio.Event

    async def close(self) -> None:
        """Stop after any current SQLite repair pass finishes."""

        self.stop_event.set()
        await self.task


async def repair_stale_active_rows_once(
    *,
    config: BatchRunConfig,
    repository: BatchStateRepository,
    max_attempts_by_task: Mapping[str, int],
    limit: int | None = None,
) -> StaleActiveRepairResult:
    """Repair obvious pseudo-active rows without consuming task workers.

    The repair is deliberately narrow:
    - controller terminal rows use the local controller DB as authority;
    - rows with no controller id are repaired after their local lease expired,
      is absent, or belongs to a different runner that missed a heartbeat;
    - missing controller task ids are treated as unknown only when the local
      controller DB is readable and the batch lease is not live.

    Args:
        config: Batch run config that provides runner id, lease policy, and
            controller DB path.
        repository: Batch state repository whose SQLite path is repaired.
        max_attempts_by_task: Retry budget upper bounds keyed by task id.
        limit: Optional cap for rows inspected in this pass.

    Returns:
        Counters describing which rows were repaired or skipped.
    """

    batch_db_path = getattr(repository, "path", None)
    if not isinstance(batch_db_path, Path):
        return StaleActiveRepairResult()
    repair_limit = limit if limit is not None else _stale_active_repair_limit()
    if repair_limit <= 0:
        return StaleActiveRepairResult()
    return await asyncio.to_thread(
        _repair_stale_active_rows_sync,
        batch_db_path=batch_db_path,
        controller_db_path=config.telemetry_controller_db_path,
        runner_id=config.runner_id,
        lease_timeout_seconds=config.lease_timeout_seconds,
        foreign_live_no_controller_stale_after_seconds=_foreign_live_no_controller_stale_after_seconds(config),
        max_attempts_by_task=dict(max_attempts_by_task),
        infra_retry_backoff_seconds=config.infra_retry_backoff_seconds,
        limit=repair_limit,
    )


def start_stale_active_repair_loop(
    *,
    config: BatchRunConfig,
    repository: BatchStateRepository,
    max_attempts_by_task: Mapping[str, int],
    stop_requested: Callable[[], bool],
) -> StaleActiveRepairLoop | None:
    """Start a low-frequency repair loop when the repository supports it.

    Args:
        config: Batch run config that provides runner id and timing knobs.
        repository: Batch state repository whose SQLite path is repaired.
        max_attempts_by_task: Retry budget upper bounds keyed by task id.
        stop_requested: Caller predicate used to stop before process shutdown.

    Returns:
        Loop handle when repair is enabled and the repository exposes a path,
        otherwise ``None``.
    """

    batch_db_path = getattr(repository, "path", None)
    if not isinstance(batch_db_path, Path):
        return None
    interval = _stale_active_repair_interval_seconds()
    if interval <= 0:
        return None
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _stale_active_repair_loop(
            config=config,
            repository=repository,
            max_attempts_by_task=max_attempts_by_task,
            stop_requested=stop_requested,
            stop_event=stop_event,
            interval_seconds=interval,
        ),
        name="northcua-batch-stale-active-repair",
    )
    return StaleActiveRepairLoop(task=task, stop_event=stop_event)


async def _stale_active_repair_loop(
    *,
    config: BatchRunConfig,
    repository: BatchStateRepository,
    max_attempts_by_task: Mapping[str, int],
    stop_requested: Callable[[], bool],
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while not stop_requested() and not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        if stop_requested() or stop_event.is_set():
            return
        try:
            result = await repair_stale_active_rows_once(
                config=config,
                repository=repository,
                max_attempts_by_task=max_attempts_by_task,
            )
        except Exception:
            logger.warning(
                "batch stale active repair pass failed",
                extra={"batch_id": config.manifest.batch_id, "runner_id": config.runner_id},
                exc_info=True,
            )
            continue
        _log_repair_result(config=config, result=result, periodic=True)


def _repair_stale_active_rows_sync(
    *,
    batch_db_path: Path,
    controller_db_path: Path | None,
    runner_id: str,
    lease_timeout_seconds: float,
    foreign_live_no_controller_stale_after_seconds: float,
    max_attempts_by_task: dict[str, int],
    infra_retry_backoff_seconds: list[float],
    limit: int,
) -> StaleActiveRepairResult:
    now = time.time()
    connection = sqlite3.connect(str(batch_db_path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        rows = connection.execute(
            """
            SELECT *
            FROM batch_tasks
            WHERE cancel_requested=0
              AND status IN ({placeholders})
            ORDER BY updated_at ASC, order_index ASC
            LIMIT ?
            """.format(placeholders=",".join("?" for _ in _ACTIVE_BATCH_STATUSES)),
            (*sorted(_ACTIVE_BATCH_STATUSES), limit),
        ).fetchall()
        if not rows:
            return StaleActiveRepairResult()
        controller_snapshots, controller_db_authoritative = _controller_snapshots_for_rows(
            rows,
            controller_db_path=controller_db_path,
        )
        return _repair_rows(
            connection=connection,
            rows=rows,
            controller_snapshots=controller_snapshots,
            controller_db_authoritative=controller_db_authoritative,
            runner_id=runner_id,
            lease_timeout_seconds=lease_timeout_seconds,
            foreign_live_no_controller_stale_after_seconds=foreign_live_no_controller_stale_after_seconds,
            now=now,
            max_attempts_by_task=max_attempts_by_task,
            infra_retry_backoff_seconds=infra_retry_backoff_seconds,
        )
    finally:
        connection.close()


def _controller_snapshots_for_rows(
    rows: list[sqlite3.Row],
    *,
    controller_db_path: Path | None,
) -> tuple[dict[str, TaskStatusSnapshot], bool]:
    controller_task_ids = sorted({str(row["controller_task_id"] or "") for row in rows if row["controller_task_id"]})
    if not controller_task_ids:
        return {}, False
    reader = LocalControllerDBStatusReader(controller_db_path)
    if not reader.available():
        return {}, False
    snapshots = reader.statuses(controller_task_ids)
    if snapshots is None:
        return {}, False
    return {snapshot.task_id: snapshot for snapshot in snapshots}, True


def _repair_rows(
    *,
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    controller_snapshots: Mapping[str, TaskStatusSnapshot],
    controller_db_authoritative: bool,
    runner_id: str,
    lease_timeout_seconds: float,
    foreign_live_no_controller_stale_after_seconds: float,
    now: float,
    max_attempts_by_task: Mapping[str, int],
    infra_retry_backoff_seconds: list[float],
) -> StaleActiveRepairResult:
    controller_terminal = 0
    no_controller_id_expired = 0
    foreign_live_no_controller_id_stale = 0
    missing_controller_task = 0
    skipped_current_owner = 0
    skipped_live_lease = 0
    update_conflicts = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            state = _state_from_row(row)
            controller_task_id = str(row["controller_task_id"] or "")
            lease_token = str(row["lease_token"] or "")
            lease_owner = str(row["lease_owner"] or "")
            lease_expires_at = float(row["lease_expires_at"] or 0.0)
            lease_expired = not lease_token or lease_expires_at <= now
            live_lease = bool(lease_token) and not lease_expired
            foreign_live_lease_expires_before = (
                now + lease_timeout_seconds - foreign_live_no_controller_stale_after_seconds
            )
            owner_is_current = live_lease and lease_owner == runner_id
            foreign_live_no_controller_stale = (
                live_lease
                and lease_owner != ""
                and lease_owner != runner_id
                and controller_task_id == ""
                and lease_expires_at <= foreign_live_lease_expires_before
            )
            repaired: BatchTaskState | None = None
            clear_lease = False
            foreign_live_owner_to_clear = ""
            foreign_live_lease_expires_at_or_before = 0.0
            kind = ""
            if controller_task_id:
                snapshot = controller_snapshots.get(controller_task_id)
                if snapshot is not None and snapshot.state in _TERMINAL_CONTROLLER_STATES:
                    if live_lease:
                        if owner_is_current:
                            skipped_current_owner += 1
                        else:
                            skipped_live_lease += 1
                        continue
                    repaired = reconciled_state_from_controller_snapshot(state, snapshot)
                    clear_lease = True
                    kind = "controller_terminal"
                elif snapshot is None and controller_db_authoritative and lease_expired:
                    repaired = _missing_controller_task_state(
                        state,
                        max_attempts_by_task=max_attempts_by_task,
                        infra_retry_backoff_seconds=infra_retry_backoff_seconds,
                        now=now,
                    )
                    clear_lease = True
                    kind = "missing_controller_task"
                elif owner_is_current:
                    skipped_current_owner += 1
                elif live_lease:
                    skipped_live_lease += 1
            elif lease_expired:
                repaired = _no_controller_id_stale_state(
                    state,
                    max_attempts_by_task=max_attempts_by_task,
                    reason="expired active batch lease had no controller task id; released stale local work",
                )
                clear_lease = True
                kind = "no_controller_id_expired"
            elif foreign_live_no_controller_stale:
                repaired = _no_controller_id_stale_state(
                    state,
                    max_attempts_by_task=max_attempts_by_task,
                    reason=(
                        "foreign batch lease missed heartbeat and had no controller task id; "
                        "released stale local setup work"
                    ),
                )
                clear_lease = True
                foreign_live_owner_to_clear = lease_owner
                foreign_live_lease_expires_at_or_before = foreign_live_lease_expires_before
                kind = "foreign_live_no_controller_id_stale"
            elif owner_is_current:
                skipped_current_owner += 1
            elif live_lease:
                skipped_live_lease += 1
            if repaired is None:
                continue
            if not _cas_update(
                connection,
                row=row,
                state=repaired,
                now=now,
                clear_lease=clear_lease,
                foreign_live_owner_to_clear=foreign_live_owner_to_clear,
                foreign_live_lease_expires_at_or_before=foreign_live_lease_expires_at_or_before,
            ):
                update_conflicts += 1
                continue
            if kind == "controller_terminal":
                controller_terminal += 1
            elif kind == "missing_controller_task":
                missing_controller_task += 1
            elif kind == "no_controller_id_expired":
                no_controller_id_expired += 1
            elif kind == "foreign_live_no_controller_id_stale":
                foreign_live_no_controller_id_stale += 1
        connection.commit()
    except Exception:
        logger.warning(
            "stale active repair pass failed; rolling back batch state updates",
            extra={"runner_id": runner_id, "row_count": len(rows)},
            exc_info=True,
        )
        connection.rollback()
        raise
    return StaleActiveRepairResult(
        controller_terminal=controller_terminal,
        no_controller_id_expired=no_controller_id_expired,
        foreign_live_no_controller_id_stale=foreign_live_no_controller_id_stale,
        missing_controller_task=missing_controller_task,
        skipped_current_owner=skipped_current_owner,
        skipped_live_lease=skipped_live_lease,
        update_conflicts=update_conflicts,
    )


def _state_from_row(row: sqlite3.Row) -> BatchTaskState:
    state = _state_from_json_text(str(row["state_json"]), fallback_task_id=str(row["task_id"] or ""))
    return state.model_copy(
        update={
            "retry_budget_remaining": int(row["retry_budget_remaining"] or 0),
            "next_attempt_after": float(row["next_attempt_after"] or 0.0),
        }
    )


def _no_controller_id_stale_state(
    state: BatchTaskState,
    *,
    max_attempts_by_task: Mapping[str, int],
    reason: str,
) -> BatchTaskState:
    failure_category = _failure_category_for_stale_local_status(state.status)
    budget = max_attempts_by_task.get(state.task_id)
    retry_budget_exhausted = budget is not None and state.attempts >= budget
    error = reason
    retry_budget_remaining = state.retry_budget_remaining
    if retry_budget_exhausted:
        error = f"local setup attempt budget exhausted while recovering {state.status.value} after {budget} attempt(s)"
        retry_budget_remaining = 0
    return state.model_copy(
        update={
            "status": BatchTaskStatus.FAILED,
            "error": error,
            "last_failure_category": failure_category,
            "last_reconciled_at": utc_now_iso(),
            "last_reconciled_status": "local_stale_reset",
            "retry_budget_remaining": retry_budget_remaining,
            "next_attempt_after": 0.0,
        }
    )


def _missing_controller_task_state(
    state: BatchTaskState,
    *,
    max_attempts_by_task: Mapping[str, int],
    infra_retry_backoff_seconds: list[float],
    now: float,
) -> BatchTaskState:
    count = state.reconcile_unknown_count + 1
    budget = max_attempts_by_task.get(state.task_id)
    retry_budget_available = budget is None or state.attempts < budget
    if retry_budget_available:
        delay = _unknown_reconcile_backoff_seconds(infra_retry_backoff_seconds, count)
        return state.model_copy(
            update={
                "status": BatchTaskStatus.FAILED,
                "error": (
                    "controller task status was not found in local controller DB; "
                    "will retry controller status reconcile before any fresh submit"
                ),
                "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": "unknown",
                "next_attempt_after": now + delay,
                "reconcile_unknown_count": count,
            }
        )
    return state.model_copy(
        update={
            "status": BatchTaskStatus.FAILED,
            "error": "controller task status was not found in local controller DB; retry budget exhausted",
            "recovery_action": FailureAction.FAIL_FAST,
            "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
            "last_reconciled_at": utc_now_iso(),
            "last_reconciled_status": "unknown",
            "reconcile_unknown_count": count,
        }
    )


def _failure_category_for_stale_local_status(status: BatchTaskStatus) -> FailureCategory:
    if status == BatchTaskStatus.STARTING_SANDBOX:
        return FailureCategory.SANDBOX_CREATE
    if status == BatchTaskStatus.BOOTSTRAPPING_DEVICE:
        return FailureCategory.DEVICE_BOOTSTRAP
    if status == BatchTaskStatus.WAITING_DEVICE:
        return FailureCategory.READINESS_TIMEOUT
    if status == BatchTaskStatus.SUBMITTED:
        return FailureCategory.CONTROLLER_SUBMIT
    return FailureCategory.MAIN_AGENT


def _cas_update(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    state: BatchTaskState,
    now: float,
    clear_lease: bool,
    foreign_live_owner_to_clear: str = "",
    foreign_live_lease_expires_at_or_before: float = 0.0,
) -> bool:
    values = _state_update_values(state, updated_at=now)
    if not clear_lease:
        cursor = connection.execute(
            """
            UPDATE batch_tasks
            SET state_json=?, status=?, attempts=?, controller_task_id=?,
                trace_id=?, session_id=?, sandbox_id=?, device_id=?,
                terminal_state=?, error=?, recovery_action=?,
                last_failure_category=?, last_reconciled_at=?,
                last_reconciled_status=?, cleanup_status=?,
                retry_budget_remaining=?, next_attempt_after=?,
                updated_at=?
            WHERE task_id=?
              AND generation=?
              AND lease_token=?
              AND status=?
              AND controller_task_id=?
              AND cancel_requested=0
            """,
            (
                *values,
                state.task_id,
                int(row["generation"]),
                str(row["lease_token"] or ""),
                str(row["status"]),
                str(row["controller_task_id"] or ""),
            ),
        )
        return cursor.rowcount == 1
    cursor = connection.execute(
        """
        UPDATE batch_tasks
        SET state_json=?, status=?, attempts=?, controller_task_id=?,
            trace_id=?, session_id=?, sandbox_id=?, device_id=?,
            terminal_state=?, error=?, recovery_action=?,
            last_failure_category=?, last_reconciled_at=?,
            last_reconciled_status=?, cleanup_status=?,
            retry_budget_remaining=?, next_attempt_after=?,
            lease_owner='', lease_token='', lease_expires_at=0,
            generation=generation+1, updated_at=?
        WHERE task_id=?
          AND generation=?
          AND lease_token=?
          AND (
            lease_token=''
            OR lease_expires_at<=?
            OR (
              ?!=''
              AND lease_owner=?
              AND lease_expires_at>?
              AND lease_expires_at<=?
            )
          )
          AND status=?
          AND controller_task_id=?
          AND cancel_requested=0
        """,
        (
            *values,
            state.task_id,
            int(row["generation"]),
            str(row["lease_token"] or ""),
            now,
            foreign_live_owner_to_clear,
            foreign_live_owner_to_clear,
            now,
            foreign_live_lease_expires_at_or_before,
            str(row["status"]),
            str(row["controller_task_id"] or ""),
        ),
    )
    return cursor.rowcount == 1


def _stale_active_repair_interval_seconds() -> float:
    raw = os.environ.get(
        "NORTHCUA_BATCH_STALE_ACTIVE_REPAIR_INTERVAL_SECONDS",
        str(_DEFAULT_STALE_ACTIVE_REPAIR_INTERVAL_SECONDS),
    )
    try:
        return max(float(raw), 0.0)
    except ValueError:
        logger.warning("invalid stale active repair interval; using default", extra={"value": raw})
        return _DEFAULT_STALE_ACTIVE_REPAIR_INTERVAL_SECONDS


def _stale_active_repair_limit() -> int:
    raw = os.environ.get("NORTHCUA_BATCH_STALE_ACTIVE_REPAIR_LIMIT", str(_DEFAULT_STALE_ACTIVE_REPAIR_LIMIT))
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning("invalid stale active repair limit; using default", extra={"value": raw})
        return _DEFAULT_STALE_ACTIVE_REPAIR_LIMIT


def _foreign_live_no_controller_stale_after_seconds(config: BatchRunConfig) -> float:
    raw = os.environ.get("NORTHCUA_BATCH_FOREIGN_LIVE_LOCAL_SETUP_STALE_SECONDS")
    if raw is not None:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            logger.warning("invalid foreign live local setup stale threshold; using default", extra={"value": raw})
    policy = LeasePolicy.from_timeout(config.lease_timeout_seconds)
    return policy.heartbeat_interval_seconds + _DEFAULT_FOREIGN_LIVE_LOCAL_SETUP_STALE_GRACE_SECONDS


def _log_repair_result(*, config: BatchRunConfig, result: StaleActiveRepairResult, periodic: bool = False) -> None:
    if result.repaired <= 0 and result.update_conflicts <= 0:
        return
    logger.info(
        "batch stale active repair %s: "
        "controller_terminal=%s no_controller_id_expired=%s "
        "foreign_live_no_controller_id_stale=%s missing_controller_task=%s "
        "skipped_current_owner=%s skipped_live_lease=%s update_conflicts=%s",
        "periodic" if periodic else "startup",
        result.controller_terminal,
        result.no_controller_id_expired,
        result.foreign_live_no_controller_id_stale,
        result.missing_controller_task,
        result.skipped_current_owner,
        result.skipped_live_lease,
        result.update_conflicts,
        extra={
            "batch_id": config.manifest.batch_id,
            "runner_id": config.runner_id,
            "controller_terminal": result.controller_terminal,
            "no_controller_id_expired": result.no_controller_id_expired,
            "foreign_live_no_controller_id_stale": result.foreign_live_no_controller_id_stale,
            "missing_controller_task": result.missing_controller_task,
            "skipped_current_owner": result.skipped_current_owner,
            "skipped_live_lease": result.skipped_live_lease,
            "update_conflicts": result.update_conflicts,
            "repaired": result.repaired,
            "periodic": periodic,
        },
    )
