"""Controller-backed batch resume reconciliation."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
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
from northcua_protocol.payloads import TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)

_RESUMABLE_LOCAL_STATES = {
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
    BatchTaskStatus.FAILED,
    BatchTaskStatus.TIMED_OUT,
}
_TERMINAL_CONTROLLER_STATES = {
    TaskState.FINISHED,
    TaskState.FAILED,
    TaskState.INTERRUPTED,
}
_RESIDUE_CLEANUP_INTERRUPTED_ERROR = "controller residue cleanup interrupted stale task"
_UNKNOWN_RECONCILE_CLEAR_AFTER = 3
_MIN_UNKNOWN_RECONCILE_BACKOFF_SECONDS = 1.0
_DEFAULT_UNKNOWN_RECONCILE_BACKOFF_SECONDS = [30.0, 90.0, 180.0, 300.0]
_DB_MISS_WEBSOCKET_FALLBACK_ENV = "NORTHCUA_BATCH_RECONCILE_STATUS_WEBSOCKET_FALLBACK_ON_DB_MISS"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ControllerStatusUnavailable(RuntimeError):
    """Raised when resume should delay controller status reconciliation."""


class BatchStatusReconciler:
    """Reconcile local JSONL state with controller task snapshots."""

    def __init__(self, watcher: ControllerTaskWatcher, *, poll_interval_seconds: float = 1.0) -> None:
        self._watcher = watcher
        self._poll_interval_seconds = poll_interval_seconds

    async def reconcile(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        existing: BatchTaskState,
        status_semaphore: asyncio.Semaphore | None = None,
    ) -> BatchTaskState | None:
        """Return a reconciled task state when controller status is authoritative.

        Args:
            config: Current batch runtime configuration.
            task: Manifest task being considered for resume.
            existing: Latest local state from the JSONL store.

        Returns:
            A reconciled state when the controller can answer for the previous
            submission, otherwise ``None`` so the scheduler may run a new
            attempt.
        """

        if existing.status == BatchTaskStatus.SUCCEEDED:
            return existing
        if existing.status not in _RESUMABLE_LOCAL_STATES or not existing.controller_task_id or config.dry_run:
            return None
        defaults = config.manifest.defaults
        controller_url = config.controller_url or defaults.controller_url
        token = config.token or defaults.token
        try:
            snapshots = await self._statuses(
                config=config,
                controller_url=controller_url,
                token=token,
                task_ids=[existing.controller_task_id],
                status_semaphore=status_semaphore,
            )
        except ControllerStatusUnavailable as exc:
            logger.warning(
                "delaying batch task controller status reconcile",
                extra={
                    "task_id": task.task_id,
                    "controller_task_id": existing.controller_task_id,
                    "reason": str(exc),
                },
            )
            return self._unknown_status(config, task, existing, str(exc), allow_fresh_submit=False)
        except Exception:
            logger.warning(
                "failed to reconcile batch task from controller status",
                extra={"task_id": task.task_id, "controller_task_id": existing.controller_task_id},
                exc_info=True,
            )
            return self._unknown_status(
                config,
                task,
                existing,
                "could not reconcile controller task status",
                allow_fresh_submit=False,
            )
        if not snapshots:
            return self._unknown_status(
                config,
                task,
                existing,
                "controller task status was not found",
                allow_fresh_submit=True,
            )
        snapshot = snapshots[0]
        if snapshot.state not in _TERMINAL_CONTROLLER_STATES:
            return await self._wait_for_terminal_snapshot(
                config=config,
                task=task,
                existing=existing,
                initial=snapshot,
                status_semaphore=status_semaphore,
            )
        return self._from_snapshot(existing, snapshot)

    async def _statuses(
        self,
        *,
        config: BatchRunConfig,
        controller_url: str,
        token: str,
        task_ids: list[str],
        status_semaphore: asyncio.Semaphore | None,
    ) -> list[TaskStatusSnapshot]:
        db_snapshots = await self._local_db_statuses(config=config, task_ids=task_ids)
        if db_snapshots is not None:
            if db_snapshots or not task_ids:
                return db_snapshots
            allow_websocket_fallback = _env_bool(_DB_MISS_WEBSOCKET_FALLBACK_ENV, True)
            logger.warning(
                "local controller DB resume status lookup missed explicit task id",
                extra={
                    "task_count": len(task_ids),
                    "websocket_fallback": allow_websocket_fallback,
                },
            )
            if not allow_websocket_fallback:
                raise ControllerStatusUnavailable("local controller DB status lookup returned no row")
        if status_semaphore is None:
            return await self._watcher.statuses(controller_url=controller_url, token=token, task_ids=task_ids)
        async with status_semaphore:
            return await self._watcher.statuses(controller_url=controller_url, token=token, task_ids=task_ids)

    async def _local_db_statuses(
        self,
        *,
        config: BatchRunConfig,
        task_ids: list[str],
    ) -> list[TaskStatusSnapshot] | None:
        timeout_seconds = _env_float("NORTHCUA_BATCH_RECONCILE_STATUS_DB_TIMEOUT_SECONDS", 5.0)
        busy_timeout_ms = _env_int("NORTHCUA_BATCH_RECONCILE_STATUS_DB_BUSY_TIMEOUT_MS", 1000)
        if timeout_seconds <= 0:
            return None
        reader = LocalControllerDBStatusReader(config.telemetry_controller_db_path, busy_timeout_ms=busy_timeout_ms)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(reader.statuses, task_ids),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            allow_websocket_fallback = _env_bool(
                "NORTHCUA_BATCH_RECONCILE_STATUS_WEBSOCKET_FALLBACK_ON_DB_TIMEOUT", False
            )
            logger.warning(
                "local controller DB resume status lookup timed out",
                extra={
                    "task_count": len(task_ids),
                    "timeout_seconds": timeout_seconds,
                    "websocket_fallback": allow_websocket_fallback,
                },
            )
            if not allow_websocket_fallback:
                raise ControllerStatusUnavailable("local controller DB status lookup timed out") from None
            return None

    async def _wait_for_terminal_snapshot(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        existing: BatchTaskState,
        initial: TaskStatusSnapshot,
        status_semaphore: asyncio.Semaphore | None,
    ) -> BatchTaskState | None:
        defaults = config.manifest.defaults
        controller_url = config.controller_url or defaults.controller_url
        token = config.token or defaults.token
        timeout_seconds = task.timeout_seconds or defaults.timeout_seconds
        deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        snapshot = initial
        while snapshot.state not in _TERMINAL_CONTROLLER_STATES:
            if deadline is not None and time.monotonic() >= deadline:
                return existing.model_copy(
                    update={
                        "status": existing.status,
                        "error": "controller task remained non-terminal during resume; will reconcile later",
                        "last_reconciled_at": utc_now_iso(),
                        "last_reconciled_status": snapshot.state.value,
                        "last_failure_category": existing.last_failure_category,
                        "next_attempt_after": time.time() + 1.0,
                    }
                )
            sleep_seconds = self._poll_interval_seconds
            if deadline is not None:
                sleep_seconds = max(0.0, min(sleep_seconds, deadline - time.monotonic()))
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
            try:
                snapshots = await self._statuses(
                    config=config,
                    controller_url=controller_url,
                    token=token,
                    task_ids=[existing.controller_task_id],
                    status_semaphore=status_semaphore,
                )
            except ControllerStatusUnavailable:
                return existing.model_copy(
                    update={
                        "status": existing.status,
                        "error": "controller status unavailable while waiting for non-terminal task; will reconcile later",
                        "last_reconciled_at": utc_now_iso(),
                        "last_reconciled_status": "unknown",
                        "next_attempt_after": time.time() + 1.0,
                    }
                )
            if not snapshots:
                return self._unknown_status(
                    config,
                    task,
                    existing,
                    "controller task disappeared while waiting during resume",
                    allow_fresh_submit=True,
                )
            snapshot = snapshots[0]
        return self._from_snapshot(existing, snapshot)

    def _unknown_status(
        self,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        existing: BatchTaskState,
        message: str,
        allow_fresh_submit: bool,
    ) -> BatchTaskState:
        if self._has_retry_budget(config=config, task=task, existing=existing):
            count = existing.reconcile_unknown_count + 1
            if not allow_fresh_submit:
                return self._delayed_unknown_status(config, existing, message, reconcile_unknown_count=count)
            if count >= _UNKNOWN_RECONCILE_CLEAR_AFTER:
                return self._retryable_unknown_status(
                    config,
                    existing,
                    message,
                    reconcile_unknown_count=count,
                )
            return self._delayed_unknown_status(config, existing, message, reconcile_unknown_count=count)
        return self._blocked_unknown_status(existing, f"{message}; retry budget exhausted")

    @staticmethod
    def _has_retry_budget(*, config: BatchRunConfig, task: BatchTaskSpec, existing: BatchTaskState) -> bool:
        if existing.retry_budget_remaining > 0:
            return True
        max_retries = task.max_retries if task.max_retries >= 0 else config.manifest.defaults.max_retries
        return existing.attempts < max(max_retries, 0) + 1

    @staticmethod
    def _delayed_unknown_status(
        config: BatchRunConfig,
        existing: BatchTaskState,
        message: str,
        *,
        reconcile_unknown_count: int,
    ) -> BatchTaskState:
        """Preserve the controller id and retry status lookup later.

        Unknown controller status is retryable infrastructure state, but it
        must not remain locally submitted/running while it waits for backoff:
        those statuses count as active batch work and can overfill later waves.
        Keeping the controller id on a failed row still forces the next claim to
        reconcile the old controller task before any fresh submit.
        """

        delay = _unknown_reconcile_backoff_seconds(config.infra_retry_backoff_seconds, reconcile_unknown_count)
        return existing.model_copy(
            update={
                "status": BatchTaskStatus.FAILED,
                "error": f"{message}; will retry controller status reconcile before any fresh submit",
                "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": "unknown",
                "next_attempt_after": time.time() + delay,
                "reconcile_unknown_count": reconcile_unknown_count,
            }
        )

    @staticmethod
    def _retryable_unknown_status(
        config: BatchRunConfig,
        existing: BatchTaskState,
        message: str,
        *,
        reconcile_unknown_count: int,
    ) -> BatchTaskState:
        """Clear stale controller ids so a budgeted resume can submit a fresh attempt."""

        delay = _unknown_reconcile_backoff_seconds(config.infra_retry_backoff_seconds, 1)
        return existing.model_copy(
            update={
                "controller_task_id": "",
                "trace_id": "",
                "session_id": "",
                "terminal_state": None,
                "status": BatchTaskStatus.FAILED,
                "error": (
                    f"{message}; cleared stale controller task id for retry after "
                    f"{reconcile_unknown_count} unknown reconcile attempts"
                ),
                "recovery_action": None,
                "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": "unknown",
                "next_attempt_after": time.time() + delay,
                "reconcile_unknown_count": reconcile_unknown_count,
                "terminal_decision": {},
            }
        )

    @staticmethod
    def _blocked_unknown_status(existing: BatchTaskState, message: str) -> BatchTaskState:
        """Return a fail-fast reconciliation state for unknown submitted work.

        Args:
            existing: Local state with a previously assigned controller task id.
            message: Operator-facing reason recorded on the task state.

        Returns:
            Failed state that tells the scheduler to stop instead of submitting a
            second controller task for the same batch case.
        """

        return existing.model_copy(
            update={
                "status": BatchTaskStatus.FAILED,
                "error": message,
                "recovery_action": FailureAction.FAIL_FAST,
                "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
                "last_reconciled_at": utc_now_iso(),
                "last_reconciled_status": "unknown",
                "reconcile_unknown_count": existing.reconcile_unknown_count + 1,
            }
        )

    def _from_snapshot(self, existing: BatchTaskState, snapshot: TaskStatusSnapshot) -> BatchTaskState:
        return reconciled_state_from_controller_snapshot(existing, snapshot)

    @staticmethod
    def _local_status(
        controller_state: TaskState,
        *,
        last_failure_category: FailureCategory | None = None,
        evaluator_success: bool = False,
    ) -> BatchTaskStatus:
        return _local_status_from_controller_state(
            controller_state,
            last_failure_category=last_failure_category,
            evaluator_success=evaluator_success,
        )


def reconciled_state_from_controller_snapshot(
    existing: BatchTaskState,
    snapshot: TaskStatusSnapshot,
) -> BatchTaskState:
    """Return the local batch state implied by an authoritative controller snapshot.

    Args:
        existing: Current local batch state before reconciliation.
        snapshot: Controller task snapshot read from the local controller DB or
            status API.

    Returns:
        Updated batch state that preserves retry metadata while reflecting the
        controller terminal or non-terminal state.
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
    local_status = _local_status_from_controller_state(
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


def _local_status_from_controller_state(
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


def _unknown_reconcile_backoff_seconds(backoffs: list[float], count: int) -> float:
    values = [float(value) for value in (backoffs or _DEFAULT_UNKNOWN_RECONCILE_BACKOFF_SECONDS) if float(value) >= 0]
    if not values:
        return _MIN_UNKNOWN_RECONCILE_BACKOFF_SECONDS
    index = min(max(count - 1, 0), len(values) - 1)
    return max(values[index], _MIN_UNKNOWN_RECONCILE_BACKOFF_SECONDS)
