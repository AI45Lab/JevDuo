"""Recovery plan models for experiment resume and audit reports."""

from __future__ import annotations

from pydantic import BaseModel, Field

from northcua.batch.contracts import BatchTaskStatus
from northcua.reliability.contracts import FailureAction, RecoverablePhase
from northcua.telemetry.contracts import FailureCategory


class RecoveryPlanItem(BaseModel):
    """One case-level recovery decision.

    Attributes:
        task_id: Experiment and batch task id.
        current_status: Latest local batch status for this task.
        action: Recovery action selected by the failure policy.
        phase: Phase that explains why the action was selected.
        failure_category: Normalized failure bucket when known.
        attempts: Attempts already recorded for the task.
        retry_budget_remaining: Whole-case retry budget remaining.
        controller_task_id: Previous controller task id when available.
        trace_id: Previous trace id when available.
        session_id: Previous session id when available.
        reason: Operator-facing explanation.
        runnable: Whether ``experiment resume`` should expect scheduler work.
        evaluator_only: Whether the case can be recovered after main-agent success.
        terminal: Whether the task should not be retried automatically.
        errors: Verifier errors that influenced the decision.
    """

    task_id: str
    current_status: BatchTaskStatus = BatchTaskStatus.PENDING
    action: FailureAction
    phase: RecoverablePhase
    failure_category: FailureCategory | None = None
    attempts: int = 0
    retry_budget_remaining: int = 0
    controller_task_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    reason: str = ""
    runnable: bool = False
    evaluator_only: bool = False
    terminal: bool = False
    errors: list[str] = Field(default_factory=list)


class RecoveryPlan(BaseModel):
    """Full recovery plan generated before an experiment resume.

    Attributes:
        experiment_id: Experiment namespace.
        batch_id: Batch namespace.
        output_dir: Concrete experiment output directory.
        items: Per-case recovery decisions.
        summary: Counts by recovery action.
    """

    experiment_id: str
    batch_id: str
    output_dir: str
    items: list[RecoveryPlanItem]
    summary: dict[str, int] = Field(default_factory=dict)

    @property
    def runnable_count(self) -> int:
        """Return the number of cases that require scheduler work.

        Returns:
            Count of plan items whose recovery action needs a whole-case rerun.
        """

        return sum(1 for item in self.items if item.runnable)
