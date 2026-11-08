"""Domain-neutral evaluator enum contracts."""

from __future__ import annotations

from enum import Enum

from northcua.core.enums import (
    DiagnosisCategory,
    EvaluationControllability,
    EvaluationEvidenceKind,
    EvaluationMode,
    EvaluationOutcome,
    EvaluationSafetyCategory,
    EvaluationSafetyRiskLevel,
    EvaluationSchedule,
    EvaluationSeverity,
    EvaluationToolCheckStatus,
    EvaluationToolPlacement,
    EvaluationVerdict,
)


class EvaluationInputScope(str, Enum):
    """Input scope evaluated by an AgentKit evaluator."""

    ROUND = "round"
    SESSION = "session"
    TASK = "task"


class EvidenceSelectionReason(str, Enum):
    """Why an evidence ref was selected into the final manifest."""

    PINNED_INITIAL_SCREENSHOT = "pinned_initial_screenshot"
    PINNED_LAST_SCREENSHOT = "pinned_last_screenshot"
    PINNED_REQUIRED_EVIDENCE = "pinned_required_evidence"
    PINNED_POLICY = "pinned_policy"
    SELECTED_BY_POLICY = "selected_by_policy"


__all__ = [
    "DiagnosisCategory",
    "EvaluationControllability",
    "EvaluationEvidenceKind",
    "EvaluationInputScope",
    "EvaluationMode",
    "EvaluationOutcome",
    "EvaluationSafetyCategory",
    "EvaluationSafetyRiskLevel",
    "EvaluationSchedule",
    "EvaluationSeverity",
    "EvaluationToolCheckStatus",
    "EvaluationToolPlacement",
    "EvaluationVerdict",
    "EvidenceSelectionReason",
]
