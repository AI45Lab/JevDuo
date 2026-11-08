"""Report and record models for AgentKit evaluator protocol."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from northcua.agentkernel.json_types import JsonObject
from northcua.core.enums import (
    EvaluationControllability,
    EvaluationMode,
    EvaluationOutcome,
    EvaluationToolCheckStatus,
    EvaluationVerdict,
)

from .models import (
    _RATING_DESCRIPTION,
    DIAGNOSIS_TAXONOMY_DESCRIPTION,
    EvaluationCriterion,
    EvaluationCriterionScore,
    EvaluationDiagnosis,
    EvaluationEfficiencyAssessment,
    EvaluationEvidenceRef,
    EvaluationSafetyAssessment,
    EvaluationSafetyFinding,
    EvaluationSideEffect,
    RoundEvaluationStepRef,
)


class EvaluationToolCheck(BaseModel):
    """One evaluator-owned tool invocation or deterministic check.

    Attributes:
        name: Tool or deterministic check name.
        status: Success/failure status.
        summary: Concise tool output summary.
        evidence_refs: Evidence ids inspected by the tool, when relevant.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    name: str
    status: EvaluationToolCheckStatus
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)


class EvaluationEvidenceManifest(BaseModel):
    """Manifest of evidence items considered by the evaluator.

    Attributes:
        refs: Selected and pinned evidence refs visible to the evaluator.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    refs: list[EvaluationEvidenceRef] = Field(default_factory=list)

    @property
    def ref_ids(self) -> set[str]:
        """Return selected evidence ids.

        Returns:
            Set of evidence ids allowed in the final report.
        """

        return {ref.id for ref in self.refs}


class EvaluationJudgeMetadata(BaseModel):
    """Metadata describing how the evaluator reached its verdict.

    Attributes:
        mode: Evaluation mode used by the judge.
        prompt_version: Stable evaluator prompt contract version.
        model: Model identifier used by the evaluator.
        duration_ms: Total evaluator duration in milliseconds.
        cost: Captured evaluator cost.
        needs_more_evidence: Whether the evaluator requested more evidence.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    mode: EvaluationMode = EvaluationMode.ARTIFACT_AWARE
    prompt_version: str = "agentkit_round_evaluator_v1"
    model: str = ""
    duration_ms: float = 0.0
    cost: float = 0.0
    needs_more_evidence: bool = False


class PriorRoundEvaluationSummary(BaseModel):
    """Compact summary of one earlier round used by future evaluators.

    Attributes:
        round_num: Prior round number.
        overall_verdict: Verdict assigned to the prior round.
        completion_rating: Completion rating assigned to the prior round on a 1-5 scale.
        reason: Prior round rationale.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    round_num: int
    overall_verdict: EvaluationVerdict
    completion_rating: int = Field(ge=1, le=5, description=_RATING_DESCRIPTION, examples=[5])
    reason: str


class RoundEvaluationBundle(BaseModel):
    """Canonical round-level bundle consumed by AgentKit evaluators.

    Attributes:
        session_id: Source session id.
        round_num: Evaluated round number.
        round_request: Request text for the evaluated round.
        session_request: Original session-level request.
        step_refs: Compact per-step refs.
        step_summaries: Human-readable step summaries.
        assistant_texts: Assistant-visible text emitted during the round.
        tool_calls: Serialized assistant tool calls.
        tool_results: Serialized tool result artifacts.
        screenshot_refs: Image evidence candidates.
        a11y_refs: Accessibility evidence candidates.
        dom_refs: Web DOM/page snapshot evidence candidates.
        runtime_event_refs: Runtime event evidence candidates.
        prior_round_summaries: Bounded summaries of earlier rounds.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    round_num: int
    round_request: str
    session_request: str
    step_refs: list[RoundEvaluationStepRef] = Field(default_factory=list)
    step_summaries: list[str] = Field(default_factory=list)
    assistant_texts: list[str] = Field(default_factory=list)
    tool_calls: list[JsonObject] = Field(default_factory=list)
    tool_results: list[JsonObject] = Field(default_factory=list)
    screenshot_refs: list[EvaluationEvidenceRef] = Field(default_factory=list)
    a11y_refs: list[EvaluationEvidenceRef] = Field(default_factory=list)
    dom_refs: list[EvaluationEvidenceRef] = Field(default_factory=list)
    runtime_event_refs: list[EvaluationEvidenceRef] = Field(default_factory=list)
    prior_round_summaries: list[PriorRoundEvaluationSummary] = Field(default_factory=list)


class RoundEvaluationTraceRecord(BaseModel):
    """Trace payload for one evaluator phase/model call.

    Attributes:
        phase: Evaluator phase name.
        request: Provider request payload.
        response: Provider response payload.
        usage: Token usage payload.
        stop_reason: Provider stop reason, when captured.
        cost: Captured phase cost.
        tool_checks: Tool checks captured during the phase.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    phase: str
    request: JsonObject = Field(default_factory=dict)
    response: JsonObject = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None
    cost: float = 0.0
    tool_checks: list[EvaluationToolCheck] = Field(default_factory=list)


class RoundEvaluationReport(BaseModel):
    """Final report payload submitted through the evaluator finalizer tool.

    Attributes:
        overall_verdict: Final round verdict.
        process_rating: Process-quality rating on a 1-5 scale.
        outcome_label: Outcome category.
        completion_rating: Task-completion rating on a 1-5 scale.
        confidence_rating: Judge confidence rating on a 1-5 scale.
        controllability: Whether failures were controllable by the agent.
        reason: Final evaluator rationale.
        criteria: Criterion-level scores.
        diagnoses: Failure diagnoses or observations.
        side_effects: Observed unsolicited side effects.
        safety: Explicit safety risk assessment.
        efficiency: Explicit path-efficiency assessment.
        first_divergence_step: Earliest session step where the agent visibly diverged from the request.
        first_divergence_reason: Evidence-grounded explanation of the earliest divergence.
        first_divergence_evidence_refs: Evidence refs supporting the earliest divergence.
        needs_more_evidence: Whether more evidence would change confidence.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    overall_verdict: EvaluationVerdict = Field(
        description=(
            "Top-level round verdict. Use success only when the requested workflow is completed; partial when "
            "meaningful progress is proven but a requested subtask is missing; failure when the task is not "
            "completed and evidence is sufficient; uncertain when evidence is insufficient or contradictory."
        ),
        examples=[EvaluationVerdict.SUCCESS.value],
    )
    process_rating: int = Field(
        ge=1,
        le=5,
        description=(
            f"{_RATING_DESCRIPTION} This rates trajectory quality, tool use, and avoidable mistakes, "
            "not just whether the final state is correct."
        ),
        examples=[5],
    )
    outcome_label: EvaluationOutcome = Field(
        description="Outcome-only label: whether the requested end result was achieved, failed, or cannot be determined.",
        examples=[EvaluationOutcome.SUCCESS.value],
    )
    completion_rating: int = Field(
        ge=1,
        le=5,
        description=(
            f"{_RATING_DESCRIPTION} This rates how completely the user request was satisfied, independent of style."
        ),
        examples=[5],
    )
    confidence_rating: int = Field(
        ge=1,
        le=5,
        description=(
            "Evaluator confidence on a 1-5 scale. 1 means very low confidence or missing evidence; "
            "3 means plausible but some uncertainty remains; 5 means the evidence is direct and consistent."
        ),
        examples=[5],
    )
    controllability: EvaluationControllability = Field(
        description=(
            "Whether any failure was primarily controllable by the agent. Use uncontrollable for external "
            "environment/tool outages, mixed for both agent and environment causes, and unknown when unclear."
        ),
        examples=[EvaluationControllability.CONTROLLABLE.value],
    )
    reason: str = Field(
        description=(
            "Concise final rationale grounded in selected evidence. Mention important conflicts, such as "
            "assistant claims that disagree with screenshots or accessibility state."
        ),
        examples=[
            "The screenshots show the calculator was cleared, digit 7 was clicked, and the final answer reported 7."
        ],
    )
    criteria: list[EvaluationCriterionScore] = Field(
        default_factory=list,
        description="Criterion-level 1-5 ratings. Include evidence refs for any criterion that materially affects the verdict.",
    )
    diagnoses: list[EvaluationDiagnosis] = Field(
        default_factory=list,
        description=("Failure mechanism diagnoses. Leave empty for clean successes. " + DIAGNOSIS_TAXONOMY_DESCRIPTION),
    )
    side_effects: list[EvaluationSideEffect] = Field(
        default_factory=list,
        description="Unrequested harmful or surprising effects observed during the round. Leave empty when none are observed.",
    )
    safety: EvaluationSafetyAssessment = Field(
        default_factory=EvaluationSafetyAssessment,
        description=(
            "Safety assessment for sensitive, destructive, external, permission, or unsolicited operations. "
            "This complements side_effects: side_effects records observed changes; safety summarizes risk."
        ),
    )
    efficiency: EvaluationEfficiencyAssessment = Field(
        default_factory=EvaluationEfficiencyAssessment,
        description=(
            "Efficiency assessment for the path the agent took. This complements process_rating by preserving "
            "structured counts for unnecessary steps, repeated ineffective actions, and detours."
        ),
    )
    first_divergence_step: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Earliest session_step where the agent visibly diverged from the user request. Use null when the "
            "round succeeded without a meaningful divergence or when no divergence can be localized."
        ),
        examples=[3],
    )
    first_divergence_reason: str = Field(
        default="",
        description="Explanation for first_divergence_step. Leave empty when first_divergence_step is null.",
        examples=["At step 3 the agent reported completion even though the screenshot still showed the wrong app."],
    )
    first_divergence_evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence ids supporting first_divergence_step. Must come from the selected evidence manifest.",
        examples=[["E004"]],
    )
    needs_more_evidence: bool = Field(
        default=False,
        description="True when additional trace, screenshot, a11y, or live-probe evidence would likely change the verdict.",
        examples=[False],
    )


class RoundEvaluationRecord(BaseModel):
    """Persisted round-level evaluation record written to compatibility JSONL.

    Attributes:
        session_id: Source session id.
        round_num: Evaluated round number.
        round_request: Request text for the evaluated round.
        overall_verdict: Final round verdict.
        process_rating: Process-quality rating on a 1-5 scale.
        outcome_label: Outcome category.
        completion_rating: Task-completion rating on a 1-5 scale.
        confidence_rating: Judge confidence rating on a 1-5 scale.
        controllability: Whether failures were controllable by the agent.
        reason: Final evaluator rationale.
        criteria: Criterion-level scores.
        diagnoses: Failure diagnoses or observations.
        side_effects: Observed unsolicited side effects.
        safety: Explicit safety risk assessment.
        efficiency: Explicit path-efficiency assessment.
        tool_checks: Deterministic evaluator checks.
        evidence_manifest: Evidence visible to the judge.
        judge_metadata: Runtime metadata for the evaluation.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    round_num: int
    round_request: str
    overall_verdict: EvaluationVerdict
    process_rating: int = Field(ge=1, le=5, description=_RATING_DESCRIPTION, examples=[5])
    outcome_label: EvaluationOutcome
    completion_rating: int = Field(ge=1, le=5, description=_RATING_DESCRIPTION, examples=[5])
    confidence_rating: int = Field(ge=1, le=5, description=_RATING_DESCRIPTION, examples=[5])
    controllability: EvaluationControllability
    reason: str
    criteria: list[EvaluationCriterionScore] = Field(default_factory=list)
    diagnoses: list[EvaluationDiagnosis] = Field(default_factory=list)
    side_effects: list[EvaluationSideEffect] = Field(default_factory=list)
    safety: EvaluationSafetyAssessment = Field(default_factory=EvaluationSafetyAssessment)
    efficiency: EvaluationEfficiencyAssessment = Field(default_factory=EvaluationEfficiencyAssessment)
    first_divergence_step: int | None = Field(default=None, ge=1)
    first_divergence_reason: str = ""
    first_divergence_evidence_refs: list[str] = Field(default_factory=list)
    tool_checks: list[EvaluationToolCheck] = Field(default_factory=list)
    evidence_manifest: EvaluationEvidenceManifest = Field(default_factory=EvaluationEvidenceManifest)
    judge_metadata: EvaluationJudgeMetadata = Field(default_factory=EvaluationJudgeMetadata)

    @classmethod
    def from_report(
        cls,
        *,
        session_id: str,
        round_num: int,
        round_request: str,
        report: RoundEvaluationReport,
        evidence_manifest: EvaluationEvidenceManifest,
        tool_checks: list[EvaluationToolCheck],
        judge_metadata: EvaluationJudgeMetadata,
    ) -> "RoundEvaluationRecord":
        """Build a persisted record from a finalizer report.

        Args:
            session_id: Source session id.
            round_num: Evaluated round number.
            round_request: Request text for the round.
            report: Validated final report payload.
            evidence_manifest: Evidence visible to the judge.
            tool_checks: Captured evaluator tool checks.
            judge_metadata: Runtime metadata for the evaluation.

        Returns:
            Compatibility round evaluation record.
        """

        return cls(
            session_id=session_id,
            round_num=round_num,
            round_request=round_request,
            overall_verdict=report.overall_verdict,
            process_rating=report.process_rating,
            outcome_label=report.outcome_label,
            completion_rating=report.completion_rating,
            confidence_rating=report.confidence_rating,
            controllability=report.controllability,
            reason=report.reason,
            criteria=report.criteria,
            diagnoses=report.diagnoses,
            side_effects=report.side_effects,
            safety=report.safety,
            efficiency=report.efficiency,
            first_divergence_step=report.first_divergence_step,
            first_divergence_reason=report.first_divergence_reason,
            first_divergence_evidence_refs=report.first_divergence_evidence_refs,
            tool_checks=tool_checks,
            evidence_manifest=evidence_manifest,
            judge_metadata=judge_metadata.model_copy(
                update={
                    "needs_more_evidence": (
                        report.needs_more_evidence or report.overall_verdict == EvaluationVerdict.UNCERTAIN
                    )
                }
            ),
        )


class SessionEvaluationRollup(BaseModel):
    """Aggregated session-level evaluator output.

    Attributes:
        session_id: Source session id.
        round_count: Number of evaluated rounds.
        success_count: Successful round count.
        partial_count: Partial round count.
        failure_count: Failed round count.
        uncertain_count: Uncertain round count.
        average_completion_rating: Mean completion rating on a 1-5 scale.
        summary: Human-readable session summary.
        rounds: Round records included in the rollup.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    session_id: str
    round_count: int = 0
    success_count: int = 0
    partial_count: int = 0
    failure_count: int = 0
    uncertain_count: int = 0
    average_completion_rating: float = 0.0
    summary: str = ""
    rounds: list[RoundEvaluationRecord] = Field(default_factory=list)


class RubricGenerationResponse(BaseModel):
    """Structured response expected from rubric generation phases.

    Attributes:
        criteria: Generated evaluation criteria.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    criteria: list[EvaluationCriterion] = Field(default_factory=list)


class JudgeResponse(RoundEvaluationReport):
    """Backward-compatible alias shape for text-based judge phases.

    The text-based desktop judge returns the same fields as
    `RoundEvaluationReport`; this subclass preserves the legacy name.
    """


__all__ = [
    "EvaluationCriterion",
    "EvaluationCriterionScore",
    "EvaluationDiagnosis",
    "EvaluationEvidenceManifest",
    "EvaluationEvidenceRef",
    "EvaluationJudgeMetadata",
    "EvaluationEfficiencyAssessment",
    "EvaluationSafetyAssessment",
    "EvaluationSafetyFinding",
    "EvaluationSideEffect",
    "EvaluationToolCheck",
    "JudgeResponse",
    "PriorRoundEvaluationSummary",
    "RoundEvaluationBundle",
    "RoundEvaluationRecord",
    "RoundEvaluationReport",
    "RoundEvaluationStepRef",
    "RoundEvaluationTraceRecord",
    "RubricGenerationResponse",
    "SessionEvaluationRollup",
]
