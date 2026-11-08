"""Typed recovery policy vocabulary for robust CUA batch execution."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from northcua.telemetry.contracts import FailureCategory


class RecoverablePhase(str, Enum):
    """Execution phases where the batch system can make recovery decisions."""

    PREFLIGHT = "preflight"
    SANDBOX_CREATE = "sandbox_create"
    FILE_UPLOAD = "file_upload"
    WORKER_BOOTSTRAP = "worker_bootstrap"
    WORKER_READY = "worker_ready"
    CONTROLLER_SUBMIT = "controller_submit"
    MAIN_AGENT = "main_agent"
    EVALUATOR = "evaluator"
    ARTIFACT_VERIFICATION = "artifact_verification"
    CLEANUP = "cleanup"


class FailureAction(str, Enum):
    """Operator-visible action selected by a recovery policy."""

    RETRY_CASE = "retry_case"
    RETRY_EVALUATOR = "retry_evaluator"
    REMATERIALIZE_ARTIFACTS = "rematerialize_artifacts"
    SKIP_SUCCEEDED = "skip_succeeded"
    FAIL_FAST = "fail_fast"
    MARK_CANCELED = "mark_canceled"


class RecoveryDecision(BaseModel):
    """One policy decision for a failed or incomplete case.

    Attributes:
        action: Recovery action the runner or operator should take.
        phase: Phase that drove the decision.
        failure_category: Normalized failure bucket when known.
        reason: Short human-readable explanation for reports and CLI output.
        retry_budget_remaining: Number of whole-case attempts still available.
        evaluator_only: Whether the decision can avoid a sandbox/main-agent rerun.
        terminal: Whether no further automated work should be attempted.
    """

    action: FailureAction
    phase: RecoverablePhase
    failure_category: FailureCategory | None = None
    reason: str = ""
    retry_budget_remaining: int = 0
    evaluator_only: bool = False
    terminal: bool = False
