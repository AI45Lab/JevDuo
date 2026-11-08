"""Helper functions for executing one batch task attempt."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.metrics import BatchMetrics
from northcua.batch.planner import BatchTaskPlanner
from northcua.batch.post_run import PostRunHookResult, PostRunHookRunner, PostRunHookSpec, PostRunHookStatus
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.manager import PreparedSandboxManager
from northcua.batch.runner.attempt_failure import (
    _failure_category_for_attempt,
    _recovery_action_from_claim,
    _retry_budget_from_claim,
)
from northcua.batch.runner.attempt_submit import submit_to_controller
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.controller import ControllerTaskWatcher
from northcua.batch.runner.dry_run import run_dry_attempt
from northcua.batch.runner.events import BatchAttemptEventMessage, BatchEventWriter
from northcua.batch.runner.leases import BatchTaskLeaseContext, BatchTaskLeaseCoordinator
from northcua.batch.runner.telemetry_helpers import CleanupResult, release_with_telemetry
from northcua.sandboxes.contracts import (
    SandboxCommandResult,
    SandboxCommandSpec,
    SandboxFileUploadSpec,
    SandboxLease,
    SandboxProviderSpec,
)
from northcua.sandboxes.guest_capability_evidence import guest_capability_evidence_payloads
from northcua.telemetry.contracts import CasePhase, CaseStatus, FailureCategory, TelemetryEntityKind

logger = logging.getLogger(__name__)
_DEFERRED_CONTROLLER_RECONCILE_CLEANUP_STATUS = "deferred_controller_reconcile"
_RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS = "release_current_lease_after_duplicate_reconcile"
_StopRequested = Callable[[], bool | Awaitable[bool]]
_T = TypeVar("_T")


async def _finish_cleanup_task_after_cancellation(task: asyncio.Future[_T]) -> _T:
    """Wait for a cleanup task to finish despite repeated caller cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return await task


async def _await_prepared_cleanup_update(
    update: Awaitable[None],
    *,
    task_id: str,
    attempt: int,
    action: str,
) -> bool:
    """Run a prepared-sandbox state update through task cancellation.

    Returns:
        ``True`` when the caller was cancelled while the update was running.
    """

    update_task = asyncio.ensure_future(update)
    if isinstance(update_task, asyncio.Task):
        update_task.set_name(f"northcua-prepared-cleanup-{action}:{task_id}:{attempt}")
    try:
        await asyncio.shield(update_task)
        return False
    except asyncio.CancelledError:
        await _finish_cleanup_task_after_cancellation(update_task)
        return True


class PreparedSandboxBackpressureDeferred(RuntimeError):
    """Raised when pressure should defer a claim instead of failing an attempt."""


class PreSubmitCommandFailure(RuntimeError):
    """Raised when a required pre-submit command fails after device readiness."""

    failure_category = FailureCategory.STARTUP_COMMAND


def _round_seconds(value: float) -> float:
    return round(float(value), 3)


def _prepared_assignment_submission_details(assignment: PreparedSandboxAssignment) -> dict[str, object]:
    now = time.time()
    return {
        "prepared_sandbox_ttl_seconds": _round_seconds(assignment.prepared_ttl_seconds()),
        "remaining_ttl_seconds_at_submission": _round_seconds(assignment.remaining_ttl_seconds(now=now)),
        "prepared_age_seconds_at_submission": _round_seconds(assignment.prepared_age_seconds(now=now)),
        "prepared_to_assigned_seconds": _round_seconds(assignment.prepared_to_assigned_seconds()),
        "expired_before_assignment": assignment.expired_before_assignment(),
        "expired_after_assignment": assignment.expired_after_assignment(now=now),
        "prepared_sandbox_expires_at": assignment.record.expires_at,
        "prepared_sandbox_assigned_at": assignment.record.assigned_at,
    }


def _prepared_assignment_failure_details(
    assignment: PreparedSandboxAssignment,
    *,
    error: str,
) -> dict[str, object]:
    now = time.time()
    details: dict[str, object] = {
        "prepared_sandbox_ttl_seconds": _round_seconds(assignment.prepared_ttl_seconds()),
        "remaining_ttl_seconds_at_failure": _round_seconds(assignment.remaining_ttl_seconds(now=now)),
        "prepared_age_seconds_at_failure": _round_seconds(assignment.prepared_age_seconds(now=now)),
        "prepared_to_assigned_seconds": _round_seconds(assignment.prepared_to_assigned_seconds()),
        "assigned_runtime_seconds": _round_seconds(assignment.assigned_runtime_seconds(now=now)),
        "expired_before_assignment": assignment.expired_before_assignment(),
        "expired_after_assignment": assignment.expired_after_assignment(now=now),
        "prepared_sandbox_expires_at": assignment.record.expires_at,
        "prepared_sandbox_assigned_at": assignment.record.assigned_at,
    }
    if "websocket disconnected" in error.lower():
        details["assigned_runtime_seconds_before_disconnect"] = _round_seconds(
            assignment.assigned_runtime_seconds(now=now)
        )
    return details


def _prepared_assignment_cleanup_details(assignment: PreparedSandboxAssignment) -> dict[str, object]:
    now = time.time()
    return {
        "remaining_ttl_seconds_at_cleanup": _round_seconds(assignment.remaining_ttl_seconds(now=now)),
        "prepared_age_seconds_at_cleanup": _round_seconds(assignment.prepared_age_seconds(now=now)),
        "assigned_runtime_seconds_at_cleanup": _round_seconds(assignment.assigned_runtime_seconds(now=now)),
        "expired_before_assignment": assignment.expired_before_assignment(),
        "expired_after_assignment": assignment.expired_after_assignment(now=now),
        "prepared_sandbox_expires_at": assignment.record.expires_at,
        "prepared_sandbox_assigned_at": assignment.record.assigned_at,
    }


async def _execute_attempt(
    *,
    coordinator: BatchTaskLeaseCoordinator,
    binding_factory: SandboxDeviceBindingFactory,
    planner: BatchTaskPlanner,
    watcher: ControllerTaskWatcher,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    events: BatchEventWriter,
    metrics: BatchMetrics,
    output_dir: Path,
    attempt: int,
    create_semaphore: asyncio.Semaphore | None = None,
    submit_semaphore: asyncio.Semaphore | None = None,
    prepared_manager: PreparedSandboxManager | None = None,
    backpressure_gate: ControllerBackpressureGate | None = None,
    backpressure_stop_requested: _StopRequested | None = None,
) -> BatchTaskState:
    defaults = config.manifest.defaults
    submitter_controller_url = config.controller_url or defaults.controller_url
    device_controller_url = config.device_controller_url or defaults.device_controller_url or submitter_controller_url
    token = config.token or defaults.token
    sandbox_spec = task.sandbox or defaults.sandbox
    lease: SandboxLease | None = None
    lease_context: BatchTaskLeaseContext | None = None
    prepared_assignment: PreparedSandboxAssignment | None = None
    state = BatchTaskState(
        task_id=task.task_id,
        attempts=attempt,
        status=BatchTaskStatus.STARTING_SANDBOX,
        recovery_action=_recovery_action_from_claim(events),
        retry_budget_remaining=_retry_budget_from_claim(events),
    )
    await events.upsert_task_async(state)
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.ATTEMPT_STARTED,
        phase=CasePhase.CLAIMED,
        status=CaseStatus.PROVISIONING,
        attempt=attempt,
        details={
            "recovery_action": state.recovery_action.value if state.recovery_action is not None else "",
            "retry_budget_remaining": state.retry_budget_remaining,
            "controller_submit_concurrency": config.controller_submit_concurrency,
            "controller_submit_jitter_seconds": config.controller_submit_jitter_seconds,
        },
    )
    try:
        if config.dry_run:
            return run_dry_attempt(
                binding_factory=binding_factory,
                planner=planner,
                config=config,
                task=task,
                sandbox_spec=sandbox_spec,
                attempt=attempt,
                events=events,
            )
        (
            state,
            lease,
            lease_context,
            prepared_assignment,
            ready_for_controller_submit_at,
        ) = await _prepare_sandbox_submission(
            coordinator=coordinator,
            config=config,
            task=task,
            sandbox_spec=sandbox_spec,
            controller_url=device_controller_url,
            token=token,
            events=events,
            metrics=metrics,
            state=state,
            output_dir=output_dir,
            attempt=attempt,
            create_semaphore=create_semaphore,
            prepared_manager=prepared_manager,
            backpressure_gate=backpressure_gate,
        )
        if prepared_assignment is not None and prepared_manager is not None:
            await prepared_manager.run_deferred_startup(assignment=prepared_assignment, task=task)
        await _run_pre_submit_commands(
            config=config,
            task=task,
            lease=lease,
            lease_context=lease_context,
            attempt=attempt,
            events=events,
        )
        with events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.MAIN_AGENT,
            status=CaseStatus.RUNNING_AGENT,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.TASK,
            entity_id=state.controller_task_id or task.task_id,
        ):
            state = await submit_to_controller(
                planner=planner,
                watcher=watcher,
                config=config,
                task=task,
                lease=lease,
                controller_url=submitter_controller_url,
                token=token,
                metrics=metrics,
                state=state,
                events=events,
                submit_semaphore=submit_semaphore,
                backpressure_gate=backpressure_gate,
                backpressure_stop_requested=backpressure_stop_requested,
                ready_for_controller_submit_at=ready_for_controller_submit_at,
                prepared_assignment=prepared_assignment,
            )
        state = await _run_post_run_hooks(
            config=config,
            task=task,
            state=state,
            lease=lease,
            lease_context=lease_context,
            output_dir=output_dir,
            attempt=attempt,
            events=events,
        )
        await events.upsert_task_async(state)
        return state
    except PreparedSandboxBackpressureDeferred:
        raise
    except TimeoutError as exc:
        state = await _record_attempt_failure(
            state=state,
            task=task,
            attempt=attempt,
            status=BatchTaskStatus.TIMED_OUT,
            error=exc,
            events=events,
            prepared_assignment=prepared_assignment,
        )
        metrics.observe_attempt_failure(state.last_failure_category)
        return state
    except Exception as exc:
        logger.warning("batch task attempt failed", extra={"task_id": task.task_id, "attempt": attempt}, exc_info=True)
        state = await _record_attempt_failure(
            state=state,
            task=task,
            attempt=attempt,
            status=BatchTaskStatus.FAILED,
            error=exc,
            events=events,
            prepared_assignment=prepared_assignment,
            emit_log=False,
        )
        metrics.observe_attempt_failure(state.last_failure_category)
        return state
    finally:
        cleanup_cancelled = False
        if _should_defer_controller_reconcile_cleanup(state):
            cleanup_result = CleanupResult(ok=True, cleanup_status=_DEFERRED_CONTROLLER_RECONCILE_CLEANUP_STATUS)
        else:
            cleanup_task = asyncio.create_task(
                release_with_telemetry(
                    release=_release_task_lease,
                    coordinator=coordinator,
                    config=config,
                    task=task,
                    state=state,
                    lease=lease,
                    lease_context=lease_context,
                    output_dir=output_dir,
                    attempt=attempt,
                    events=events,
                ),
                name=f"northcua-attempt-cleanup:{task.task_id}:{attempt}",
            )
            try:
                cleanup_result = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cleanup_cancelled = True
                cleanup_result = await _finish_cleanup_task_after_cancellation(cleanup_task)
        cleanup_deferred = cleanup_result.cleanup_status == _DEFERRED_CONTROLLER_RECONCILE_CLEANUP_STATUS
        if (
            cleanup_result.ok
            and prepared_assignment is not None
            and prepared_manager is not None
            and not cleanup_deferred
        ):
            cleanup_cancelled = (
                await _await_prepared_cleanup_update(
                    prepared_manager.mark_released_async(prepared_assignment),
                    task_id=task.task_id,
                    attempt=attempt,
                    action="released",
                )
                or cleanup_cancelled
            )
        elif prepared_assignment is not None and prepared_manager is not None and not cleanup_deferred:
            cleanup_cancelled = (
                await _await_prepared_cleanup_update(
                    prepared_manager.mark_cleanup_failed_async(
                        prepared_assignment,
                        error=cleanup_result.cleanup_status or "prepared sandbox cleanup failed",
                    ),
                    task_id=task.task_id,
                    attempt=attempt,
                    action="cleanup-failed",
                )
                or cleanup_cancelled
            )
        elif prepared_assignment is not None and prepared_manager is not None and cleanup_deferred:
            cleanup_cancelled = (
                await _await_prepared_cleanup_update(
                    prepared_manager.mark_cleanup_deferred_async(
                        prepared_assignment,
                        error=cleanup_result.cleanup_status or _DEFERRED_CONTROLLER_RECONCILE_CLEANUP_STATUS,
                    ),
                    task_id=task.task_id,
                    attempt=attempt,
                    action="cleanup-deferred",
                )
                or cleanup_cancelled
            )
        if cleanup_deferred:
            state = state.model_copy(update={"cleanup_status": cleanup_result.cleanup_status})
            await events.upsert_task_async(state)
        cleanup_details: dict[str, object] = {
            "cleanup_ok": cleanup_result.ok,
            "cleanup_status": cleanup_result.cleanup_status,
            "cleanup_deferred": cleanup_deferred,
            "prepared_assignment": prepared_assignment is not None,
            "terminal_status": state.status.value,
        }
        if prepared_assignment is not None:
            cleanup_details.update(_prepared_assignment_cleanup_details(prepared_assignment))
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.ATTEMPT_CLEANUP_FINISHED,
            phase=CasePhase.CLEANUP,
            status=_case_status_for_batch_task(state.status),
            attempt=attempt,
            entity_kind=TelemetryEntityKind.SANDBOX,
            entity_id=lease.handle.sandbox_id if lease is not None else task.task_id,
            details=cleanup_details,
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            sandbox_id=lease.handle.sandbox_id if lease is not None else state.sandbox_id,
            device_id=lease.device_id if lease is not None else state.device_id,
        )
        if cleanup_cancelled:
            raise asyncio.CancelledError()


async def _prepare_sandbox_submission(
    *,
    coordinator: BatchTaskLeaseCoordinator,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    sandbox_spec: SandboxProviderSpec,
    controller_url: str,
    token: str,
    events: BatchEventWriter,
    metrics: BatchMetrics,
    state: BatchTaskState,
    output_dir: Path,
    attempt: int,
    create_semaphore: asyncio.Semaphore | None,
    prepared_manager: PreparedSandboxManager | None,
    backpressure_gate: ControllerBackpressureGate | None = None,
) -> tuple[BatchTaskState, SandboxLease, BatchTaskLeaseContext | None, PreparedSandboxAssignment | None, float]:
    """Acquire sandbox capacity and persist the task's target device."""

    prepare_started = time.monotonic()
    lease_context: BatchTaskLeaseContext | None = None
    prepared_assignment: PreparedSandboxAssignment | None = None
    source = "direct_create"
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.SANDBOX_PREPARE_STARTED,
        phase=CasePhase.SANDBOX_CREATE,
        status=CaseStatus.PROVISIONING,
        attempt=attempt,
        entity_kind=TelemetryEntityKind.SANDBOX,
        details={
            "prepared_sandboxes": prepared_manager is not None,
            "create_semaphore": create_semaphore is not None,
            "device_readiness_timeout_seconds": config.device_readiness_timeout_seconds,
        },
    )
    if prepared_manager is not None:
        prepared_assignment = await prepared_manager.claim(task=task, attempt=attempt)
        if prepared_assignment is not None:
            source = "prepared_sandbox_hit"
            lease = prepared_assignment.lease
            lease_context = prepared_assignment.lease_context
        elif backpressure_gate is not None and (await backpressure_gate.snapshot()).active:
            raise PreparedSandboxBackpressureDeferred(
                "controller backpressure deferred direct sandbox create after prepared miss"
            )
        else:
            source = "prepared_sandbox_miss_create"
            with events.phase_timer(
                case_id=task.task_id,
                phase=CasePhase.SANDBOX_CREATE,
                status=CaseStatus.PROVISIONING,
                attempt=attempt,
                entity_kind=TelemetryEntityKind.SANDBOX,
            ):
                created_at = time.monotonic()
                lease_context = await coordinator.acquire(
                    config=config,
                    task=task,
                    sandbox_spec=sandbox_spec,
                    controller_url=controller_url,
                    token=token,
                    events=events,
                    output_dir=output_dir,
                    attempt=attempt,
                    create_semaphore=create_semaphore,
                )
                lease = lease_context.lease
                metrics.observe_sandbox_create(time.monotonic() - created_at)
    else:
        with events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.SANDBOX_CREATE,
            status=CaseStatus.PROVISIONING,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.SANDBOX,
        ):
            created_at = time.monotonic()
            lease_context = await coordinator.acquire(
                config=config,
                task=task,
                sandbox_spec=sandbox_spec,
                controller_url=controller_url,
                token=token,
                events=events,
                output_dir=output_dir,
                attempt=attempt,
                create_semaphore=create_semaphore,
            )
            lease = lease_context.lease
            metrics.observe_sandbox_create(time.monotonic() - created_at)
    state = state.model_copy(
        update={
            "sandbox_id": lease.handle.sandbox_id,
            "device_id": lease.device_id,
            "status": BatchTaskStatus.WAITING_DEVICE,
        }
    )
    await events.upsert_task_async(state)
    ready_details: dict[str, object] = {
        "source": source,
        "elapsed_seconds": round(time.monotonic() - prepare_started, 3),
        "prepared_sandbox_hit": prepared_assignment is not None,
    }
    if prepared_assignment is not None:
        ready_details.update(_prepared_assignment_submission_details(prepared_assignment))
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.SANDBOX_READY_FOR_CONTROLLER_SUBMIT,
        phase=CasePhase.WORKER_READY,
        status=CaseStatus.BOOTSTRAPPING,
        attempt=attempt,
        entity_kind=TelemetryEntityKind.DEVICE,
        entity_id=lease.device_id,
        details=ready_details,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state, lease, lease_context, prepared_assignment, time.monotonic()


async def _record_attempt_failure(
    *,
    state: BatchTaskState,
    task: BatchTaskSpec,
    attempt: int,
    status: BatchTaskStatus,
    error: Exception,
    events: BatchEventWriter,
    prepared_assignment: PreparedSandboxAssignment | None = None,
    exc_info: bool = False,
    emit_log: bool = True,
) -> BatchTaskState:
    """Log and persist an attempt failure state."""

    if emit_log:
        message = "batch task attempt timed out" if status == BatchTaskStatus.TIMED_OUT else "batch task attempt failed"
        logger.warning(message, extra={"task_id": task.task_id, "attempt": attempt}, exc_info=exc_info)
    failure_category = _failure_category_for_attempt(state=state, status=status, error=error)
    failure_details: dict[str, object] = {
        "failure_category": failure_category.value if failure_category is not None else "",
        "error": _short_error(str(error)),
        "status_before_failure": state.status.value,
    }
    capability_evidence = guest_capability_evidence_payloads(str(error))
    if capability_evidence:
        failure_details["guest_capability_evidence"] = capability_evidence
    if prepared_assignment is not None:
        failure_details.update(_prepared_assignment_failure_details(prepared_assignment, error=str(error)))
    failed = state.model_copy(
        update={
            "status": status,
            "error": str(error),
            "last_failure_category": failure_category,
        }
    )
    await events.upsert_task_async(failed)
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.ATTEMPT_FAILED,
        phase=_phase_for_attempt_failure(failure_category),
        status=CaseStatus.TIMED_OUT if status == BatchTaskStatus.TIMED_OUT else CaseStatus.FAILED,
        attempt=attempt,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=state.controller_task_id or state.sandbox_id or task.task_id,
        details=failure_details,
        controller_task_id=state.controller_task_id,
        trace_id=state.trace_id,
        sandbox_id=state.sandbox_id,
        device_id=state.device_id,
    )
    return failed


def _phase_for_attempt_failure(failure_category: object) -> CasePhase:
    if failure_category is None:
        return CasePhase.COMPLETED
    value = getattr(failure_category, "value", "")
    if value == "controller_submit":
        return CasePhase.CONTROLLER_SUBMIT
    if value == "readiness_timeout":
        return CasePhase.WORKER_READY
    if value == "startup_command":
        return CasePhase.STARTUP_COMMANDS
    if value in {"device_transport", "device_command_timeout"}:
        return CasePhase.MAIN_AGENT
    if value == "sandbox_create":
        return CasePhase.SANDBOX_CREATE
    return CasePhase.COMPLETED


def _short_error(error: str, *, max_length: int = 500) -> str:
    return error if len(error) <= max_length else error[: max_length - 3] + "..."


def _case_status_for_batch_task(status: BatchTaskStatus) -> CaseStatus:
    if status == BatchTaskStatus.SUCCEEDED:
        return CaseStatus.SUCCEEDED
    if status == BatchTaskStatus.TIMED_OUT:
        return CaseStatus.TIMED_OUT
    if status == BatchTaskStatus.CANCELED:
        return CaseStatus.CANCELED
    if status == BatchTaskStatus.FAILED:
        return CaseStatus.FAILED
    if status == BatchTaskStatus.EVALUATION_PENDING:
        return CaseStatus.EVALUATING
    return CaseStatus.FINALIZING


def _should_defer_controller_reconcile_cleanup(state: BatchTaskState) -> bool:
    if not state.controller_task_id:
        return False
    if state.cleanup_status == _RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS:
        return False
    if state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
        return True
    return state.last_reconciled_status == "unknown"


async def _run_pre_submit_commands(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    lease: SandboxLease | None,
    lease_context: BatchTaskLeaseContext | None,
    attempt: int,
    events: BatchEventWriter,
) -> None:
    """Run sandbox commands after device readiness and before agent submit."""

    commands = [*config.manifest.defaults.pre_submit_commands, *task.pre_submit_commands]
    if config.dry_run or not commands or lease is None or lease_context is None:
        return
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.PRE_SUBMIT_COMMANDS_STARTED,
        phase=CasePhase.WORKER_READY,
        status=CaseStatus.BOOTSTRAPPING,
        attempt=attempt,
        entity_kind=TelemetryEntityKind.COMMAND,
        entity_id=task.task_id,
        details={"command_count": len(commands), "labels": [_command_label(command) for command in commands]},
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    results: list[dict[str, object]] = []
    started = time.monotonic()
    try:
        for command in commands:
            result = await lease_context.manager.run_command(lease, command)
            result_summary = _command_result_summary(command=command, result=result)
            results.append(result_summary)
            if not result.ok and not command.allow_failure:
                raise PreSubmitCommandFailure(_pre_submit_command_failure_message(command, result))
    finally:
        failed = any(not result.get("ok", False) and not result.get("allow_failure", False) for result in results)
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.PRE_SUBMIT_COMMANDS_FINISHED,
            phase=CasePhase.WORKER_READY,
            status=CaseStatus.FAILED if failed else CaseStatus.BOOTSTRAPPING,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.COMMAND,
            entity_id=task.task_id,
            details={
                "command_count": len(commands),
                "completed_count": len(results),
                "duration_seconds": round(time.monotonic() - started, 3),
                "results": results,
            },
            sandbox_id=lease.handle.sandbox_id,
            device_id=lease.device_id,
        )


def _command_result_summary(*, command: SandboxCommandSpec, result: SandboxCommandResult) -> dict[str, object]:
    return {
        "label": _command_label(command),
        "ok": result.ok,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "allow_failure": command.allow_failure,
        "stdout_tail": _redacted_tail(result.stdout, command.redact),
        "stderr_tail": _redacted_tail(result.stderr, command.redact),
    }


def _pre_submit_command_failure_message(command: SandboxCommandSpec, result: SandboxCommandResult) -> str:
    return (
        "pre-submit command failed: "
        f"label={_command_label(command)!r} "
        f"exit_code={result.exit_code} "
        f"timed_out={result.timed_out} "
        f"stderr_tail={_redacted_tail(result.stderr, command.redact)!r} "
        f"stdout_tail={_redacted_tail(result.stdout, command.redact)!r}"
    )


def _command_label(command: SandboxCommandSpec) -> str:
    return command.label or "<unlabeled>"


def _redacted_tail(value: str, redactions: list[str], *, limit: int = 1000) -> str:
    stripped = value.strip()
    text = stripped if len(stripped) <= limit else stripped[-limit:]
    for secret in redactions:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


async def _run_post_run_hooks(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    state: BatchTaskState,
    lease: SandboxLease | None,
    lease_context: BatchTaskLeaseContext | None,
    output_dir: Path,
    attempt: int,
    events: BatchEventWriter,
) -> BatchTaskState:
    """Run configured benchmark hooks without changing controller status."""

    if (
        config.dry_run
        or not task.post_run_hooks
        or lease is None
        or lease_context is None
        or state.terminal_state is None
    ):
        return state
    results: list[PostRunHookResult] | None = None
    try:
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.POST_RUN_HOOK_STARTED,
            phase=CasePhase.EVALUATOR,
            status=CaseStatus.EVALUATING,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.EVALUATOR,
            entity_id=task.task_id,
            details={"hook_count": len(task.post_run_hooks)},
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            sandbox_id=lease.handle.sandbox_id,
            device_id=lease.device_id,
        )
        with events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.EVALUATOR,
            status=CaseStatus.EVALUATING,
            attempt=attempt,
            entity_kind=TelemetryEntityKind.EVALUATOR,
            entity_id=task.task_id,
        ):
            results = await PostRunHookRunner(output_dir=output_dir).run_all(
                hooks=_with_terminal_decision_inputs(
                    task.post_run_hooks,
                    state=state,
                    output_dir=output_dir,
                    task_id=task.task_id,
                    attempt=attempt,
                ),
                manager=lease_context.manager,
                lease=lease,
                task_id=task.task_id,
                attempt=attempt,
            )
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.POST_RUN_HOOK_FINISHED,
            phase=CasePhase.EVALUATOR,
            status=_case_status_for_batch_task(state.status),
            attempt=attempt,
            entity_kind=TelemetryEntityKind.EVALUATOR,
            entity_id=task.task_id,
            details=_post_run_hook_event_details(results),
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            sandbox_id=lease.handle.sandbox_id,
            device_id=lease.device_id,
        )
        return state.model_copy(update={"post_run_results": results})
    except Exception as exc:  # noqa: BLE001 - hook infra must not rewrite controller terminal status.
        logger.warning(
            "post-run hook infrastructure failed; preserving controller terminal status",
            extra={"task_id": task.task_id, "attempt": attempt, "status": state.status.value},
            exc_info=True,
        )
        error_results = (
            _post_run_results_with_infra_error(results=results, error=exc)
            if results is not None
            else _post_run_infra_error_results(hooks=task.post_run_hooks, error=exc)
        )
        _safe_post_run_hook_finished_event(
            events=events,
            task=task,
            state=state,
            lease=lease,
            attempt=attempt,
            results=error_results,
            error=exc,
        )
        return state.model_copy(update={"post_run_results": error_results})


def _with_terminal_decision_inputs(
    hooks: list[PostRunHookSpec],
    *,
    state: BatchTaskState,
    output_dir: Path,
    task_id: str,
    attempt: int,
) -> list[PostRunHookSpec]:
    """Snapshot the source terminal decision for evaluators that request it."""

    projected: list[PostRunHookSpec] = []
    for hook in hooks:
        remote = hook.terminal_decision_remote_path
        if not remote:
            projected.append(hook)
            continue
        local = output_dir / "post-run-inputs" / task_id / f"attempt-{attempt}" / "terminal-decision.json"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(
            json.dumps(
                {
                    "schema_version": "northcua.batch_terminal_decision/v1",
                    "task_id": task_id,
                    "attempt": attempt,
                    "terminal_decision": state.terminal_decision,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        upload = SandboxFileUploadSpec(local_path=local, remote_path=remote, visibility="evaluator")
        projected.append(hook.model_copy(update={"file_uploads": [*hook.file_uploads, upload]}))
    return projected


def _post_run_hook_event_details(
    results: list[PostRunHookResult], *, error: Exception | None = None
) -> dict[str, object]:
    details: dict[str, object] = {
        "hook_count": len(results),
        "passed_count": sum(1 for result in results if result.status == PostRunHookStatus.PASSED),
        "failed_count": sum(1 for result in results if result.status == PostRunHookStatus.FAILED),
        "error_count": sum(
            1 for result in results if result.status in {PostRunHookStatus.ERROR, PostRunHookStatus.TIMED_OUT}
        ),
        "results": [result.model_dump(mode="json") for result in results],
    }
    if error is not None:
        details["infra_error"] = _short_error(str(error))
        details["infra_error_type"] = type(error).__name__
    return details


def _post_run_infra_error_results(*, hooks: list[PostRunHookSpec], error: Exception) -> list[PostRunHookResult]:
    return [
        PostRunHookResult(
            hook_id=hook.hook_id,
            kind=hook.kind,
            label=hook.label,
            status=PostRunHookStatus.ERROR,
            passed=None,
            score_threshold=hook.score_threshold,
            error=_short_error(str(error)),
            metadata={
                **hook.metadata,
                "infra_error": True,
                "infra_error_type": type(error).__name__,
            },
        )
        for hook in hooks
    ]


def _post_run_results_with_infra_error(
    *,
    results: list[PostRunHookResult],
    error: Exception,
) -> list[PostRunHookResult]:
    return [
        result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "infra_error": True,
                    "infra_error_type": type(error).__name__,
                    "infra_error_message": _short_error(str(error)),
                }
            }
        )
        for result in results
    ]


def _safe_post_run_hook_finished_event(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    state: BatchTaskState,
    lease: SandboxLease,
    attempt: int,
    results: list[PostRunHookResult],
    error: Exception,
) -> None:
    try:
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.POST_RUN_HOOK_FINISHED,
            phase=CasePhase.EVALUATOR,
            status=_case_status_for_batch_task(state.status),
            attempt=attempt,
            entity_kind=TelemetryEntityKind.EVALUATOR,
            entity_id=task.task_id,
            details=_post_run_hook_event_details(results, error=error),
            controller_task_id=state.controller_task_id,
            trace_id=state.trace_id,
            sandbox_id=lease.handle.sandbox_id,
            device_id=lease.device_id,
        )
    except Exception:  # noqa: BLE001 - event sinks are best effort after hook infra failure.
        logger.warning(
            "failed to record post-run hook infrastructure failure event",
            extra={"task_id": task.task_id, "attempt": attempt},
            exc_info=True,
        )


async def _release_task_lease(
    *,
    coordinator: BatchTaskLeaseCoordinator,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    state: BatchTaskState,
    lease: SandboxLease | None,
    lease_context: BatchTaskLeaseContext | None,
    output_dir: Path,
    attempt: int,
) -> None:
    """Release sandbox capacity after a task attempt."""

    if config.dry_run or lease is None:
        return
    if lease_context is not None:
        await coordinator.release(
            config=config,
            task=task,
            state=state,
            context=lease_context,
            output_dir=output_dir,
            attempt=attempt,
        )
