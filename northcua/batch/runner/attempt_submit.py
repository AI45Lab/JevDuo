"""Controller submission projection for one batch task attempt."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.evaluator_outcome import (
    evaluator_accepts_uncontrollable_result,
    evaluator_is_pending_from_terminal,
    evaluator_report_is_present,
    evaluator_terminal_metadata,
    evaluator_verdict_is_success,
)
from northcua.batch.metrics import BatchMetrics
from northcua.batch.planner import BatchTaskPlanner, PlannedTaskSubmission
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.runner.attempt_reconcile import (
    _case_status_from_terminal,
    _last_failure_category,
    _local_duplicate_statuses,
    _local_status_from_controller_terminal,
    _short_error,
    _state_from_existing_controller_task_after_submit_timeout,
    _state_from_existing_controller_task_if_duplicate,
)
from northcua.batch.runner.controller import ControllerTaskTimeoutError, ControllerTaskWatcher
from northcua.batch.runner.events import BatchAttemptEventMessage, BatchEventWriter
from northcua.sandboxes.contracts import SandboxLease
from northcua.telemetry.contracts import CasePhase, CaseStatus, FailureCategory, TelemetryEntityKind
from northcua.telemetry.failure_categories import failure_category_from_message, failure_category_from_terminal_decision
from northcua_protocol.payloads import TaskAccepted, TaskFinished

logger = logging.getLogger(__name__)
_StopRequested = Callable[[], bool | Awaitable[bool]]


def _elapsed_since(start: float | None, *, end: float | None = None) -> float | None:
    if start is None:
        return None
    return round((end if end is not None else time.monotonic()) - start, 3)


def _prepared_assignment_terminal_details(
    assignment: PreparedSandboxAssignment | None,
    *,
    failure_category: FailureCategory | None,
) -> dict[str, object]:
    if assignment is None:
        return {}
    now = time.time()
    details: dict[str, object] = {
        "prepared_sandbox_ttl_seconds": round(assignment.prepared_ttl_seconds(), 3),
        "remaining_ttl_seconds_at_failure": round(assignment.remaining_ttl_seconds(now=now), 3),
        "prepared_age_seconds_at_failure": round(assignment.prepared_age_seconds(now=now), 3),
        "prepared_to_assigned_seconds": round(assignment.prepared_to_assigned_seconds(), 3),
        "assigned_runtime_seconds": round(assignment.assigned_runtime_seconds(now=now), 3),
        "expired_before_assignment": assignment.expired_before_assignment(),
        "expired_after_assignment": assignment.expired_after_assignment(now=now),
        "prepared_sandbox_expires_at": assignment.record.expires_at,
        "prepared_sandbox_assigned_at": assignment.record.assigned_at,
    }
    if failure_category == FailureCategory.DEVICE_TRANSPORT:
        details["assigned_runtime_seconds_before_disconnect"] = round(assignment.assigned_runtime_seconds(now=now), 3)
    return details


def _planned_submission_for_attempt(
    *,
    planner: BatchTaskPlanner,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
) -> PlannedTaskSubmission:
    return planner.plan(
        config.manifest,
        task,
        device_id=lease.device_id,
        sandbox_id=lease.handle.sandbox_id,
        attempt=state.attempts,
    )


async def _wait_for_controller_backpressure(
    *,
    backpressure_gate: ControllerBackpressureGate | None,
    stop_requested: _StopRequested | None,
) -> bool:
    if backpressure_gate is None:
        return True
    return await backpressure_gate.wait_if_needed(scope="submit", stop_requested=stop_requested)


def _canceled_before_controller_submit_state(state: BatchTaskState) -> BatchTaskState:
    return state.model_copy(
        update={
            "status": BatchTaskStatus.CANCELED,
            "controller_task_id": "",
            "trace_id": "",
            "error": "batch canceled before controller submit while waiting for controller backpressure",
        }
    )


def _submitted_state(state: BatchTaskState, planned: PlannedTaskSubmission) -> BatchTaskState:
    return state.model_copy(
        update={
            "controller_task_id": planned.controller_task_id,
            "trace_id": planned.trace_id,
            "status": BatchTaskStatus.SUBMITTED,
        }
    )


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


def _backpressure_snapshot_details(backpressure_gate: ControllerBackpressureGate | None) -> dict[str, object]:
    if backpressure_gate is None:
        return {}
    snapshot = backpressure_gate.cached_snapshot()
    return {
        "active": snapshot.active,
        "reason": snapshot.reason,
        "samples": snapshot.samples,
        "event_loop_lag_ms": snapshot.latest_event_loop_lag_ms,
        "recv_q_max": snapshot.latest_recv_q_max,
        "sample_duration_seconds": snapshot.sample_duration_seconds,
        "sample_timed_out": snapshot.sample_timed_out,
        "sample_error": snapshot.sample_error,
    }


def _effective_submit_jitter_seconds(
    requested_seconds: float,
    *,
    backpressure_gate: ControllerBackpressureGate | None,
    backpressure_wait_seconds: float,
) -> tuple[float, str]:
    requested = max(requested_seconds, 0.0)
    if requested <= 0:
        return 0.0, "disabled"
    recovery_window_seconds = _env_float("NORTHCUA_BATCH_SUBMIT_JITTER_RECOVERY_WINDOW_SECONDS", 300.0)
    normal_cap_seconds = _env_float("NORTHCUA_BATCH_SUBMIT_JITTER_NORMAL_CAP_SECONDS", 30.0)
    recovery_cap_seconds = _env_float("NORTHCUA_BATCH_SUBMIT_JITTER_RECOVERY_CAP_SECONDS", 60.0)
    recently_backpressured = bool(
        backpressure_gate is not None
        and callable(getattr(backpressure_gate, "recently_active", None))
        and backpressure_gate.recently_active(window_seconds=recovery_window_seconds)
    )
    recovered_from_wait = backpressure_wait_seconds >= 1.0
    if recently_backpressured or recovered_from_wait:
        cap = recovery_cap_seconds
        mode = "recovery_cap"
    else:
        cap = normal_cap_seconds
        mode = "normal_cap"
    if cap < 0:
        return requested, "uncapped"
    return min(requested, max(cap, 0.0)), mode


def _emit_submit_preflight_started(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    ready_to_backpressure_gate_seconds: float | None,
    backpressure_gate: ControllerBackpressureGate | None,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_PREFLIGHT_STARTED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "ready_to_backpressure_gate_seconds": ready_to_backpressure_gate_seconds,
            "backpressure_gate_present": backpressure_gate is not None,
            "backpressure_snapshot": _backpressure_snapshot_details(backpressure_gate),
            "submit_semaphore": submit_semaphore is not None,
            "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
            "submit_jitter_seconds": config.controller_submit_jitter_seconds,
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


def _emit_submit_preflight_finished(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    ready_for_controller_submit_at: float | None,
    ready_to_backpressure_gate_seconds: float | None,
    backpressure_wait_started_at: float,
    backpressure_wait_finished_at: float,
    upsert_started_at: float,
    upsert_finished_at: float,
    backpressure_gate: ControllerBackpressureGate | None,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
    effective_submit_jitter_seconds: float,
    submit_jitter_mode: str,
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_PREFLIGHT_FINISHED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "ready_to_backpressure_gate_seconds": ready_to_backpressure_gate_seconds,
            "ready_to_after_backpressure_seconds": _elapsed_since(
                ready_for_controller_submit_at,
                end=backpressure_wait_finished_at,
            ),
            "backpressure_wait_seconds": round(backpressure_wait_finished_at - backpressure_wait_started_at, 3),
            "backpressure_gate_present": backpressure_gate is not None,
            "backpressure_snapshot": _backpressure_snapshot_details(backpressure_gate),
            "upsert_task_submitted_seconds": round(upsert_finished_at - upsert_started_at, 3),
            "submit_semaphore": submit_semaphore is not None,
            "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
            "effective_submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_mode": submit_jitter_mode,
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


def _emit_submit_started(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    timeout: float | None,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
    effective_submit_jitter_seconds: float,
    submit_jitter_mode: str,
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_STARTED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "timeout_seconds": timeout or 0,
            "submit_semaphore": submit_semaphore is not None,
            "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
            "effective_submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_mode": submit_jitter_mode,
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


def _emit_submit_open_started(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
    effective_submit_jitter_seconds: float,
    submit_jitter_mode: str,
    open_details: dict[str, object] | None = None,
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_OPEN_STARTED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "submit_semaphore": submit_semaphore is not None,
            "submit_semaphore_value": getattr(submit_semaphore, "_value", None),
            "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
            "effective_submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_mode": submit_jitter_mode,
            **(open_details or {}),
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


def _emit_submit_request_sent(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
    effective_submit_jitter_seconds: float,
    submit_jitter_mode: str,
    request_details: dict[str, object] | None = None,
) -> None:
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_REQUEST_SENT,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.RUNNING_AGENT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=planned.controller_task_id,
        details={
            "submit_semaphore": submit_semaphore is not None,
            "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
            "effective_submit_jitter_seconds": effective_submit_jitter_seconds,
            "submit_jitter_mode": submit_jitter_mode,
            **(request_details or {}),
        },
        controller_task_id=planned.controller_task_id,
        trace_id=planned.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )


def _accepted_event_sink(
    *,
    events: BatchEventWriter,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    planned: PlannedTaskSubmission,
    submit_semaphore: asyncio.Semaphore | None,
    config: BatchRunConfig,
    effective_submit_jitter_seconds: float,
    submit_jitter_mode: str,
) -> Callable[[TaskAccepted, float], None]:
    def _emit(accepted: TaskAccepted, elapsed: float) -> None:
        events.attempt_event(
            case_id=task.task_id,
            message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_ACCEPTED,
            phase=CasePhase.CONTROLLER_SUBMIT,
            status=CaseStatus.RUNNING_AGENT,
            attempt=state.attempts,
            entity_kind=TelemetryEntityKind.TASK,
            entity_id=accepted.task_id,
            details={
                "handshake_elapsed_seconds": round(elapsed, 3),
                "controller_task_id": accepted.task_id,
                "trace_id": accepted.trace_id,
                "session_id": accepted.session_id,
                "target_device_id": planned.target_device_id,
                "submit_semaphore": submit_semaphore is not None,
                "requested_submit_jitter_seconds": config.controller_submit_jitter_seconds,
                "effective_submit_jitter_seconds": effective_submit_jitter_seconds,
                "submit_jitter_seconds": effective_submit_jitter_seconds,
                "submit_jitter_mode": submit_jitter_mode,
            },
            controller_task_id=accepted.task_id,
            trace_id=accepted.trace_id,
            sandbox_id=lease.handle.sandbox_id,
            device_id=lease.device_id,
        )

    return _emit


async def submit_to_controller(
    *,
    planner: BatchTaskPlanner,
    watcher: ControllerTaskWatcher,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    lease: SandboxLease,
    controller_url: str,
    token: str,
    metrics: BatchMetrics,
    state: BatchTaskState,
    events: BatchEventWriter,
    submit_semaphore: asyncio.Semaphore | None = None,
    backpressure_gate: ControllerBackpressureGate | None = None,
    backpressure_stop_requested: _StopRequested | None = None,
    ready_for_controller_submit_at: float | None = None,
    prepared_assignment: PreparedSandboxAssignment | None = None,
    before_submit: Callable[[], Awaitable[None]] | None = None,
) -> BatchTaskState:
    """Submit a prepared task and persist deterministic controller ids.

    Args:
        planner: Planner that projects manifest tasks into controller requests.
        watcher: Controller client that submits and waits for terminal status.
        config: Batch run configuration with manifest defaults and controller
            endpoint settings.
        task: Manifest task being submitted.
        lease: Sandbox/device lease bound to this attempt.
        controller_url: Public controller URL used by the submitter.
        token: Controller authentication token.
        metrics: Mutable batch metrics accumulator.
        state: Current task state before controller submission.
        events: State/audit writer used as the local source of truth facade.
        submit_semaphore: Optional gate for controller submit websocket
            opening and TASK_ACCEPTED handshake.
        backpressure_gate: Optional controller pressure sampler used before
            opening the submit websocket.
        backpressure_stop_requested: Optional cancellation predicate for
            backpressure waits.
        ready_for_controller_submit_at: Monotonic timestamp captured when the
            sandbox/device was ready for controller submission.
        prepared_assignment: Optional prepared sandbox assignment context for
            TTL and device-transport diagnostics.
        before_submit: Optional awaitable callback invoked inside the submit
            gate immediately before opening the controller submit websocket.

    Returns:
        Updated task state projected from the controller terminal payload.
    """

    return await _submit_to_controller_impl(
        planner=planner,
        watcher=watcher,
        config=config,
        task=task,
        lease=lease,
        controller_url=controller_url,
        token=token,
        metrics=metrics,
        state=state,
        events=events,
        submit_semaphore=submit_semaphore,
        backpressure_gate=backpressure_gate,
        backpressure_stop_requested=backpressure_stop_requested,
        ready_for_controller_submit_at=ready_for_controller_submit_at,
        prepared_assignment=prepared_assignment,
        before_submit=before_submit,
    )


async def _submit_to_controller_impl(
    *,
    planner: BatchTaskPlanner,
    watcher: ControllerTaskWatcher,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    lease: SandboxLease,
    controller_url: str,
    token: str,
    metrics: BatchMetrics,
    state: BatchTaskState,
    events: BatchEventWriter,
    submit_semaphore: asyncio.Semaphore | None = None,
    backpressure_gate: ControllerBackpressureGate | None = None,
    backpressure_stop_requested: _StopRequested | None = None,
    ready_for_controller_submit_at: float | None = None,
    prepared_assignment: PreparedSandboxAssignment | None = None,
    before_submit: Callable[[], Awaitable[None]] | None = None,
) -> BatchTaskState:
    """Submit a prepared task and persist deterministic controller ids.

    Args:
        planner: Planner that projects manifest tasks into controller requests.
        watcher: Controller client that submits and waits for terminal status.
        config: Batch run configuration with manifest defaults and controller
            endpoint settings.
        task: Manifest task being submitted.
        lease: Sandbox/device lease bound to this attempt.
        controller_url: Public controller URL used by the submitter.
        token: Controller authentication token.
        metrics: Mutable batch metrics accumulator.
        state: Current task state before controller submission.
        events: State/audit writer used as the local source of truth facade.
        submit_semaphore: Optional gate for controller submit websocket
            opening and TASK_ACCEPTED handshake.
        ready_for_controller_submit_at: Monotonic timestamp captured when the
            sandbox/device was ready for controller submission.
        prepared_assignment: Optional prepared sandbox assignment context for
            TTL and device-transport diagnostics.
        before_submit: Optional awaitable callback invoked inside the submit
            gate immediately before opening the controller submit websocket.

    Returns:
        Updated task state projected from the controller terminal payload.
    """

    planned = _planned_submission_for_attempt(
        planner=planner,
        config=config,
        task=task,
        lease=lease,
        state=state,
    )
    backpressure_wait_started_at = time.monotonic()
    ready_to_backpressure_gate_seconds = _elapsed_since(
        ready_for_controller_submit_at,
        end=backpressure_wait_started_at,
    )
    _emit_submit_preflight_started(
        events=events,
        task=task,
        lease=lease,
        state=state,
        planned=planned,
        ready_to_backpressure_gate_seconds=ready_to_backpressure_gate_seconds,
        backpressure_gate=backpressure_gate,
        submit_semaphore=submit_semaphore,
        config=config,
    )
    proceed = await _wait_for_controller_backpressure(
        backpressure_gate=backpressure_gate,
        stop_requested=backpressure_stop_requested,
    )
    if not proceed:
        logger.info(
            "controller backpressure submit wait stopped by cancel flag",
            extra={"task_id": task.task_id, "attempt": state.attempts},
        )
        return _canceled_before_controller_submit_state(state)
    backpressure_wait_finished_at = time.monotonic()
    backpressure_wait_seconds = round(backpressure_wait_finished_at - backpressure_wait_started_at, 3)
    effective_submit_jitter, submit_jitter_mode = _effective_submit_jitter_seconds(
        config.controller_submit_jitter_seconds,
        backpressure_gate=backpressure_gate,
        backpressure_wait_seconds=backpressure_wait_seconds,
    )
    state = _submitted_state(state, planned)
    upsert_started_at = time.monotonic()
    submitted_write_accepted = await events.upsert_task_async(state)
    upsert_finished_at = time.monotonic()
    if not submitted_write_accepted:
        logger.info(
            "controller submit skipped because local submitted state write was rejected",
            extra={"task_id": task.task_id, "attempt": state.attempts},
        )
        return _canceled_before_controller_submit_state(state)
    _emit_submit_preflight_finished(
        events=events,
        task=task,
        lease=lease,
        state=state,
        planned=planned,
        ready_for_controller_submit_at=ready_for_controller_submit_at,
        ready_to_backpressure_gate_seconds=ready_to_backpressure_gate_seconds,
        backpressure_wait_started_at=backpressure_wait_started_at,
        backpressure_wait_finished_at=backpressure_wait_finished_at,
        upsert_started_at=upsert_started_at,
        upsert_finished_at=upsert_finished_at,
        backpressure_gate=backpressure_gate,
        submit_semaphore=submit_semaphore,
        config=config,
        effective_submit_jitter_seconds=effective_submit_jitter,
        submit_jitter_mode=submit_jitter_mode,
    )
    task_start = time.monotonic()
    timeout = task.timeout_seconds or config.manifest.defaults.timeout_seconds or None
    _emit_submit_started(
        events=events,
        task=task,
        lease=lease,
        state=state,
        planned=planned,
        timeout=timeout,
        submit_semaphore=submit_semaphore,
        config=config,
        effective_submit_jitter_seconds=effective_submit_jitter,
        submit_jitter_mode=submit_jitter_mode,
    )

    async def _on_submit_open_ready(open_details: dict[str, object]) -> None:
        before_submit_elapsed = 0.0
        if before_submit is not None:
            before_submit_started_at = time.monotonic()
            await before_submit()
            before_submit_elapsed = time.monotonic() - before_submit_started_at
        updated_details = dict(open_details)
        updated_details["submit_started_to_open_started_seconds"] = _elapsed_since(task_start)
        updated_details["before_submit_elapsed_seconds"] = round(before_submit_elapsed, 3)
        if before_submit is not None:
            updated_details["before_submit_callback_present"] = True
        _emit_submit_open_started(
            events=events,
            task=task,
            lease=lease,
            state=state,
            planned=planned,
            submit_semaphore=submit_semaphore,
            config=config,
            effective_submit_jitter_seconds=effective_submit_jitter,
            submit_jitter_mode=submit_jitter_mode,
            open_details=updated_details,
        )

    def _on_request_sent(request_details: dict[str, object]) -> None:
        _emit_submit_request_sent(
            events=events,
            task=task,
            lease=lease,
            state=state,
            planned=planned,
            submit_semaphore=submit_semaphore,
            config=config,
            effective_submit_jitter_seconds=effective_submit_jitter,
            submit_jitter_mode=submit_jitter_mode,
            request_details=request_details,
        )

    try:
        accepted, finished = await watcher.submit_and_wait(
            controller_url=controller_url,
            token=token,
            planned=planned,
            timeout_seconds=timeout,
            submit_semaphore=submit_semaphore,
            submit_jitter_seconds=effective_submit_jitter,
            on_submit_open_ready=_on_submit_open_ready,
            on_request_sent=_on_request_sent,
            on_accepted=_accepted_event_sink(
                events=events,
                task=task,
                lease=lease,
                state=state,
                planned=planned,
                submit_semaphore=submit_semaphore,
                config=config,
                effective_submit_jitter_seconds=effective_submit_jitter,
                submit_jitter_mode=submit_jitter_mode,
            ),
        )
    except ControllerTaskTimeoutError as exc:
        return _timeout_state(
            exc=exc,
            task=task,
            lease=lease,
            state=state,
            metrics=metrics,
            events=events,
            task_start=task_start,
        )
    except Exception as exc:
        timeout_reconciled_state = await _state_from_existing_controller_task_after_submit_timeout(
            exc=exc,
            config=config,
            planned=planned,
            task=task,
            lease=lease,
            state=state,
            metrics=metrics,
            events=events,
            task_start=task_start,
            timeout_seconds=timeout,
        )
        if timeout_reconciled_state is not None:
            return timeout_reconciled_state
        duplicate_state = await _state_from_existing_controller_task_if_duplicate(
            exc=exc,
            watcher=watcher,
            controller_url=controller_url,
            token=token,
            config=config,
            planned=planned,
            task=task,
            lease=lease,
            state=state,
            metrics=metrics,
            events=events,
            task_start=task_start,
        )
        if duplicate_state is not None:
            return duplicate_state
        logger.debug("controller submit raised before terminal projection", exc_info=True)
        return _pre_terminal_failure_state(
            exc=exc,
            task=task,
            lease=lease,
            state=state,
            metrics=metrics,
            events=events,
            task_start=task_start,
        )
    return _state_from_terminal_controller_result(
        accepted=accepted,
        finished=finished,
        task=task,
        lease=lease,
        state=state,
        metrics=metrics,
        events=events,
        task_start=task_start,
        prepared_assignment=prepared_assignment,
    )


def _state_from_terminal_controller_result(
    *,
    accepted: TaskAccepted,
    finished: TaskFinished,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
    prepared_assignment: PreparedSandboxAssignment | None = None,
) -> BatchTaskState:
    elapsed_seconds = time.monotonic() - task_start
    metrics.observe_task(elapsed_seconds)
    evaluator_report_present = evaluator_report_is_present(finished.result)
    evaluator_success = evaluator_verdict_is_success(finished.result)
    evaluator_accepted_infeasible = evaluator_accepts_uncontrollable_result(finished.result)
    evaluator_pending = evaluator_is_pending_from_terminal(finished.terminal_decision)
    evaluator_metadata = evaluator_terminal_metadata(finished.terminal_decision)
    terminal_failure_category = failure_category_from_terminal_decision(finished.terminal_decision)
    last_failure_category = _last_failure_category(finished.state, finished.error or "", terminal_failure_category)
    if evaluator_success:
        last_failure_category = FailureCategory.ACCEPTED_INFEASIBLE if evaluator_accepted_infeasible else None
    terminal_status = _local_status_from_controller_terminal(
        finished.state,
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
        _log_device_connectivity_failure(
            task=task,
            attempt=state.attempts,
            controller_task_id=accepted.task_id,
            trace_id=accepted.trace_id,
            failure_category=last_failure_category,
            error=finished.error or "",
        )
    details = {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "terminal_state": finished.state.value,
        "terminal_status": terminal_status.value,
        "failure_category": last_failure_category.value if last_failure_category is not None else "",
        "error": _short_error(finished.error or ""),
        "evaluator_accepted_infeasible": evaluator_accepted_infeasible,
        "evaluation_status": evaluator_metadata.get("status", ""),
        "evaluation_projection_level": evaluator_metadata.get("projection_level", ""),
        "evaluation_attempts": evaluator_metadata.get("attempts", 0),
        **(
            {"device_transport_after_submit_seconds": round(elapsed_seconds, 3)}
            if last_failure_category == FailureCategory.DEVICE_TRANSPORT
            else {}
        ),
        **_prepared_assignment_terminal_details(prepared_assignment, failure_category=last_failure_category),
    }
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_FINISHED,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=_case_status_from_terminal(terminal_status),
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=accepted.task_id,
        details=details,
        controller_task_id=accepted.task_id,
        trace_id=accepted.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state.model_copy(
        update={
            "controller_task_id": accepted.task_id,
            "trace_id": accepted.trace_id,
            "session_id": accepted.session_id,
            "status": terminal_status,
            "terminal_state": finished.state,
            "error": (
                ""
                if terminal_status in {BatchTaskStatus.SUCCEEDED, BatchTaskStatus.EVALUATION_PENDING}
                else finished.error or ""
            ),
            "last_failure_category": last_failure_category,
            "terminal_decision": finished.terminal_decision,
            "agent_terminal": _agent_terminal_from_finished(finished),
            "evaluation_status": str(evaluator_metadata.get("status") or ""),
            "evaluation_error": evaluator_metadata,
            "evaluation_attempts": _int_value(evaluator_metadata.get("attempts")),
            "evaluation_projection_level": str(evaluator_metadata.get("projection_level") or ""),
        }
    )


def _agent_terminal_from_finished(finished: TaskFinished) -> dict[str, object]:
    return {
        "state": finished.state.value,
        "result": _short_error(str(finished.result or ""), max_length=1000),
        "terminal_decision": finished.terminal_decision,
    }


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _timeout_state(
    *,
    exc: ControllerTaskTimeoutError,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
) -> BatchTaskState:
    elapsed_seconds = time.monotonic() - task_start
    metrics.observe_task(elapsed_seconds)
    metrics.observe_attempt_failure(FailureCategory.TIMEOUT)
    finished = exc.finished
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_TIMED_OUT,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.TIMED_OUT,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=exc.accepted.task_id,
        details={
            "elapsed_seconds": round(elapsed_seconds, 3),
            "terminal_state": finished.state.value if finished is not None else "",
            "failure_category": FailureCategory.TIMEOUT.value,
            "error": _short_error(str(exc)),
        },
        controller_task_id=exc.accepted.task_id,
        trace_id=exc.accepted.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state.model_copy(
        update={
            "controller_task_id": exc.accepted.task_id,
            "trace_id": exc.accepted.trace_id,
            "session_id": exc.accepted.session_id,
            "status": BatchTaskStatus.TIMED_OUT,
            "terminal_state": finished.state if finished is not None else None,
            "error": str(exc),
            "last_failure_category": FailureCategory.TIMEOUT,
            "terminal_decision": finished.terminal_decision if finished is not None else {},
        }
    )


def _pre_terminal_failure_state(
    *,
    exc: Exception,
    task: BatchTaskSpec,
    lease: SandboxLease,
    state: BatchTaskState,
    metrics: BatchMetrics,
    events: BatchEventWriter,
    task_start: float,
) -> BatchTaskState:
    elapsed_seconds = time.monotonic() - task_start
    metrics.observe_task(elapsed_seconds)
    last_failure_category = failure_category_from_message(str(exc)) or FailureCategory.CONTROLLER_SUBMIT
    metrics.observe_attempt_failure(last_failure_category)
    logger.warning(
        "controller task failed before terminal result",
        extra={
            "task_id": task.task_id,
            "attempt": state.attempts,
            "controller_task_id": state.controller_task_id,
            "trace_id": state.trace_id,
        },
        exc_info=True,
    )
    events.attempt_event(
        case_id=task.task_id,
        message=BatchAttemptEventMessage.CONTROLLER_SUBMIT_FAILED_BEFORE_TERMINAL,
        phase=CasePhase.CONTROLLER_SUBMIT,
        status=CaseStatus.FAILED,
        attempt=state.attempts,
        entity_kind=TelemetryEntityKind.TASK,
        entity_id=state.controller_task_id or task.task_id,
        details={
            "elapsed_seconds": round(elapsed_seconds, 3),
            "failure_category": last_failure_category.value,
            "error_class": type(exc).__name__,
            "error": _short_error(str(exc)),
        },
        controller_task_id=state.controller_task_id,
        trace_id=state.trace_id,
        sandbox_id=lease.handle.sandbox_id,
        device_id=lease.device_id,
    )
    return state.model_copy(
        update={
            "status": BatchTaskStatus.FAILED,
            "error": str(exc),
            "last_failure_category": last_failure_category,
        }
    )


def _log_device_connectivity_failure(
    *,
    task: BatchTaskSpec,
    attempt: int,
    controller_task_id: str,
    trace_id: str,
    failure_category: FailureCategory,
    error: str,
) -> None:
    if failure_category not in {FailureCategory.DEVICE_COMMAND_TIMEOUT, FailureCategory.DEVICE_TRANSPORT}:
        return
    logger.warning(
        "controller task failed with device connectivity failure",
        extra={
            "task_id": task.task_id,
            "attempt": attempt,
            "controller_task_id": controller_task_id,
            "trace_id": trace_id,
            "failure_category": failure_category.value,
            "error": error,
        },
    )
