"""Post-processing helpers for judge outputs."""

from __future__ import annotations

from northcua.core.config import EvaluationJudgeConfig
from northcua.core.enums import EvaluationControllability

from .protocol import (
    EvaluationCriterionScore,
    EvaluationDiagnosis,
    EvaluationEfficiencyAssessment,
    EvaluationEvidenceManifest,
    EvaluationSafetyAssessment,
    EvaluationSafetyFinding,
    EvaluationSideEffect,
    JudgeResponse,
)


class DiagnosisBuilder:
    """Normalize judge outputs against the selected evidence manifest."""

    def __init__(self, config: EvaluationJudgeConfig) -> None:
        """Initialize diagnosis normalization.

        Args:
            config: Judge configuration controlling evidence requirements.
        """

        self.config = config

    def normalize(self, response: JudgeResponse, manifest: EvaluationEvidenceManifest) -> JudgeResponse:
        """Drop invalid evidence refs and enforce configured constraints.

        Args:
            response: Raw judge response to normalize in place.
            manifest: Evidence manifest containing valid evidence ids.

        Returns:
            Normalized judge response.
        """

        valid_refs = {ref.id for ref in manifest.refs}
        response.criteria = [self._normalize_criterion(item, valid_refs) for item in response.criteria]
        response.diagnoses = [self._normalize_diagnosis(item, valid_refs) for item in response.diagnoses]
        response.side_effects = [self._normalize_side_effect(item, valid_refs) for item in response.side_effects]
        response.safety = self._normalize_safety(response.safety, valid_refs)
        response.efficiency = self._normalize_efficiency(response.efficiency, valid_refs)
        if not response.reason.strip():
            response.reason = "The evaluator could not produce a grounded round-level reason."
        if not response.criteria:
            response.controllability = EvaluationControllability.UNKNOWN
        return response

    def _normalize_criterion(self, score: EvaluationCriterionScore, valid_refs: set[str]) -> EvaluationCriterionScore:
        """Retain only evidence refs selected into the final manifest.

        Args:
            score: Criterion score emitted by the judge.
            valid_refs: Evidence ids selected for artifact-aware scoring.

        Returns:
            The normalized criterion score.
        """

        score.evidence_refs = [ref for ref in score.evidence_refs if ref in valid_refs]
        if self.config.require_evidence_refs and not score.evidence_refs:
            score.reason = f"{score.reason.strip()} No grounded evidence refs were retained."
        return score

    def _normalize_diagnosis(self, diagnosis: EvaluationDiagnosis, valid_refs: set[str]) -> EvaluationDiagnosis:
        """Retain only selected evidence refs for one diagnosis.

        Args:
            diagnosis: Diagnosis emitted by the judge.
            valid_refs: Evidence ids selected for artifact-aware scoring.

        Returns:
            The normalized diagnosis.
        """

        diagnosis.evidence_refs = [ref for ref in diagnosis.evidence_refs if ref in valid_refs]
        if self.config.require_evidence_refs and not diagnosis.evidence_refs:
            diagnosis.reason = f"{diagnosis.reason.strip()} Evidence refs were unavailable after normalization."
        return diagnosis

    def _normalize_side_effect(self, side_effect: EvaluationSideEffect, valid_refs: set[str]) -> EvaluationSideEffect:
        """Retain only selected evidence refs for one side-effect finding.

        Args:
            side_effect: Side-effect finding emitted by the judge.
            valid_refs: Evidence ids selected for artifact-aware scoring.

        Returns:
            The normalized side-effect finding.
        """

        side_effect.evidence_refs = [ref for ref in side_effect.evidence_refs if ref in valid_refs]
        if self.config.require_evidence_refs and not side_effect.evidence_refs:
            side_effect.reason = f"{side_effect.reason.strip()} Evidence refs were unavailable after normalization."
        return side_effect

    def _normalize_safety(self, safety: EvaluationSafetyAssessment, valid_refs: set[str]) -> EvaluationSafetyAssessment:
        """Retain only selected evidence refs for safety findings.

        Args:
            safety: Safety assessment emitted by the judge.
            valid_refs: Evidence ids selected for artifact-aware scoring.

        Returns:
            Normalized safety assessment.
        """

        safety.evidence_refs = [ref for ref in safety.evidence_refs if ref in valid_refs]
        safety.findings = [self._normalize_safety_finding(item, valid_refs) for item in safety.findings]
        if self.config.require_evidence_refs and safety.risk_level.value != "none" and not safety.evidence_refs:
            safety.reason = f"{safety.reason.strip()} Evidence refs were unavailable after normalization."
        return safety

    def _normalize_safety_finding(
        self, finding: EvaluationSafetyFinding, valid_refs: set[str]
    ) -> EvaluationSafetyFinding:
        """Retain only selected evidence refs for one safety finding."""

        finding.evidence_refs = [ref for ref in finding.evidence_refs if ref in valid_refs]
        if self.config.require_evidence_refs and not finding.evidence_refs:
            finding.reason = f"{finding.reason.strip()} Evidence refs were unavailable after normalization."
        return finding

    def _normalize_efficiency(
        self, efficiency: EvaluationEfficiencyAssessment, valid_refs: set[str]
    ) -> EvaluationEfficiencyAssessment:
        """Retain only selected evidence refs for efficiency assessment."""

        efficiency.evidence_refs = [ref for ref in efficiency.evidence_refs if ref in valid_refs]
        if self.config.require_evidence_refs and efficiency.rating < 5 and not efficiency.evidence_refs:
            efficiency.reason = f"{efficiency.reason.strip()} Evidence refs were unavailable after normalization."
        return efficiency
