"""Controller submit reconciliation helpers for batch task attempts."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.controller_db_status import LocalControllerDBStatusReader
from northcua.batch.evaluator_outcome import (
    evaluator_accepts_uncontrollable_result,
    evaluator_is_pending_from_terminal,
    evaluator_report_is_present,
    evaluator_terminal_metadata,
    evaluator_verdict_is_success,
)
from northcua.batch.metrics import BatchMetrics
from northcua.batch.planner import PlannedTaskSubmission
from northcua.batch.retry_policy import is_infra_retryable_failure
from northcua.batch.runner.controller import ControllerTaskWatcher
from northcua.batch.runner.events import BatchAttemptEventMessage, BatchEventWriter
from northcua.batch.runner.retry_backoff import backoff_delay_seconds
from northcua.sandboxes.contracts import SandboxLease
from northcua.telemetry.contracts import CasePhase, CaseStatus, FailureCategory, TelemetryEntityKind, utc_now_iso
from northcua.telemetry.failure_categories import failure_category_from_message, failure_category_from_terminal_decision
from northcua_protocol.payloads import TaskStatusSnapshot
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)
_RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS = "release_current_lease_after_duplicate_reconcile"

_TERMINAL_CONTROLLER_STATES = {
    TaskState.FINISHED,
    TaskState.FAILED,
    TaskState.INTERRUPTED,
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
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


def _status_websocket_fallback_on_db_timeout_enabled() -> bool:
    return _env_bool("NORTHCUA_BATCH_RECONCILE_STATUS_WEBSOCKET_FALLBACK_ON_DB_TIMEOUT", False)


def _submit_accept_reconcile_poll_interval_seconds() -> float:
    return max(_env_float("NORTHCUA_BATCH_SUBMIT_ACCEPT_RECONCILE_POLL_SECONDS", 1.0), 0.05)


def _submit_accept_reconcile_timeout_seconds(timeout: float | None) -> float:
    if timeout is not None and timeout > 0:
        return timeout
    return max(_env_float("NORTHCUA_BATCH_SUBMIT_ACCEPT_RECONCILE_TIMEOUT_SECONDS", 900.0), 1.0)


def _submit_accept_nonterminal_reconcile_wait_seconds() -> float:
    return max(_env_float("NORTHCUA_BATCH_SUBMIT_ACCEPT_NONTERMINAL_RECONCILE_WAIT_SECONDS", 0.0), 0.0)


def _submit_accept_reconcile_deadline_monotonic(*, task_start: float, timeout_seconds: float | None) -> float:
    if timeout_seconds is not None and timeout_seconds > 0:
        return task_start + timeout_seconds
    return time.monotonic() + _submit_accept_reconcile_timeout_seconds(timeout_seconds)


async def _local_duplicate_statuses(
    *,
    controller_db_path: Path | str | None,
    task_id: str,
) -> list[TaskStatusSnapshot] | None:
    timeout_seconds = _env_float("NORTHCUA_BATCH_DUPLICATE_STATUS_DB_TIMEOUT_SECONDS", 1.0)
    busy_timeout_ms = _env_int("NORTHCUA_BATCH_DUPLICATE_STATUS_DB_BUSY_TIMEOUT_MS", 1000)
    if timeout_seconds <= 0:
        return None
    reader = LocalControllerDBStatusReader(controller_db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(reader.statuses, [task_id]),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        allow_websocket_fallback = _status_websocket_fallback_on_db_timeout_enabled()
        logger.warning(
            "local controller DB duplicate status lookup timed out",
            extra={
                "controller_task_id": task_id,
                "timeout_seconds": timeout_seconds,
                "websocket_fallback": allow_websocket_fallback,
            },
        )
        return None


def _emit_controller_status_db_lookup_finished(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    source: str,
    elapsed_seconds: float,
    snapshots: list[TaskStatusSnapshot] | None,
    error: str = "",
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_STATUS_DB_LOOKUP_FINISHED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "source": source,
            "db_query_elapsed_seconds": round(max(elapsed_seconds, 0.0), 3),
            "snapshot_count": len(snapshots or []),
            "timed_out_or_unknown": snapshots is None,
            "error": _short_error(error),
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


async def _local_duplicate_statuses_with_telemetry(
    *,
    config: BatchRunConfig,
    planned: PlannedTaskSubmission,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    events: BatchEventWriter,
    source: str,
) -> list[TaskStatusSnapshot] | None:
    started = time.monotonic()
    try:
        snapshots = await _local_duplicate_statuses(
            controller_db_path=config.telemetry_controller_db_path,
            task_id=planned.controller_task_id,
        )
    except Exception as exc:
        logger.warning(
            "controller duplicate status lookup failed during submit reconciliation",
            extra={
                "task_id": task.task_id,
                "attempt": state.attempts,
                "controller_task_id": planned.controller_task_id,
                "source": source,
            },
            exc_info=True,
        )
        _emit_controller_status_db_lookup_finished(
            events=events,
            task=task,
            lease=lease,
            state=state,
            planned=planned,
            source=source,
            elapsed_seconds=time.monotonic() - started,
            snapshots=None,
            error=str(exc),
        )
        raise
    _emit_controller_status_db_lookup_finished(
        events=events,
        task=task,
        lease=lease,
        state=state,
        planned=planned,
        source=source,
        elapsed_seconds=time.monotonic() - started,
        snapshots=snapshots,
    )
    return snapshots


async def _state_from_existing_controller_task_after_submit_timeout(
    *,
    exc: Exception,
    config: BatchRunConfig,
    planned: PlannedTaskSubmission,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
    timeout_seconds: float | None,
) -> BatchTaskState | None:
    if not isinstance(exc, TimeoutError):
        return None
    try:
        snapshots = await _local_duplicate_statuses_with_telemetry(
            config=config,
            planned=planned,
            task=task,
            lease=lease,
            state=state,
            events=events,
            source="submit_accept_timeout_reconcile",
        )
    except Exception:
        logger.warning(
            "controller submit timeout could not be reconciled from local controller DB",
            extra={"task_id": task.task_id, "controller_task_id": planned.controller_task_id},
            exc_info=True,
        )
        return _duplicate_unknown_reconcile_state(
            config=config,
            planned=planned,
            state=state,
            error="controller submit timed out waiting for TASK_ACCEPTED; local controller task status unknown",
        )
    if not snapshots:
        return _duplicate_unknown_reconcile_state(
            config=config,
            planned=planned,
            state=state,
            error="controller submit timed out waiting for TASK_ACCEPTED; local controller task status unknown",
        )
    snapshot = snapshots[0]
    if not _snapshot_matches_planned_duplicate(snapshot, planned=planned, task=task):
        logger.warning(
            "controller submit timeout snapshot did not match planned batch attempt",
            extra={
                "task_id": task.task_id,
                "controller_task_id": planned.controller_task_id,
                "snapshot_task_id": snapshot.task_id,
                "snapshot_trace_id": snapshot.trace_id,
            },
        )
        return None
    if snapshot.state in _TERMINAL_CONTROLLER_STATES:
        return _terminal_state_from_snapshot(
            snapshot=snapshot,
            task=task,
            lease=lease,
            state=state,
            metrics=metrics,
            events=events,
            task_start=task_start,
            source="submit_accept_timeout_reconcile",
        )
    wait_seconds = _submit_accept_nonterminal_reconcile_wait_seconds()
    logger.info(
        "controller submit timed out waiting for TASK_ACCEPTED but local controller task exists",
        extra={
            "task_id": task.task_id,
            "controller_task_id": snapshot.task_id,
            "controller_status": snapshot.state.value,
            "nonterminal_reconcile_wait_seconds": round(wait_seconds, 3),
        },
    )
    terminal = None
    if wait_seconds > 0:
        terminal = await _wait_for_terminal_snapshot_after_submit_timeout(
            config=config,
            planned=planned,
            task=task,
            lease=lease,
            state=state,
            events=events,
            initial=snapshot,
            task_start=task_start,
            timeout_seconds=min(timeout_seconds or wait_seconds, wait_seconds),
        )
    if terminal is None:
        defer_cleanup = _snapshot_targets_current_lease(snapshot, planned=planned, lease=lease)
        return _non_terminal_controller_reconcile_state(
            config=config,
            snapshot=snapshot,
            task=task,
            lease=lease,
            state=state,
            events=events,
            source="submit_accept_timeout_reconcile",
            reason="controller task exists but TASK_ACCEPTED was not observed; will reconcile later",
            defer_cleanup=defer_cleanup,
        )
    return _terminal_state_from_snapshot(
        snapshot=terminal,
        task=task,
        lease=lease,
        state=state,
        metrics=metrics,
        events=events,
        task_start=task_start,
        source="submit_accept_timeout_reconcile",
    )


async def _wait_for_terminal_snapshot_after_submit_timeout(
    *,
    config: BatchRunConfig,
    planned: PlannedTaskSubmission,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    events: BatchEventWriter,
    initial: TaskStatusSnapshot,
    task_start: float,
    timeout_seconds: float | None,
) -> TaskStatusSnapshot | None:
    deadline = _submit_accept_reconcile_deadline_monotonic(task_start=task_start, timeout_seconds=timeout_seconds)
    snapshot = initial
    poll_seconds = _submit_accept_reconcile_poll_interval_seconds()
    while snapshot.state not in _TERMINAL_CONTROLLER_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(poll_seconds, remaining))
        try:
            snapshots = await _local_duplicate_statuses_with_telemetry(
                config=config,
                planned=planned,
                task=task,
                lease=lease,
                state=state,
                events=events,
                source="submit_accept_timeout_terminal_poll",
            )
        except Exception:
            logger.warning(
                "controller submit timeout terminal wait could not read local controller DB; retrying",
                extra={"task_id": task.task_id, "controller_task_id": planned.controller_task_id},
                exc_info=True,
            )
            continue
        if not snapshots:
            continue
        candidate = snapshots[0]
        if not _snapshot_matches_planned_duplicate(candidate, planned=planned, task=task):
            logger.warning(
                "controller submit timeout terminal wait snapshot did not match planned batch attempt",
                extra={
                    "task_id": task.task_id,
                    "controller_task_id": planned.controller_task_id,
                    "snapshot_task_id": candidate.task_id,
                    "snapshot_trace_id": candidate.trace_id,
                },
            )
            return None
        snapshot = candidate
    return snapshot


async def _state_from_existing_controller_task_if_duplicate(
    *,
    exc: Exception,
    watcher: ControllerTaskWatcher,
    controller_url: str,
    token: str,
    config: BatchRunConfig,
    planned: PlannedTaskSubmission,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
) -> BatchTaskState | None:
    if "task_id already exists" not in str(exc):
        return None
    controller_db_path = config.telemetry_controller_db_path
    try:
        snapshots = await _local_duplicate_statuses_with_telemetry(
            config=config,
            planned=planned,
            task=task,
            lease=lease,
            state=state,
            events=events,
            source="duplicate_task_id_reconcile",
        )
        if (
            snapshots is None
            and controller_url
            and (not controller_db_path or _status_websocket_fallback_on_db_timeout_enabled())
        ):
            snapshots = await watcher.statuses(
                controller_url=controller_url,
                token=token,
                task_ids=[planned.controller_task_id],
            )
    except Exception:
        logger.warning(
            "controller duplicate task id could not be reconciled",
            extra={"task_id": task.task_id, "controller_task_id": planned.controller_task_id},
            exc_info=True,
        )
        return None
    if not snapshots:
        return _duplicate_unknown_reconcile_state(
            config=config,
            planned=planned,
            state=state,
            error=str(exc),
        )
    snapshot = snapshots[0]
    if not _snapshot_matches_planned_duplicate(snapshot, planned=planned, task=task):
        logger.warning(
            "controller duplicate task id snapshot did not match planned batch attempt",
            extra={
                "task_id": task.task_id,
                "controller_task_id": planned.controller_task_id,
                "snapshot_task_id": snapshot.task_id,
                "snapshot_trace_id": snapshot.trace_id,
            },
        )
        return None
    if snapshot.state not in _TERMINAL_CONTROLLER_STATES:
        wait_seconds = _submit_accept_nonterminal_reconcile_wait_seconds()
        logger.info(
            "controller duplicate task id points at non-terminal task",
            extra={
                "task_id": task.task_id,
                "controller_task_id": snapshot.task_id,
                "controller_status": snapshot.state.value,
                "nonterminal_reconcile_wait_seconds": round(wait_seconds, 3),
            },
        )
        terminal = None
        if wait_seconds > 0:
            terminal = await _wait_for_terminal_snapshot_after_submit_timeout(
                config=config,
                planned=planned,
                task=task,
                lease=lease,
                state=state,
                events=events,
                initial=snapshot,
                task_start=task_start,
                timeout_seconds=wait_seconds,
            )
        if terminal is not None:
            return _terminal_state_from_snapshot(
                snapshot=terminal,
                task=task,
                lease=lease,
                state=state,
                metrics=metrics,
                events=events,
                task_start=task_start,
                source="duplicate_task_id_reconcile",
            )
        defer_cleanup = _snapshot_targets_current_lease(snapshot, planned=planned, lease=lease)
        return _non_terminal_controller_reconcile_state(
            config=config,
            snapshot=snapshot,
            task=task,
            lease=lease,
            state=state,
            events=events,
            source="duplicate_task_id_reconcile",
            reason="controller task already exists and is non-terminal; will reconcile later",
            defer_cleanup=defer_cleanup,
        )
    return _terminal_state_from_snapshot(
        snapshot=snapshot,
        task=task,
        lease=lease,
        state=state,
        metrics=metrics,
        events=events,
        task_start=task_start,
    )


def _non_terminal_controller_reconcile_state(
    *,
    config: BatchRunConfig,
    snapshot: TaskStatusSnapshot,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    events: BatchEventWriter,
    source: str,
    reason: str,
    defer_cleanup: bool = True,
) -> BatchTaskState:
    _ = config
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_EXISTING_NON_TERMINAL_RECONCILED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=snapshot.task_id,
        details={
            "controller_state": snapshot.state.value,
            "source": source,
            "reason": reason,
            "defer_cleanup": defer_cleanup,
            "snapshot_sandbox_id": (snapshot.metadata or {}).get("sandbox_id", ""),
            "snapshot_device_id": snapshot.device_id or "",
            "current_sandbox_id": lease.handle.sandbox_id,
            "current_device_id": lease.device_id,
        },
        controller_task_id=snapshot.task_id,
        trace_id=snapshot.trace_id or state.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state.model_copy(
        update={
            "controller_task_id": snapshot.task_id,
            "trace_id": snapshot.trace_id or state.trace_id,
            "session_id": snapshot.session_id or state.session_id,
            "device_id": snapshot.device_id or state.device_id,
            "status": BatchTaskStatus.SUBMITTED,
            "cleanup_status": "" if defer_cleanup else _RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS,
            "error": "",
            "last_failure_category": None,
            "last_reconciled_at": utc_now_iso(),
            "last_reconciled_status": snapshot.state.value,
            "next_attempt_after": 0.0,
            "reconcile_unknown_count": 0,
        }
    )


def _duplicate_reconcile_backoff_seconds(config: BatchRunConfig, *, attempt: int) -> float:
    delay = backoff_delay_seconds(config.infra_retry_backoff_seconds, attempt=attempt)
    return max(delay, 1.0)


def _duplicate_unknown_reconcile_state(
    *,
    config: BatchRunConfig,
    planned: PlannedTaskSubmission,
    state: BatchTaskState,
    error: str,
) -> BatchTaskState:
    return state.model_copy(
        update={
            "controller_task_id": planned.controller_task_id,
            "trace_id": planned.trace_id or state.trace_id,
            "status": BatchTaskStatus.FAILED,
            "error": f"{error}; duplicate controller task status unknown; will reconcile before fresh submit",
            "last_failure_category": FailureCategory.CONTROLLER_SUBMIT,
            "last_reconciled_at": utc_now_iso(),
            "last_reconciled_status": "unknown",
            "next_attempt_after": time.time() + _duplicate_reconcile_backoff_seconds(config, attempt=state.attempts),
            "reconcile_unknown_count": state.reconcile_unknown_count + 1,
        }
    )


def _snapshot_matches_planned_duplicate(
    snapshot: TaskStatusSnapshot,
    *,
    planned: PlannedTaskSubmission,
    task: BatchTaskSpec,
) -> bool:
    if snapshot.task_id != planned.controller_task_id:
        return False
    if not snapshot.trace_id or snapshot.trace_id != planned.trace_id:
        return False
    if not snapshot.agent_kind or snapshot.agent_kind != planned.agent_kind:
        return False
    metadata = snapshot.metadata or {}
    expected = {
        "batch_id": planned.metadata.get("batch_id"),
        "batch_task_id": planned.metadata.get("batch_task_id", task.task_id),
        "batch_attempt": planned.metadata.get("batch_attempt"),
    }
    if any(value is None for value in expected.values()):
        return False
    return all(key in metadata and str(metadata.get(key)) == str(value) for key, value in expected.items())


def _snapshot_targets_current_lease(
    snapshot: TaskStatusSnapshot,
    *,
    planned: PlannedTaskSubmission,
    lease: SandboxLease,
) -> bool:
    metadata = snapshot.metadata or {}
    matched = False
    snapshot_sandbox_id = metadata.get("sandbox_id")
    expected_sandbox_id = planned.metadata.get("sandbox_id") or lease.handle.sandbox_id
    if snapshot_sandbox_id and expected_sandbox_id and str(snapshot_sandbox_id) != str(expected_sandbox_id):
        return False
    if snapshot_sandbox_id and expected_sandbox_id:
        matched = True
    if snapshot.device_id and str(snapshot.device_id) != str(lease.device_id):
        return False
    if snapshot.device_id:
        matched = True
    return matched


def _terminal_state_from_snapshot(
    *,
    snapshot: TaskStatusSnapshot,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
    source: str = "duplicate_task_id_reconcile",
) -> BatchTaskState:
    elapsed_seconds = time.monotonic() - task_start
    metrics.observe_task(elapsed_seconds)
    evaluator_report_present = evaluator_report_is_present(snapshot.result)
    evaluator_success = evaluator_verdict_is_success(snapshot.result)
    evaluator_accepted_infeasible = evaluator_accepts_uncontrollable_result(snapshot.result)
    evaluator_pending = evaluator_is_pending_from_terminal(snapshot.terminal_decision)
    evaluator_metadata = evaluator_terminal_metadata(snapshot.terminal_decision)
    terminal_failure_category = failure_category_from_terminal_decision(snapshot.terminal_decision)
    last_failure_category = _last_failure_category(
        snapshot.state,
        snapshot.error or "",
        terminal_failure_category,
    )
    if evaluator_success:
        last_failure_category = FailureCategory.ACCEPTED_INFEASIBLE if evaluator_accepted_infeasible else None
    terminal_status = _local_status_from_controller_terminal(
        snapshot.state,
        last_failure_category=last_failure_category,
        evaluator_success=evaluator_success,
        evaluator_report_present=evaluator_report_present,
    )
    if evaluator_pending:
        terminal_status = BatchTaskStatus.EVALUATION_PENDING
        last_failure_category = FailureCategory.EVALUATOR
    if terminal_status == BatchTaskStatus.FAILED:
        last_failure_category = last_failure_category or FailureCategory.MAIN_AGENT
        metrics.observe_attempt_failure(last_failure_category)
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_FINISHED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=_case_status_from_terminal(terminal_status),
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=snapshot.task_id,
        details={
            "elapsed_seconds": round(elapsed_seconds, 3),
            "terminal_state": snapshot.state.value,
            "terminal_status": terminal_status.value,
            "failure_category": last_failure_category.value if last_failure_category is not None else "",
            "error": _short_error(snapshot.error or ""),
            "evaluator_accepted_infeasible": evaluator_accepted_infeasible,
            "evaluation_status": evaluator_metadata.get("status", ""),
            "evaluation_projection_level": evaluator_metadata.get("projection_level", ""),
            "evaluation_attempts": evaluator_metadata.get("attempts", 0),
            "source": source,
        },
        controller_task_id=snapshot.task_id,
        trace_id=snapshot.trace_id or state.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state.model_copy(
        update={
            "controller_task_id": snapshot.task_id,
            "trace_id": snapshot.trace_id or state.trace_id,
            "session_id": snapshot.session_id or state.session_id,
            "device_id": snapshot.device_id or state.device_id,
            "status": terminal_status,
            "terminal_state": snapshot.state,
            "error": (
                ""
                if terminal_status in {BatchTaskStatus.SUCCEEDED, BatchTaskStatus.EVALUATION_PENDING}
                else snapshot.error or ""
            ),
            "last_failure_category": last_failure_category,
            "last_reconciled_at": utc_now_iso(),
            "last_reconciled_status": snapshot.state.value,
            "terminal_decision": snapshot.terminal_decision,
            "agent_terminal": _agent_terminal_from_snapshot(snapshot),
            "evaluation_status": str(evaluator_metadata.get("status") or ""),
            "evaluation_error": evaluator_metadata,
            "evaluation_attempts": _int_value(evaluator_metadata.get("attempts")),
            "evaluation_projection_level": str(evaluator_metadata.get("projection_level") or ""),
        }
    )


def _last_failure_category(
    state: TaskState,
    error: str,
    terminal_failure_category: FailureCategory | None,
) -> FailureCategory | None:
    if state == TaskState.FAILED:
        return terminal_failure_category or failure_category_from_message(error) or FailureCategory.MAIN_AGENT
    if state == TaskState.INTERRUPTED:
        return terminal_failure_category or failure_category_from_message(error)
    return None


def _local_status_from_controller_terminal(
    state: TaskState,
    *,
    last_failure_category: FailureCategory | None,
    evaluator_success: bool = False,
    evaluator_report_present: bool = False,
) -> BatchTaskStatus:
    if evaluator_success:
        return BatchTaskStatus.SUCCEEDED
    if evaluator_report_present:
        return BatchTaskStatus.FAILED
    if state == TaskState.FINISHED:
        return BatchTaskStatus.SUCCEEDED
    if state == TaskState.INTERRUPTED and last_failure_category == FailureCategory.TIMEOUT:
        return BatchTaskStatus.TIMED_OUT
    if state == TaskState.INTERRUPTED and not is_infra_retryable_failure(last_failure_category):
        return BatchTaskStatus.CANCELED
    return BatchTaskStatus.FAILED


def _case_status_from_terminal(status: BatchTaskStatus) -> CaseStatus:
    if status == BatchTaskStatus.SUCCEEDED:
        return CaseStatus.SUCCEEDED
    if status == BatchTaskStatus.TIMED_OUT:
        return CaseStatus.TIMED_OUT
    if status == BatchTaskStatus.CANCELED:
        return CaseStatus.CANCELED
    if status == BatchTaskStatus.EVALUATION_PENDING:
        return CaseStatus.EVALUATING
    return CaseStatus.FAILED


def _short_error(error: str, *, max_length: int = 500) -> str:
    return error if len(error) <= max_length else error[: max_length - 3] + "..."


def _agent_terminal_from_snapshot(snapshot: TaskStatusSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "result": _short_error(str(snapshot.result or ""), max_length=1000),
        "terminal_decision": snapshot.terminal_decision,
    }


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
