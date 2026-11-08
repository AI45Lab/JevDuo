"""Default failure policy for E2B experiment recovery."""

from __future__ import annotations

from northcua.batch.contracts import BatchTaskState, BatchTaskStatus
from northcua.experiments.contracts import ExperimentTaskResult
from northcua.reliability.contracts import FailureAction, RecoverablePhase, RecoveryDecision
from northcua.telemetry.contracts import FailureCategory


class DefaultFailurePolicy:
    """Map task state and verifier results to a conservative recovery action."""

    def decide(
        self,
        *,
        state: BatchTaskState | None,
        verification: ExperimentTaskResult | None,
        max_attempts: int,
    ) -> RecoveryDecision:
        """Return the recovery decision for one case.

        Args:
            state: Latest batch state, or ``None`` when the task never entered
                the scheduler.
            verification: Latest artifact verification row when available.
            max_attempts: Total whole-case attempt budget.

        Returns:
            Policy decision used by ``experiment recover-plan`` and resume.
        """

        if state is None:
            return RecoveryDecision(
                action=FailureAction.RETRY_CASE,
                phase=RecoverablePhase.PREFLIGHT,
                failure_category=FailureCategory.PREFLIGHT,
                reason="no batch state exists for this task",
                retry_budget_remaining=max(max_attempts, 0),
            )
        if state.recovery_action == FailureAction.FAIL_FAST or _has_non_retryable_terminal_decision(state):
            return RecoveryDecision(
                action=FailureAction.FAIL_FAST,
                phase=self._phase_for_failed_state(state),
                failure_category=self._failure_category_for_state(state),
                reason="task has a non-retryable terminal decision",
                retry_budget_remaining=0,
                terminal=True,
            )
        remaining = max(max_attempts - state.attempts, 0)
        if state.status == BatchTaskStatus.CANCELED:
            return RecoveryDecision(
                action=FailureAction.MARK_CANCELED,
                phase=RecoverablePhase.MAIN_AGENT,
                failure_category=FailureCategory.UNKNOWN,
                reason="task was canceled cooperatively",
                retry_budget_remaining=remaining,
                terminal=True,
            )
        if state.status == BatchTaskStatus.SUCCEEDED:
            return self._after_success(state=state, verification=verification)
        if remaining <= 0:
            return RecoveryDecision(
                action=FailureAction.FAIL_FAST,
                phase=self._phase_for_failed_state(state),
                failure_category=self._failure_category_for_state(state),
                reason="retry budget exhausted",
                retry_budget_remaining=0,
                terminal=True,
            )
        return RecoveryDecision(
            action=FailureAction.RETRY_CASE,
            phase=self._phase_for_failed_state(state),
            failure_category=self._failure_category_for_state(state),
            reason="case can be rerun with a fresh sandbox attempt",
            retry_budget_remaining=remaining,
        )

    def _after_success(
        self,
        *,
        state: BatchTaskState,
        verification: ExperimentTaskResult | None,
    ) -> RecoveryDecision:
        if verification is None or verification.passed:
            return RecoveryDecision(
                action=FailureAction.SKIP_SUCCEEDED,
                phase=RecoverablePhase.ARTIFACT_VERIFICATION,
                reason="main agent and verifier outputs are complete",
                retry_budget_remaining=0,
                terminal=True,
            )
        errors = " ".join(verification.errors).lower()
        if "evaluator" in errors and verification.main_steps_path and verification.main_llm_trace_path:
            return RecoveryDecision(
                action=FailureAction.RETRY_EVALUATOR,
                phase=RecoverablePhase.EVALUATOR,
                failure_category=FailureCategory.EVALUATOR,
                reason="main trace is complete but evaluator artifacts are missing or failed",
                retry_budget_remaining=0,
                evaluator_only=True,
            )
        return RecoveryDecision(
            action=FailureAction.REMATERIALIZE_ARTIFACTS,
            phase=RecoverablePhase.ARTIFACT_VERIFICATION,
            failure_category=FailureCategory.ARTIFACT_MISSING,
            reason="task succeeded but required artifacts are incomplete",
            retry_budget_remaining=0,
            evaluator_only=True,
        )

    @staticmethod
    def _phase_for_failed_state(state: BatchTaskState) -> RecoverablePhase:
        if state.status == BatchTaskStatus.STARTING_SANDBOX:
            return RecoverablePhase.SANDBOX_CREATE
        if state.status == BatchTaskStatus.BOOTSTRAPPING_DEVICE:
            return RecoverablePhase.WORKER_BOOTSTRAP
        if state.status == BatchTaskStatus.WAITING_DEVICE:
            return RecoverablePhase.WORKER_READY
        if state.status == BatchTaskStatus.SUBMITTED:
            return RecoverablePhase.CONTROLLER_SUBMIT
        return RecoverablePhase.MAIN_AGENT

    @staticmethod
    def _failure_category_for_state(state: BatchTaskState) -> FailureCategory:
        if state.last_failure_category is not None:
            return state.last_failure_category
        if state.status == BatchTaskStatus.TIMED_OUT:
            return FailureCategory.TIMEOUT
        if state.status == BatchTaskStatus.STARTING_SANDBOX:
            return FailureCategory.SANDBOX_CREATE
        if state.status in {BatchTaskStatus.BOOTSTRAPPING_DEVICE, BatchTaskStatus.WAITING_DEVICE}:
            return FailureCategory.DEVICE_BOOTSTRAP
        if state.status == BatchTaskStatus.SUBMITTED:
            return FailureCategory.CONTROLLER_SUBMIT
        return FailureCategory.MAIN_AGENT if state.controller_task_id else FailureCategory.UNKNOWN


def _has_non_retryable_terminal_decision(state: BatchTaskState) -> bool:
    """Return whether the state carries an explicit non-retryable decision."""

    decision = state.terminal_decision
    return isinstance(decision, dict) and decision.get("retryable") is False
