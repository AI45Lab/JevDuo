"""Experiment recovery plan generation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from northcua.batch.contracts import BatchManifest, BatchTaskSpec, BatchTaskState, BatchTaskStatus
from northcua.experiments.contracts import ExperimentSpec, ExperimentTaskResult
from northcua.experiments.recovery.policy import DefaultFailurePolicy
from northcua.experiments.recovery.types import RecoveryPlan, RecoveryPlanItem
from northcua.reliability.contracts import FailureAction


class ExperimentRecoveryPlanner:
    """Build a deterministic recovery plan from local state and verification."""

    def __init__(self, *, policy: DefaultFailurePolicy | None = None) -> None:
        self._policy = policy or DefaultFailurePolicy()

    def plan(
        self,
        *,
        spec: ExperimentSpec,
        manifest: BatchManifest,
        output_dir: Path,
        states: list[BatchTaskState],
        verification: list[ExperimentTaskResult] | None = None,
    ) -> RecoveryPlan:
        """Generate a recovery plan for one persisted experiment.

        Args:
            spec: Experiment spec loaded from ``experiment_spec.json``.
            manifest: Batch manifest loaded from ``batch_manifest.json``.
            output_dir: Concrete experiment output directory.
            states: Latest SQLite task states.
            verification: Optional verifier rows from the current artifacts.

        Returns:
            Typed recovery plan with one item per experiment task.
        """

        states_by_id = {state.task_id: state for state in states}
        verification_by_id = {row.task_id: row for row in verification or []}
        task_specs = {task.task_id: task for task in manifest.tasks}
        items = [
            self._plan_item(
                task_id=task.task_id,
                task=task_specs.get(task.task_id),
                state=states_by_id.get(task.task_id),
                verification=verification_by_id.get(task.task_id),
                default_max_retries=manifest.defaults.max_retries,
            )
            for task in spec.tasks
        ]
        summary = Counter(item.action.value for item in items)
        return RecoveryPlan(
            experiment_id=spec.experiment_id,
            batch_id=manifest.batch_id,
            output_dir=str(output_dir),
            items=items,
            summary=dict(summary),
        )

    def _plan_item(
        self,
        *,
        task_id: str,
        task: BatchTaskSpec | None,
        state: BatchTaskState | None,
        verification: ExperimentTaskResult | None,
        default_max_retries: int,
    ) -> RecoveryPlanItem:
        max_retries = task.max_retries if task is not None and task.max_retries >= 0 else default_max_retries
        max_attempts = max_retries + 1
        decision = self._policy.decide(state=state, verification=verification, max_attempts=max_attempts)
        current_status = state.status if state is not None else BatchTaskStatus.PENDING
        return RecoveryPlanItem(
            task_id=task_id,
            current_status=current_status,
            action=decision.action,
            phase=decision.phase,
            failure_category=decision.failure_category,
            attempts=state.attempts if state is not None else 0,
            retry_budget_remaining=decision.retry_budget_remaining,
            controller_task_id=state.controller_task_id if state is not None else "",
            trace_id=state.trace_id if state is not None else "",
            session_id=state.session_id if state is not None else "",
            reason=decision.reason,
            runnable=decision.action == FailureAction.RETRY_CASE and decision.retry_budget_remaining > 0,
            evaluator_only=decision.evaluator_only,
            terminal=decision.terminal,
            errors=verification.errors if verification is not None else [],
        )
