"""Prepare one single-use sandbox reservation for a batch task."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.batch.metrics import BatchMetrics
from northcua.batch.preparation.assignment import PreparedSandboxAssignment
from northcua.batch.preparation.contracts import (
    PreparedSandboxEventMessage,
    PreparedSandboxFailureKind,
    PreparedSandboxPolicy,
    PreparedSandboxRecord,
)
from northcua.batch.preparation.events import PreparedSandboxEventEmitter
from northcua.batch.preparation.planner import PreparedSandboxCandidate
from northcua.batch.preparation.release import PreparedSandboxReleaseHelper
from northcua.batch.preparation.sqlite_store import SQLitePreparedSandboxStore
from northcua.batch.preparation.startup import PreparedSandboxStartupRunner
from northcua.batch.runner.events import BatchEventWriter
from northcua.batch.runner.leases import BatchTaskLeaseContext, BatchTaskLeaseCoordinator
from northcua.batch.state.async_repository import prepared_repository_call as repository_call
from northcua.sandboxes.contracts import SandboxLifecycleEvent, SandboxLifecycleMessage, SandboxProviderSpec
from northcua.sandboxes.guest_capability_evidence import guest_capability_evidence_payloads
from northcua.sandboxes.lifecycle.errors import SandboxAcquireCleanupError
from northcua.telemetry.contracts import CasePhase, CaseStatus, TelemetryEntityKind

logger = logging.getLogger(__name__)


_CANCEL_REASON_TO_FAILURE_KIND = {
    PreparedSandboxFailureKind.CLAIM_WAIT_TIMEOUT_CANCELLED_FUTURE.value: (
        PreparedSandboxFailureKind.CLAIM_WAIT_TIMEOUT_CANCELLED_FUTURE
    ),
    PreparedSandboxFailureKind.MANAGER_CLOSED_BEFORE_ASSIGNMENT.value: (
        PreparedSandboxFailureKind.MANAGER_CLOSED_BEFORE_ASSIGNMENT
    ),
    PreparedSandboxFailureKind.PREPARATION_STOPPED_BY_CONTROL.value: (
        PreparedSandboxFailureKind.PREPARATION_STOPPED_BY_CONTROL
    ),
    PreparedSandboxFailureKind.EXPIRED_BEFORE_ASSIGNMENT.value: PreparedSandboxFailureKind.EXPIRED_BEFORE_ASSIGNMENT,
}
_SANDBOX_COMMAND_FAILURE_KIND_RE = re.compile(r"\bfailure_kind=([a-z0-9_]+)")
_SANDBOX_COMMAND_LABEL_RE = re.compile(r"\blabel='([^']*)'")


def _elapsed_seconds(started_at: float | None, finished_at: float | None) -> float | None:
    if started_at is None or finished_at is None:
        return None
    return round(max(finished_at - started_at, 0.0), 3)


def _compact_dict(values: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


@dataclass
class _PreparedTiming:
    scheduled_at: float
    semaphore_acquired_at: float | None = None
    backpressure_gate_entered_at: float | None = None
    backpressure_cleared_at: float | None = None
    reserve_started_at: float | None = None
    reserve_finished_at: float | None = None
    acquire_started_at: float | None = None
    acquire_finished_at: float | None = None
    sandbox_acquire_started_at: float | None = None
    sandbox_acquired_at: float | None = None
    device_worker_started_at: float | None = None
    device_readiness_wait_started_at: float | None = None
    device_readiness_wait_finished_at: float | None = None
    device_ready_at: float | None = None
    persist_started_at: float | None = None
    persist_finished_at: float | None = None
    ready_at: float | None = None

    def observe_lifecycle_event(self, event: SandboxLifecycleEvent) -> None:
        """Capture monotonic timestamps from sandbox lifecycle events.

        Args:
            event: Lifecycle event emitted by the sandbox lease manager while
                creating the prepared sandbox or waiting for device readiness.
        """

        now = time.monotonic()
        if event.message == SandboxLifecycleMessage.SANDBOX_ACQUIRE_STARTED.value:
            self.sandbox_acquire_started_at = now
        elif event.message == SandboxLifecycleMessage.SANDBOX_ACQUIRED.value:
            self.sandbox_acquired_at = now
        elif event.message == SandboxLifecycleMessage.DEVICE_WORKER_STARTED.value:
            self.device_worker_started_at = now
        elif event.message == SandboxLifecycleMessage.DEVICE_READINESS_WAIT_STARTED.value:
            self.device_readiness_wait_started_at = now
        elif event.message == SandboxLifecycleMessage.DEVICE_READINESS_WAIT_FINISHED.value:
            self.device_readiness_wait_finished_at = now
        elif event.message == SandboxLifecycleMessage.DEVICE_READY.value:
            self.device_ready_at = now

    def details(self) -> dict[str, object]:
        """Return compact timing fields for telemetry and audit events.

        Returns:
            Mapping of available phase durations. Missing phases are omitted so
            callers can merge the result into JSON telemetry without null churn.
        """

        return _compact_dict(
            {
                "candidate_scheduled_to_semaphore_acquired_seconds": _elapsed_seconds(
                    self.scheduled_at,
                    self.semaphore_acquired_at,
                ),
                "semaphore_to_backpressure_gate_seconds": _elapsed_seconds(
                    self.semaphore_acquired_at,
                    self.backpressure_gate_entered_at,
                ),
                "backpressure_wait_seconds": _elapsed_seconds(
                    self.backpressure_gate_entered_at,
                    self.backpressure_cleared_at,
                ),
                "reserve_seconds": _elapsed_seconds(self.reserve_started_at, self.reserve_finished_at),
                "coordinator_acquire_seconds": _elapsed_seconds(self.acquire_started_at, self.acquire_finished_at),
                "e2b_create_seconds": _elapsed_seconds(
                    self.sandbox_acquire_started_at,
                    self.sandbox_acquired_at,
                ),
                "worker_start_to_device_ready_seconds": _elapsed_seconds(
                    self.device_worker_started_at,
                    self.device_ready_at,
                ),
                "readiness_wait_seconds": _elapsed_seconds(
                    self.device_readiness_wait_started_at,
                    self.device_readiness_wait_finished_at,
                ),
                "prepared_ready_persist_seconds": _elapsed_seconds(self.persist_started_at, self.persist_finished_at),
                "candidate_scheduled_to_ready_seconds": _elapsed_seconds(self.scheduled_at, self.ready_at),
            }
        )


def _failure_kind_for_cancel(exc: asyncio.CancelledError) -> PreparedSandboxFailureKind:
    if not exc.args:
        return PreparedSandboxFailureKind.CREATION_CANCELED
    reason = str(exc.args[0])
    return _CANCEL_REASON_TO_FAILURE_KIND.get(reason, PreparedSandboxFailureKind.CREATION_CANCELED)


def _recipe_failure_details(error: str) -> dict[str, object]:
    details: dict[str, object] = {}
    kind_match = _SANDBOX_COMMAND_FAILURE_KIND_RE.search(error)
    if kind_match is not None:
        details["recipe_failure_kind"] = kind_match.group(1)
    label_match = _SANDBOX_COMMAND_LABEL_RE.search(error)
    if label_match is not None:
        details["recipe_label"] = label_match.group(1)
    capability_evidence = guest_capability_evidence_payloads(error)
    if capability_evidence:
        details["guest_capability_evidence"] = capability_evidence
    return details


def _prepared_device_id(
    *,
    coordinator: BatchTaskLeaseCoordinator,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    candidate: PreparedSandboxCandidate,
    reservation_token: str,
) -> str:
    base = coordinator.binding_factory.device_id_for_task(
        config.manifest.batch_id,
        task,
        candidate.sandbox_spec,
        attempt=candidate.attempt,
    )
    return f"{base}-prep-{reservation_token[:8]}"


def _prepared_sandbox_spec(
    *,
    config: BatchRunConfig,
    task: BatchTaskSpec,
    candidate: PreparedSandboxCandidate,
    policy: PreparedSandboxPolicy,
    runner_id: str,
) -> SandboxProviderSpec:
    task_timeout_seconds = float(task.timeout_seconds or config.manifest.defaults.timeout_seconds or 0.0)
    original_provider_timeout_seconds = max(float(candidate.sandbox_spec.resources.timeout_seconds or 0.0), 0.0)
    required_provider_timeout_seconds = max(
        policy.ttl_seconds + max(task_timeout_seconds, 0.0) + policy.assignment_ttl_margin_seconds,
        0.0,
    )
    provider_timeout_seconds = max(
        int(math.ceil(original_provider_timeout_seconds)),
        int(math.ceil(required_provider_timeout_seconds)),
    )
    metadata = {
        **candidate.sandbox_spec.metadata,
        "prepared_sandbox": "true",
        "prepared_depth": policy.depth.value,
        "prepared_runner_id": runner_id,
        "prepared_ttl_seconds": policy.ttl_seconds,
        "prepared_case_timeout_seconds": max(task_timeout_seconds, 0.0),
        "prepared_assignment_ttl_margin_seconds": policy.assignment_ttl_margin_seconds,
        "prepared_original_provider_timeout_seconds": original_provider_timeout_seconds,
        "prepared_required_provider_timeout_seconds": required_provider_timeout_seconds,
        "prepared_provider_timeout_seconds": provider_timeout_seconds,
    }
    resources = candidate.sandbox_spec.resources.model_copy(update={"timeout_seconds": provider_timeout_seconds})
    return candidate.sandbox_spec.model_copy(update={"metadata": metadata, "resources": resources})


def _prepared_log_dir(output_dir: Path, *, task: BatchTaskSpec, attempt: int) -> Path:
    return output_dir / "sandbox-logs" / task.task_id / f"attempt-{attempt}-prepared"


def _device_controller_url(config: BatchRunConfig) -> str:
    defaults = config.manifest.defaults
    submitter_controller_url = config.controller_url or defaults.controller_url
    return config.device_controller_url or defaults.device_controller_url or submitter_controller_url


def _token(config: BatchRunConfig) -> str:
    return config.token or config.manifest.defaults.token


class PreparedSandboxPreparer:
    """Create, initialize, and persist one prepared sandbox assignment."""

    def __init__(
        self,
        *,
        config: BatchRunConfig,
        store: SQLitePreparedSandboxStore,
        coordinator: BatchTaskLeaseCoordinator,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
        policy: PreparedSandboxPolicy,
        run_concurrency: int,
        prepare_semaphore: asyncio.Semaphore,
        startup_runner: PreparedSandboxStartupRunner,
        release_helper: PreparedSandboxReleaseHelper,
        event_emitter: PreparedSandboxEventEmitter,
        assignments: dict[tuple[str, int], PreparedSandboxAssignment],
        ready_changed: asyncio.Event,
        claim_visible: Callable[[tuple[str, int]], None],
        backpressure_gate: ControllerBackpressureGate | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Create a preparer bound to the manager's shared state."""

        self._config = config
        self._store = store
        self._coordinator = coordinator
        self._events = events
        self._metrics = metrics
        self._output_dir = output_dir
        self._policy = policy
        self._run_concurrency = run_concurrency
        self._prepare_semaphore = prepare_semaphore
        self._startup_runner = startup_runner
        self._release_helper = release_helper
        self._event_emitter = event_emitter
        self._assignments = assignments
        self._ready_changed = ready_changed
        self._claim_visible = claim_visible
        self._backpressure_gate = backpressure_gate
        self._stop_requested = stop_requested

    async def prepare_with_semaphore(
        self,
        candidate: PreparedSandboxCandidate,
        *,
        scheduled_at: float | None = None,
    ) -> PreparedSandboxAssignment | None:
        """Prepare a candidate while respecting the configured concurrency cap.

        Args:
            candidate: Task attempt selected by the preparation planner.
            scheduled_at: Monotonic time when the manager enqueued the future.

        Returns:
            Prepared sandbox assignment when preparation succeeds; otherwise
            ``None`` so the normal claim-time path can run.
        """

        timing = _PreparedTiming(scheduled_at=scheduled_at or time.monotonic())
        async with self._prepare_semaphore:
            timing.semaphore_acquired_at = time.monotonic()
            if self._backpressure_gate is not None:
                timing.backpressure_gate_entered_at = time.monotonic()
                if not await self._backpressure_gate.wait_if_needed(
                    scope="prepare",
                    stop_requested=self._stop_requested,
                ):
                    timing.backpressure_cleared_at = time.monotonic()
                    return None
                timing.backpressure_cleared_at = time.monotonic()
            try:
                return await self.prepare(candidate, timing=timing)
            finally:
                self._ready_changed.set()

    async def prepare(
        self,
        candidate: PreparedSandboxCandidate,
        *,
        timing: _PreparedTiming | None = None,
    ) -> PreparedSandboxAssignment | None:
        """Reserve and initialize a sandbox for a future task claim.

        Args:
            candidate: Task attempt and sandbox spec selected for preparation.

        Returns:
            Prepared assignment if the sandbox is ready and persisted.

        Raises:
            asyncio.CancelledError: Propagated when the manager shuts down the
                preparation task before acquisition completes.
        """

        timing = timing or _PreparedTiming(scheduled_at=time.monotonic())
        task = candidate.task
        timing.reserve_started_at = time.monotonic()
        preparing = await self._reserve_candidate(candidate)
        timing.reserve_finished_at = time.monotonic()
        if preparing is None:
            return None
        self._claim_visible((task.task_id, candidate.attempt))
        started = time.monotonic()
        try:
            timing.acquire_started_at = time.monotonic()
            context = await self._acquire_context(
                task=task,
                candidate=candidate,
                reservation=preparing,
                timing=timing,
            )
            timing.acquire_finished_at = time.monotonic()
        except asyncio.CancelledError as exc:
            timing.acquire_finished_at = time.monotonic()
            failure_kind = _failure_kind_for_cancel(exc)
            await repository_call(lambda: self._store.mark_failed(preparing, error=failure_kind.value))
            self._metrics.observe_prepared_failure()
            self._events.prepared_event(
                case_id=task.task_id,
                message=PreparedSandboxEventMessage.FAILED,
                status=CaseStatus.FAILED,
                attempt=candidate.attempt,
                details={
                    "attempt": candidate.attempt,
                    "error": failure_kind.value,
                    "failure_kind": failure_kind.value,
                    "cancelled": True,
                    "timing": timing.details(),
                },
            )
            raise
        except SandboxAcquireCleanupError as exc:
            timing.acquire_finished_at = time.monotonic()
            await self._handle_acquire_cleanup_error(
                task=task,
                candidate=candidate,
                reservation=preparing,
                exc=exc,
                timing=timing,
            )
            return None
        except Exception as exc:
            timing.acquire_finished_at = time.monotonic()
            logger.warning(
                "prepared sandbox failed",
                extra={
                    "batch_id": self._config.manifest.batch_id,
                    "task_id": task.task_id,
                    "attempt": candidate.attempt,
                },
                exc_info=True,
            )
            await self._handle_acquire_error(
                task=task,
                candidate=candidate,
                reservation=preparing,
                exc=exc,
                timing=timing,
            )
            return None
        prepared = await self._persist_prepared_context(
            task=task,
            candidate=candidate,
            reservation=preparing,
            context=context,
            started=started,
            timing=timing,
        )
        if prepared is None:
            return None
        return await self._emit_ready_assignment(
            task=task,
            candidate=candidate,
            prepared=prepared,
            context=context,
            timing=timing,
        )

    async def _reserve_candidate(self, candidate: PreparedSandboxCandidate) -> PreparedSandboxRecord | None:
        task = candidate.task
        prepared_spec = _prepared_sandbox_spec(
            config=self._config,
            task=task,
            candidate=candidate,
            policy=self._policy,
            runner_id=self._config.runner_id,
        )
        record = await repository_call(
            lambda: self._store.reserve(
                task_id=task.task_id,
                attempt=candidate.attempt,
                runner_id=self._config.runner_id,
                depth=self._policy.depth,
                provider=candidate.sandbox_spec.provider,
                template=candidate.sandbox_spec.template,
                ttl_seconds=self._policy.ttl_seconds,
                buffer_limit=self._policy.effective_buffer(run_concurrency=self._run_concurrency),
                live_limit=self._run_concurrency + self._policy.effective_buffer(run_concurrency=self._run_concurrency),
                metadata={**prepared_spec.metadata, "batch_id": self._config.manifest.batch_id},
            )
        )
        if record is None:
            return None
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.RESERVED,
            attempt=candidate.attempt,
            details={
                "attempt": candidate.attempt,
                "depth": self._policy.depth.value,
                "ttl_seconds": self._policy.ttl_seconds,
                "prepared_provider_timeout_seconds": prepared_spec.metadata["prepared_provider_timeout_seconds"],
                "prepared_case_timeout_seconds": prepared_spec.metadata["prepared_case_timeout_seconds"],
                "prepared_assignment_ttl_margin_seconds": prepared_spec.metadata[
                    "prepared_assignment_ttl_margin_seconds"
                ],
                "prepare_buffer": self._policy.effective_buffer(run_concurrency=self._run_concurrency),
                "configured_prepare_concurrency": self._policy.prepare_concurrency,
            },
        )
        return await repository_call(lambda: self._store.mark_preparing(record))

    async def _acquire_context(
        self,
        *,
        task: BatchTaskSpec,
        candidate: PreparedSandboxCandidate,
        reservation: PreparedSandboxRecord,
        timing: _PreparedTiming,
    ) -> BatchTaskLeaseContext:
        with self._events.phase_timer(
            case_id=task.task_id,
            phase=CasePhase.PREPARED_SANDBOX,
            status=CaseStatus.PROVISIONING,
            attempt=candidate.attempt,
            entity_kind=TelemetryEntityKind.SANDBOX,
        ):
            return await self._coordinator.acquire(
                config=self._config,
                task=task,
                sandbox_spec=_prepared_sandbox_spec(
                    config=self._config,
                    task=task,
                    candidate=candidate,
                    policy=self._policy,
                    runner_id=self._config.runner_id,
                ),
                controller_url=_device_controller_url(self._config),
                token=_token(self._config),
                events=self._events,
                output_dir=self._output_dir,
                attempt=candidate.attempt,
                create_semaphore=None,
                startup_commands_override=self._startup_runner.startup_commands_for_depth(task),
                sandbox_phase_override=CasePhase.PREPARED_SANDBOX,
                bootstrap_log_dir_override=_prepared_log_dir(
                    self._output_dir,
                    task=task,
                    attempt=candidate.attempt,
                ),
                device_id_override=_prepared_device_id(
                    coordinator=self._coordinator,
                    config=self._config,
                    task=task,
                    candidate=candidate,
                    reservation_token=reservation.reservation_token,
                ),
                lifecycle_event_observer=timing.observe_lifecycle_event,
            )

    async def _handle_acquire_cleanup_error(
        self,
        *,
        task: BatchTaskSpec,
        candidate: PreparedSandboxCandidate,
        reservation: PreparedSandboxRecord,
        exc: SandboxAcquireCleanupError,
        timing: _PreparedTiming,
    ) -> None:
        logger.warning(
            "prepared sandbox cleanup failed during acquire",
            extra={
                "batch_id": self._config.manifest.batch_id,
                "task_id": task.task_id,
                "attempt": candidate.attempt,
                "sandbox_id": exc.sandbox_id,
            },
            exc_info=True,
        )
        terminate_error = str(exc.cleanup_error)
        await repository_call(
            lambda: self._store.mark_cleanup_failed(
                reservation,
                error=f"acquire_failed_after_capacity; terminate_failed: {terminate_error}",
                sandbox_id=exc.sandbox_id,
            )
        )
        self._metrics.observe_prepared_failure()
        self._metrics.observe_prepared_cleanup_failure()
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.CLEANUP_FAILED,
            status=CaseStatus.FAILED,
            attempt=candidate.attempt,
            details={
                "attempt": candidate.attempt,
                "cleanup_result": "terminate_failed",
                "close_reason": "acquire_failed_after_capacity",
                "error": str(exc)[:500],
                "prepare_failed": True,
                "terminate_error": terminate_error[:500],
                "timing": timing.details(),
            },
            sandbox_id=exc.sandbox_id,
        )

    async def _handle_acquire_error(
        self,
        *,
        task: BatchTaskSpec,
        candidate: PreparedSandboxCandidate,
        reservation: PreparedSandboxRecord,
        exc: Exception,
        timing: _PreparedTiming,
    ) -> None:
        await repository_call(lambda: self._store.mark_failed(reservation, error=str(exc)))
        self._metrics.observe_prepared_failure()
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.FAILED,
            status=CaseStatus.FAILED,
            attempt=candidate.attempt,
            details={
                "attempt": candidate.attempt,
                "error": str(exc)[:500],
                "failure_kind": PreparedSandboxFailureKind.CREATION_FAILED.value,
                "timing": timing.details(),
                **_recipe_failure_details(str(exc)),
            },
        )

    async def _persist_prepared_context(
        self,
        *,
        task: BatchTaskSpec,
        candidate: PreparedSandboxCandidate,
        reservation: PreparedSandboxRecord,
        context: BatchTaskLeaseContext,
        started: float,
        timing: _PreparedTiming,
    ) -> PreparedSandboxRecord | None:
        commit_started = time.monotonic()
        timing.persist_started_at = commit_started
        try:
            prepared = await repository_call(
                lambda: self._store.mark_prepared(
                    reservation,
                    sandbox_id=context.lease.handle.sandbox_id,
                    device_id=context.lease.device_id,
                    lease_id=context.lease.lease_id,
                    prepare_seconds=time.monotonic() - started,
                )
            )
            commit_ms = max(0, int((time.monotonic() - commit_started) * 1000))
            timing.persist_finished_at = time.monotonic()
        except Exception as exc:
            error_text = str(exc)
            commit_ms = max(0, int((time.monotonic() - commit_started) * 1000))
            timing.persist_finished_at = time.monotonic()
            self._emit_prepared_stage_event(
                case_id=task.task_id,
                status=CaseStatus.FAILED,
                attempt=candidate.attempt,
                details={
                    "attempt": candidate.attempt,
                    "stage": "prepared_db_commit",
                    "status": "failed",
                    "duration_ms": commit_ms,
                    "error": error_text[:500],
                    "timing": timing.details(),
                },
                sandbox_id=context.lease.handle.sandbox_id,
                device_id=context.lease.device_id,
            )
            logger.warning(
                "prepared sandbox persistence failed after acquire",
                extra={
                    "batch_id": self._config.manifest.batch_id,
                    "task_id": task.task_id,
                    "attempt": candidate.attempt,
                    "sandbox_id": context.lease.handle.sandbox_id,
                },
                exc_info=True,
            )
            release_result = await self._release_helper.try_release_prepared_context(
                task=task,
                attempt=candidate.attempt,
                context=context,
                close_reason="persist failed after acquire",
            )
            if release_result:
                await repository_call(lambda: self._store.mark_failed(reservation, error=error_text))
                self._event_emitter.prepare_failed(task=task, attempt=candidate.attempt, error=error_text)
            else:
                await repository_call(
                    lambda: self._store.mark_cleanup_failed(
                        reservation,
                        error=f"persist failed after acquire: {error_text}; terminate_failed: {release_result.error}",
                        sandbox_id=context.lease.handle.sandbox_id,
                        device_id=context.lease.device_id,
                        lease_id=context.lease.lease_id,
                    )
                )
            self._metrics.observe_prepared_failure()
            return None
        self._emit_prepared_stage_event(
            case_id=task.task_id,
            status=CaseStatus.PROVISIONING,
            attempt=candidate.attempt,
            details={
                "attempt": candidate.attempt,
                "stage": "prepared_db_commit",
                "status": "succeeded" if prepared is not None else "rejected",
                "duration_ms": commit_ms,
                "prepare_seconds": max(time.monotonic() - started, 0.0),
                "timing": timing.details(),
            },
            sandbox_id=context.lease.handle.sandbox_id,
            device_id=context.lease.device_id,
        )
        if prepared is None:
            release_result = await self._release_helper.try_release_prepared_context(
                task=task,
                attempt=candidate.attempt,
                context=context,
                close_reason="prepared row changed before persist",
            )
            if not release_result:
                await repository_call(
                    lambda: self._store.mark_cleanup_failed(
                        reservation,
                        error=f"prepared row changed before persist; terminate_failed: {release_result.error}",
                        sandbox_id=context.lease.handle.sandbox_id,
                        device_id=context.lease.device_id,
                        lease_id=context.lease.lease_id,
                    )
                )
            else:
                await repository_call(
                    lambda: self._store.mark_failed(reservation, error="prepared row changed before persist")
                )
                self._event_emitter.prepare_failed(
                    task=task,
                    attempt=candidate.attempt,
                    error="prepared row changed before persist",
                )
            return None
        return prepared

    def _emit_prepared_stage_event(
        self,
        *,
        case_id: str,
        status: CaseStatus,
        attempt: int,
        details: dict[str, object],
        sandbox_id: str,
        device_id: str,
    ) -> None:
        try:
            self._events.prepared_event(
                case_id=case_id,
                message="prepared_sandbox_stage_timing",
                status=status,
                attempt=attempt,
                details=details,
                sandbox_id=sandbox_id,
                device_id=device_id,
            )
        except Exception:
            logger.warning(
                "failed to write prepared sandbox stage telemetry",
                extra={
                    "batch_id": self._config.manifest.batch_id,
                    "task_id": case_id,
                    "attempt": attempt,
                    "stage": details.get("stage", ""),
                },
                exc_info=True,
            )

    async def _emit_ready_assignment(
        self,
        *,
        task: BatchTaskSpec,
        candidate: PreparedSandboxCandidate,
        prepared: PreparedSandboxRecord,
        context: BatchTaskLeaseContext,
        timing: _PreparedTiming,
    ) -> PreparedSandboxAssignment:
        assignment = PreparedSandboxAssignment(record=prepared, lease=context.lease, lease_context=context)
        self._assignments[prepared.key] = assignment
        self._metrics.observe_prepared_sandbox(prepared.prepare_seconds)
        self._ready_changed.set()
        timing.ready_at = time.monotonic()
        active_unassigned, status_counts = await repository_call(
            lambda: (
                self._store.active_count(runner_id=self._config.runner_id),
                self._store.status_counts(runner_id=self._config.runner_id),
            )
        )
        self._events.prepared_event(
            case_id=task.task_id,
            message=PreparedSandboxEventMessage.READY,
            status=CaseStatus.PROVISIONING,
            attempt=candidate.attempt,
            details={
                "attempt": candidate.attempt,
                "prepare_seconds": prepared.prepare_seconds,
                "run_concurrency": self._run_concurrency,
                "prepare_buffer": self._policy.effective_buffer(run_concurrency=self._run_concurrency),
                "active_unassigned": active_unassigned,
                "prepared_status_counts": status_counts,
                "timing": timing.details(),
            },
            sandbox_id=prepared.sandbox_id,
            device_id=prepared.device_id,
        )
        return assignment
