"""Controller status, reconcile, and cancel helpers for maintenance."""

from __future__ import annotations

import logging
from pathlib import Path

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.batch.controller_db_status import LocalControllerDBStatusReader
from northcua.batch.evaluator_outcome import (
    evaluator_accepts_uncontrollable_result,
    evaluator_is_pending_from_terminal,
    evaluator_report_is_present,
    evaluator_terminal_metadata,
    evaluator_verdict_is_success,
)
from northcua.batch.retry_policy import is_infra_retryable_failure, terminal_decision_blocks_retry
from northcua.batch.runner.controller import ControllerTaskWatcher
from northcua.reliability import FailureAction
from northcua.telemetry.contracts import FailureCategory, utc_now_iso
from northcua.telemetry.failure_categories import failure_category_from_message, failure_category_from_terminal_decision
from northcua_protocol.payloads import TaskCancel, TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)

_TERMINAL_CONTROLLER_STATES = {
    TaskState.FINISHED,
    TaskState.FAILED,
    TaskState.INTERRUPTED,
}
_RESIDUE_CLEANUP_INTERRUPTED_ERROR = "controller residue cleanup interrupted stale task"


class ControllerMaintenanceClient:
    """Thin facade over controller watcher calls used by maintenance tools."""

    def __init__(self, watcher: ControllerTaskWatcher | None = None) -> None:
        self._watcher = watcher or ControllerTaskWatcher()

    async def statuses(
        self,
        *,
        controller_url: str,
        token: str,
        task_ids: list[str],
        controller_db_path: Path | None = None,
    ) -> list[TaskStatusSnapshot]:
        """Fetch explicit controller task snapshots.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter read token.
            task_ids: Controller task ids to query.

        Returns:
            Snapshots for tasks currently known by the controller.
        """

        if not task_ids:
            return []
        db_snapshots = LocalControllerDBStatusReader(controller_db_path).statuses(task_ids)
        if db_snapshots is not None:
            return db_snapshots
        return await self._watcher.statuses(controller_url=controller_url, token=token, task_ids=task_ids)

    async def list_batch(
        self,
        *,
        controller_url: str,
        token: str,
        batch_id: str,
        limit: int = 0,
        controller_db_path: Path | None = None,
    ) -> list[TaskStatusSnapshot]:
        """Fetch controller snapshots tagged with the batch id.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter read token.
            batch_id: Batch metadata value to filter on.
            limit: Maximum number of controller rows to return.

        Returns:
            Controller task snapshots associated with the batch.
        """

        db_snapshots = LocalControllerDBStatusReader(controller_db_path).list_batch(batch_id=batch_id, limit=limit)
        if db_snapshots is not None:
            return db_snapshots
        return await self._watcher.list_batch(
            controller_url=controller_url, token=token, batch_id=batch_id, limit=limit
        )

    async def cancel(
        self,
        *,
        controller_url: str,
        token: str,
        state: BatchTaskState,
        reason: str,
    ) -> None:
        """Send a best-effort controller cancel for one submitted task.

        Args:
            controller_url: Controller websocket URL.
            token: Submitter token retained for authenticated status/cancel
                paths.
            state: Local state containing controller ids.
            reason: Human-readable cancellation reason.
        """

        if not state.controller_task_id:
            return
        await self._watcher.cancel(
            controller_url=controller_url,
            token=token,
            task=TaskCancel(
                task_id=state.controller_task_id,
                trace_id=state.trace_id,
                session_id=state.session_id,
                reason=reason,
            ),
        )

    @staticmethod
    def state_from_snapshot(existing: BatchTaskState, snapshot: TaskStatusSnapshot) -> BatchTaskState:
        """Project a controller snapshot into the local batch state schema.

        Args:
            existing: Current SQLite task state.
            snapshot: Controller status snapshot to project.

        Returns:
            Updated task state with terminal status, failure category, and trace
            identifiers preserved.
        """

        terminal_decision = snapshot.terminal_decision
        evaluator_report_present = evaluator_report_is_present(snapshot.result)
        evaluator_success = evaluator_verdict_is_success(snapshot.result)
        evaluator_accepted_infeasible = evaluator_accepts_uncontrollable_result(snapshot.result)
        evaluator_pending = evaluator_is_pending_from_terminal(terminal_decision)
        evaluator_metadata = evaluator_terminal_metadata(terminal_decision)
        current_failure_category = failure_category_from_terminal_decision(
            terminal_decision
        ) or failure_category_from_message(snapshot.error)
        if (
            snapshot.state == TaskState.INTERRUPTED
            and current_failure_category is None
            and snapshot.error == _RESIDUE_CLEANUP_INTERRUPTED_ERROR
        ):
            current_failure_category = FailureCategory.CONTROLLER_SUBMIT
        last_failure_category = existing.last_failure_category
        if snapshot.state in {TaskState.FAILED, TaskState.INTERRUPTED}:
            last_failure_category = current_failure_category or (
                FailureCategory.MAIN_AGENT if snapshot.state == TaskState.FAILED else last_failure_category
            )
        if evaluator_success:
            last_failure_category = FailureCategory.ACCEPTED_INFEASIBLE if evaluator_accepted_infeasible else None
        status_failure_category = current_failure_category
        if (
            snapshot.state == TaskState.INTERRUPTED
            and status_failure_category is None
            and existing.last_failure_category == FailureCategory.TIMEOUT
        ):
            status_failure_category = FailureCategory.TIMEOUT
        local_status = _local_status(
            snapshot.state,
            last_failure_category=status_failure_category,
            evaluator_success=evaluator_success,
            evaluator_report_present=evaluator_report_present,
        )
        if evaluator_pending:
            local_status = BatchTaskStatus.EVALUATION_PENDING
            last_failure_category = FailureCategory.EVALUATOR
        non_retryable = terminal_decision_blocks_retry(
            status=local_status,
            terminal_decision=terminal_decision,
            last_failure_category=last_failure_category,
            recovery_action=existing.recovery_action,
        )
        return existing.model_copy(
            update={
                "controller_task_id": snapshot.task_id,
                "trace_id": snapshot.trace_id or existing.trace_id,
                "session_id": snapshot.session_id or existing.session_id,
                "device_id": snapshot.device_id or existing.device_id,
                "status": local_status,
                "terminal_state": snapshot.state if snapshot.state in _TERMINAL_CONTROLLER_STATES else None,
                "error": (
                    ""
                    if local_status in {BatchTaskStatus.SUCCEEDED, BatchTaskStatus.EVALUATION_PENDING}
                    else snapshot.error
                ),
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": snapshot.state.value,
                "last_failure_category": last_failure_category,
                "recovery_action": FailureAction.FAIL_FAST if non_retryable else None,
                "next_attempt_after": 0.0,
                "reconcile_unknown_count": 0,
                "terminal_decision": terminal_decision,
                "agent_terminal": _agent_terminal_from_snapshot(snapshot),
                "evaluation_status": str(evaluator_metadata.get("status") or ""),
                "evaluation_error": evaluator_metadata,
                "evaluation_attempts": _int_value(evaluator_metadata.get("attempts")),
                "evaluation_projection_level": str(evaluator_metadata.get("projection_level") or ""),
            }
        )


def is_terminal_controller_state(state: TaskState) -> bool:
    """Return whether a controller state is terminal.

    Args:
        state: Controller task lifecycle state.

    Returns:
        ``True`` when the controller has emitted a terminal task state.
    """

    return state in _TERMINAL_CONTROLLER_STATES


def _local_status(
    controller_state: TaskState,
    *,
    last_failure_category: FailureCategory | None = None,
    evaluator_success: bool = False,
    evaluator_report_present: bool = False,
) -> BatchTaskStatus:
    if evaluator_success:
        return BatchTaskStatus.SUCCEEDED
    if evaluator_report_present:
        return BatchTaskStatus.FAILED
    if controller_state == TaskState.FINISHED:
        return BatchTaskStatus.SUCCEEDED
    if controller_state == TaskState.INTERRUPTED:
        if last_failure_category == FailureCategory.TIMEOUT:
            return BatchTaskStatus.TIMED_OUT
        if is_infra_retryable_failure(last_failure_category):
            return BatchTaskStatus.FAILED
        return BatchTaskStatus.CANCELED
    if controller_state == TaskState.FAILED:
        return BatchTaskStatus.FAILED
    return BatchTaskStatus.RUNNING


def _agent_terminal_from_snapshot(snapshot: TaskStatusSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "result": str(snapshot.result or "")[:1000],
        "terminal_decision": snapshot.terminal_decision,
    }


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
