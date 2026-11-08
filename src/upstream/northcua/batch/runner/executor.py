"""Single-task batch attempt execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from northcua.batch.backpressure import ControllerBackpressureGate
from northcua.batch.contracts import (
    BatchRunConfig,
    BatchTaskSpec,
    BatchTaskState,
    BatchTaskStatus,
)
from northcua.batch.metrics import BatchMetrics
from northcua.batch.planner import BatchTaskPlanner
from northcua.batch.preparation.manager import PreparedSandboxManager
from northcua.batch.retry_policy import task_state_blocks_retry
from northcua.batch.runner.attempt_helpers import _execute_attempt
from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
from northcua.batch.runner.controller import ControllerTaskWatcher
from northcua.batch.runner.events import BatchAttemptEventMessage, BatchEventWriter
from northcua.batch.runner.lease_heartbeat import BatchLeaseHeartbeat, BatchLeaseLostError, start_batch_lease_heartbeat
from northcua.batch.runner.leases import BatchTaskLeaseCoordinator
from northcua.batch.runner.retry_backoff import phase_for_backoff, with_infra_retry_backoff
from northcua.batch.state import BatchTaskLease, LeasePolicy
from northcua.sandboxes.device.readiness import DeviceReadinessProbe
from northcua.sandboxes.provider.registry import SandboxProviderRegistry
from northcua.telemetry.contracts import CasePhase, CaseStatus, FailureCategory

_LOCAL_RECOVERY_STATUSES = {
    BatchTaskStatus.STARTING_SANDBOX,
    BatchTaskStatus.BOOTSTRAPPING_DEVICE,
    BatchTaskStatus.WAITING_DEVICE,
    BatchTaskStatus.SUBMITTED,
    BatchTaskStatus.RUNNING,
}
_StopRequested = Callable[[], bool | Awaitable[bool]]


class BatchAttemptExecutor:
    """Execute task attempts through sandbox leases and controller tasks."""

    def __init__(
        self,
        *,
        provider_registry: SandboxProviderRegistry,
        planner: BatchTaskPlanner,
        watcher: ControllerTaskWatcher,
        binding_factory: SandboxDeviceBindingFactory | None = None,
        readiness_probe: DeviceReadinessProbe | None = None,
    ) -> None:
        self._providers = provider_registry
        self._planner = planner
        self._watcher = watcher
        self._binding_factory = binding_factory or SandboxDeviceBindingFactory()
        self._leases = BatchTaskLeaseCoordinator(
            provider_registry=provider_registry,
            binding_factory=self._binding_factory,
            readiness_probe=readiness_probe,
        )

    @property
    def lease_coordinator(self) -> BatchTaskLeaseCoordinator:
        """Return the shared lease coordinator used by prepared queues.

        Returns:
            Coordinator responsible for sandbox create/upload/bootstrap/release.
        """

        return self._leases

    async def run_task(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
        claim: BatchTaskLease | None = None,
        policy: LeasePolicy | None = None,
        create_semaphore: asyncio.Semaphore | None = None,
        submit_semaphore: asyncio.Semaphore | None = None,
        prepared_manager: PreparedSandboxManager | None = None,
        backpressure_gate: ControllerBackpressureGate | None = None,
        backpressure_stop_requested: _StopRequested | None = None,
    ) -> BatchTaskState:
        """Run one batch task with configured retry policy.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task to execute.
            events: Writer for local task and sandbox lifecycle snapshots.
            metrics: Metrics collector for sandbox and task timings.
            output_dir: Concrete batch output directory.
            claim: Optional local task lease used for heartbeat ownership.
            policy: Optional heartbeat timing policy.
            create_semaphore: Optional semaphore guarding sandbox creation.
            submit_semaphore: Optional semaphore guarding controller submit
                websocket opening through TASK_ACCEPTED.
            prepared_manager: Optional background single-use prepared sandbox
                queue used before falling back to normal acquisition.

        Returns:
            Latest state after success, retry exhaustion, cancellation, or error.
        """

        defaults = config.manifest.defaults
        max_retries = defaults.max_retries if task.max_retries < 0 else task.max_retries
        initial_attempts = claim.state.attempts if claim is not None else 0
        last_state = claim.state if claim is not None else BatchTaskState(task_id=task.task_id)
        heartbeat = (
            start_batch_lease_heartbeat(events=events, claim=claim, policy=policy)
            if claim is not None and policy is not None
            else None
        )
        try:
            total_attempt_budget = max_retries + 1
            if claim is not None and initial_attempts >= total_attempt_budget:
                recovered = self._exhausted_local_recovery_state(
                    last_state,
                    total_attempt_budget=total_attempt_budget,
                )
                if recovered is not None:
                    metrics.observe_attempt_failure(recovered.last_failure_category or FailureCategory.UNKNOWN)
                    await events.upsert_task_async(recovered)
                    return recovered
            for attempt in range(initial_attempts + 1, max_retries + 2):
                last_state = await self._run_attempt_with_lease_guard(
                    config=config,
                    task=task,
                    events=events,
                    metrics=metrics,
                    output_dir=output_dir,
                    attempt=attempt,
                    heartbeat=heartbeat,
                    create_semaphore=create_semaphore,
                    submit_semaphore=submit_semaphore,
                    prepared_manager=prepared_manager,
                    backpressure_gate=backpressure_gate,
                    backpressure_stop_requested=backpressure_stop_requested,
                )
                if last_state.status == BatchTaskStatus.SUCCEEDED:
                    return self._with_retry_budget(last_state, max_retries=max_retries)
                if last_state.status == BatchTaskStatus.CANCELED:
                    return self._with_retry_budget(last_state, max_retries=max_retries)
                if last_state.status == BatchTaskStatus.EVALUATION_PENDING:
                    return self._with_retry_budget(last_state, max_retries=max_retries)
                if last_state.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
                    return self._with_retry_budget(last_state, max_retries=max_retries)
                if _has_non_retryable_terminal_decision(last_state):
                    return self._with_retry_budget(last_state, max_retries=max_retries)
                delayed = with_infra_retry_backoff(
                    config=config,
                    task=task,
                    state=self._with_retry_budget(last_state, max_retries=max_retries),
                    max_retries=max_retries,
                )
                if delayed is not last_state and delayed.next_attempt_after > 0:
                    await events.upsert_task_async(delayed)
                    events.attempt_event(
                        case_id=task.task_id,
                        message=BatchAttemptEventMessage.INFRA_RETRY_BACKOFF_SCHEDULED,
                        phase=phase_for_backoff(delayed.last_failure_category),
                        status=CaseStatus.FAILED,
                        attempt=delayed.attempts,
                        details={
                            "failure_category": (
                                delayed.last_failure_category.value if delayed.last_failure_category is not None else ""
                            ),
                            "retry_budget_remaining": delayed.retry_budget_remaining,
                            "next_attempt_after": delayed.next_attempt_after,
                            "delay_seconds": round(max(0.0, delayed.next_attempt_after - time.time()), 3),
                        },
                        controller_task_id=delayed.controller_task_id,
                        trace_id=delayed.trace_id,
                        sandbox_id=delayed.sandbox_id,
                        device_id=delayed.device_id,
                    )
                    return delayed
        finally:
            if heartbeat is not None:
                await heartbeat.close()
        return self._with_retry_budget(last_state, max_retries=max_retries)

    @staticmethod
    def _exhausted_local_recovery_state(
        state: BatchTaskState,
        *,
        total_attempt_budget: int,
    ) -> BatchTaskState | None:
        """Terminalize exhausted in-flight rows that have no attempt left."""

        if state.status not in _LOCAL_RECOVERY_STATUSES:
            return None
        if state.controller_task_id:
            return None
        category = _local_recovery_failure_category(state.status)
        return state.model_copy(
            update={
                "status": BatchTaskStatus.FAILED,
                "error": (
                    f"local setup attempt budget exhausted while recovering {state.status.value} "
                    f"after {total_attempt_budget} attempt(s)"
                ),
                "last_failure_category": category,
                "retry_budget_remaining": 0,
            }
        )

    @staticmethod
    def _with_retry_budget(state: BatchTaskState, *, max_retries: int) -> BatchTaskState:
        """Attach remaining whole-case retry budget to a task state."""

        return state.model_copy(update={"retry_budget_remaining": max(max_retries + 1 - state.attempts, 0)})

    async def run_attempt(
        self,
        *,
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
        """Run one concrete task attempt.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task to execute.
            events: Writer for local task and sandbox lifecycle snapshots.
            metrics: Metrics collector for sandbox and task timings.
            output_dir: Concrete batch output directory.
            attempt: One-based attempt number.
            create_semaphore: Optional semaphore guarding sandbox creation.
            submit_semaphore: Optional semaphore guarding controller submit.
            prepared_manager: Optional prepared sandbox queue.

        Returns:
            Latest state for this concrete attempt.
        """

        return await _execute_attempt(
            coordinator=self._leases,
            binding_factory=self._binding_factory,
            planner=self._planner,
            watcher=self._watcher,
            config=config,
            task=task,
            events=events,
            metrics=metrics,
            output_dir=output_dir,
            attempt=attempt,
            create_semaphore=create_semaphore,
            submit_semaphore=submit_semaphore,
            prepared_manager=prepared_manager,
            backpressure_gate=backpressure_gate,
            backpressure_stop_requested=backpressure_stop_requested,
        )

    async def _run_attempt_with_lease_guard(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        events: BatchEventWriter,
        metrics: BatchMetrics,
        output_dir: Path,
        attempt: int,
        heartbeat: BatchLeaseHeartbeat | None,
        create_semaphore: asyncio.Semaphore | None,
        submit_semaphore: asyncio.Semaphore | None,
        prepared_manager: PreparedSandboxManager | None,
        backpressure_gate: ControllerBackpressureGate | None,
        backpressure_stop_requested: _StopRequested | None,
    ) -> BatchTaskState:
        """Run one attempt and cancel it if local task ownership is lost.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task to execute.
            events: Writer for local task and sandbox lifecycle snapshots.
            metrics: Metrics collector for sandbox and task timings.
            output_dir: Concrete batch output directory.
            attempt: One-based attempt number.
            heartbeat: Optional heartbeat handle for a claimed task.
            create_semaphore: Optional semaphore guarding sandbox creation.
            submit_semaphore: Optional semaphore guarding controller submit.

        Returns:
            Latest task state when the attempt completed normally.

        Raises:
            BatchLeaseLostError: If another runner has reclaimed this task.
        """

        if heartbeat is None:
            return await self.run_attempt(
                config=config,
                task=task,
                events=events,
                metrics=metrics,
                output_dir=output_dir,
                attempt=attempt,
                create_semaphore=create_semaphore,
                submit_semaphore=submit_semaphore,
                prepared_manager=prepared_manager,
                backpressure_gate=backpressure_gate,
                backpressure_stop_requested=backpressure_stop_requested,
            )

        attempt_task = asyncio.create_task(
            self.run_attempt(
                config=config,
                task=task,
                events=events,
                metrics=metrics,
                output_dir=output_dir,
                attempt=attempt,
                create_semaphore=create_semaphore,
                submit_semaphore=submit_semaphore,
                prepared_manager=prepared_manager,
                backpressure_gate=backpressure_gate,
                backpressure_stop_requested=backpressure_stop_requested,
            ),
            name=f"northcua-batch-attempt:{task.task_id}:{attempt}",
        )
        lease_lost_task = asyncio.create_task(
            heartbeat.lost.wait(),
            name=f"northcua-batch-lease-lost:{task.task_id}",
        )
        done, pending = await asyncio.wait(
            {attempt_task, lease_lost_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        try:
            if lease_lost_task in done and heartbeat.lost.is_set():
                attempt_task.cancel()
                await asyncio.gather(attempt_task, return_exceptions=True)
                raise BatchLeaseLostError(f"batch task lease lost for {task.task_id}")
            lease_lost_task.cancel()
            await asyncio.gather(lease_lost_task, return_exceptions=True)
            return await attempt_task
        finally:
            for task_to_cancel in pending:
                task_to_cancel.cancel()
            for task_to_cancel in pending:
                try:
                    await task_to_cancel
                except asyncio.CancelledError:
                    pass


def _has_non_retryable_terminal_decision(state: BatchTaskState) -> bool:
    """Return whether an agent terminal decision forbids whole-case retry.

    Args:
        state: Latest task state returned by the controller attempt.

    Returns:
        ``True`` when the task failed with a structured decision whose
        ``retryable`` flag is explicitly false. Timeout rows are excluded
        because timeout retry policy belongs to the batch attempt budget, not
        to the cooperative cancel terminal payload returned by the controller.
    """

    return task_state_blocks_retry(state)


def _local_recovery_failure_category(status: BatchTaskStatus) -> FailureCategory:
    if status == BatchTaskStatus.STARTING_SANDBOX:
        return FailureCategory.SANDBOX_CREATE
    if status == BatchTaskStatus.BOOTSTRAPPING_DEVICE:
        return FailureCategory.DEVICE_BOOTSTRAP
    if status == BatchTaskStatus.WAITING_DEVICE:
        return FailureCategory.READINESS_TIMEOUT
    return FailureCategory.MAIN_AGENT
