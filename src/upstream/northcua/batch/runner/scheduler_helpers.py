"""Claim reconciliation and prepared-attempt helpers for the batch scheduler."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.batch.runner.events import BatchAttemptEventMessage, BatchEventWriter
from northcua.batch.runner.retry_backoff import phase_for_backoff, with_infra_retry_backoff
from northcua.batch.state import BatchStateRepository, BatchTaskLease
from northcua.batch.state.async_repository import repository_call
from northcua.reliability import FailureAction
from northcua.sandboxes.contracts import SandboxAllocationStrategy, SandboxDeviceBinding
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager
from northcua.telemetry.contracts import CaseStatus
from northcua_protocol.capabilities import Capability

if TYPE_CHECKING:
    from northcua.batch.runner.bindings import SandboxDeviceBindingFactory
    from northcua.batch.runner.resume import BatchStatusReconciler
    from northcua.sandboxes.device.readiness import DeviceReadinessProbe
    from northcua.sandboxes.provider.registry import SandboxProviderRegistry

logger = logging.getLogger(__name__)

_TERMINAL_RECONCILED_CONTROLLER_STATUSES = {
    "failed",
    "finished",
    "interrupted",
}
_RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS = "release_current_lease_after_duplicate_reconcile"


class BatchSchedulerHelperMixin:
    """Private claim release, resume reconciliation, and pool setup helpers."""

    _providers: "SandboxProviderRegistry"
    _binding_factory: "SandboxDeviceBindingFactory"
    _resume_reconciler: "BatchStatusReconciler | None"
    _readiness_probe: "DeviceReadinessProbe | None"

    @staticmethod
    def _total_attempt_budget(*, config: BatchRunConfig, task: BatchTaskSpec) -> int:
        """Return the maximum number of attempts allowed for a task."""

        raise NotImplementedError

    @staticmethod
    def _release_claim(
        *,
        repository: BatchStateRepository,
        claim: BatchTaskLease,
        state: BatchTaskState,
        runner_id: str,
    ) -> None:
        """Release a task claim and log ownership mismatches."""

        current = repository.get(claim.task_id)
        if (
            current is not None
            and current.cleanup_status
            and (
                not state.cleanup_status
                or state.cleanup_status == _RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS
            )
        ):
            state = state.model_copy(update={"cleanup_status": current.cleanup_status})
        if not repository.release_claim(claim, terminal_state=state):
            logger.warning(
                "batch task lease release rejected",
                extra={"task_id": claim.task_id, "runner_id": runner_id},
            )

    @staticmethod
    async def _release_claim_async(
        *,
        repository: BatchStateRepository,
        claim: BatchTaskLease,
        state: BatchTaskState,
        runner_id: str,
    ) -> None:
        """Release a task claim without blocking the event loop on SQLite."""

        current = await repository_call(lambda: repository.get(claim.task_id))
        if (
            current is not None
            and current.cleanup_status
            and (
                not state.cleanup_status
                or state.cleanup_status == _RELEASE_CURRENT_LEASE_AFTER_DUPLICATE_RECONCILE_STATUS
            )
        ):
            state = state.model_copy(update={"cleanup_status": current.cleanup_status})
        released = await repository_call(lambda: repository.release_claim(claim, terminal_state=state))
        if not released:
            logger.warning(
                "batch task lease release rejected",
                extra={"task_id": claim.task_id, "runner_id": runner_id},
            )

    async def _reconcile_existing(
        self,
        *,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        events: BatchEventWriter,
        existing: BatchTaskState,
        status_semaphore: asyncio.Semaphore | None = None,
        defer_fresh_retry: bool = False,
    ) -> BatchTaskState | None:
        if existing.status == BatchTaskStatus.SUCCEEDED:
            return existing
        if self._resume_reconciler is None:
            return None
        reconciled = await self._resume_reconciler.reconcile(
            config=config,
            task=task,
            existing=existing,
            status_semaphore=status_semaphore,
        )
        if reconciled is None:
            return None
        await events.upsert_task_async(reconciled)
        if reconciled.status in {BatchTaskStatus.SUCCEEDED, BatchTaskStatus.CANCELED}:
            return reconciled
        if reconciled.status in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
            return reconciled
        if reconciled.recovery_action == FailureAction.FAIL_FAST:
            return reconciled
        if _is_delayed_reconcile_state(reconciled):
            return reconciled
        budget = self._total_attempt_budget(config=config, task=task)
        if reconciled.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT} and reconciled.attempts >= budget:
            return reconciled
        if reconciled.status in {BatchTaskStatus.FAILED, BatchTaskStatus.TIMED_OUT}:
            retry_state = _clear_terminal_controller_identity_for_retry(reconciled)
            retry_budget_remaining = max(budget - retry_state.attempts, 0)
            if retry_state.retry_budget_remaining != retry_budget_remaining:
                retry_state = retry_state.model_copy(update={"retry_budget_remaining": retry_budget_remaining})
            if defer_fresh_retry:
                delayed = with_infra_retry_backoff(
                    config=config,
                    task=task,
                    state=retry_state,
                    max_retries=max(budget - 1, 0),
                )
                if delayed != reconciled:
                    await events.upsert_task_async(delayed)
                return delayed
            delayed = with_infra_retry_backoff(
                config=config,
                task=task,
                state=retry_state,
                max_retries=max(budget - 1, 0),
            )
            if delayed.next_attempt_after > time.time():
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
                        "source": "resume_reconcile",
                    },
                    controller_task_id=delayed.controller_task_id,
                    trace_id=delayed.trace_id,
                    sandbox_id=delayed.sandbox_id,
                    device_id=delayed.device_id,
                )
                return delayed
        return None


def _clear_terminal_controller_identity_for_retry(state: BatchTaskState) -> BatchTaskState:
    """Clear old terminal controller ids before persisting a retry cooldown."""

    if state.last_reconciled_status not in _TERMINAL_RECONCILED_CONTROLLER_STATUSES:
        return state
    if not state.controller_task_id:
        return state
    return state.model_copy(
        update={
            "controller_task_id": "",
            "trace_id": "",
            "session_id": "",
            "terminal_state": None,
            "terminal_decision": {},
        }
    )


def _is_delayed_reconcile_state(state: BatchTaskState) -> bool:
    """Return true when resume should retry controller lookup before fresh submit."""

    if state.next_attempt_after > time.time():
        return True
    if state.controller_task_id and state.last_reconciled_status == "unknown":
        return True
    if state.status not in {BatchTaskStatus.SUBMITTED, BatchTaskStatus.RUNNING}:
        return False
    return False
