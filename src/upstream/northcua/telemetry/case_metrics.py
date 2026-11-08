"""Per-case telemetry metric construction."""

from __future__ import annotations

from northcua.telemetry.artifact_metrics import CaseArtifactMetricBuilder, EvaluatorAssessmentMetricBuilder
from northcua.telemetry.contracts import (
    CaseArtifactTelemetry,
    CaseMetrics,
    CasePhase,
    CaseStatus,
    CaseTelemetrySnapshot,
    FailureCategory,
    PhaseTiming,
)
from northcua.telemetry.report_values import MetricValue, TimeValue


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


class CaseMetricBuilder:
    """Build the stable metric contract for one case."""

    def __init__(self) -> None:
        """Create a builder with artifact and evaluator report metric readers."""

        self.artifact_builder = CaseArtifactMetricBuilder()
        self.evaluator_builder = EvaluatorAssessmentMetricBuilder()

    def build(
        self,
        snapshot: CaseTelemetrySnapshot,
        timings: list[PhaseTiming],
        artifact: CaseArtifactTelemetry | None,
        manifest_metadata: dict[str, object] | None = None,
        prepared_metrics: dict[str, object] | None = None,
    ) -> CaseMetrics:
        """Build the complete ``CaseMetrics`` row for one case.

        Args:
            snapshot: Latest authoritative case state snapshot.
            timings: Phase timing rows for the case.
            artifact: Optional artifact path bundle discovered after execution.
            prepared_metrics: Optional prepared-sandbox derived metrics for this case.

        Returns:
            Normalized case metrics used by reports and downstream analytics.
        """

        metadata = manifest_metadata or {}
        prepared = prepared_metrics or {}
        phase_seconds = self._phase_seconds(timings)
        artifact_metrics = self.artifact_builder.compute(artifact)
        evaluator_metrics = self.evaluator_builder.compute(
            artifact.evaluator_report_path if artifact is not None else ""
        )
        artifact_evaluator_metrics = self._artifact_evaluator_metrics(artifact)
        expected_feasibility = str(metadata.get("expected_feasibility") or "feasible")
        terminal_reason = str(snapshot.metrics.get("terminal_reason") or "")
        expected_infeasible_success = expected_feasibility == "infeasible" and terminal_reason == "infeasible"
        status = CaseStatus.SUCCEEDED if expected_infeasible_success else snapshot.status
        # Preserve precedence: SQLite snapshot is authoritative for control-plane
        # state, while artifact/evaluator readers enrich derived report metrics.
        return CaseMetrics(
            experiment_id=snapshot.experiment_id,
            batch_id=snapshot.batch_id,
            case_id=snapshot.case_id,
            app_profile=str(metadata.get("app_profile") or ""),
            involved_app_profiles=self._string_list(metadata.get("involved_app_profiles")),
            involved_app_count=MetricValue.as_int(metadata.get("involved_app_count")),
            operation_family=str(metadata.get("operation_family") or ""),
            expected_feasibility=expected_feasibility,
            status=status,
            total_seconds=self._case_total_seconds(snapshot, timings),
            sandbox_seconds=phase_seconds.get(CasePhase.SANDBOX_CREATE, 0.0),
            prepared_sandbox_seconds=phase_seconds.get(CasePhase.PREPARED_SANDBOX, 0.0),
            prepared_sandbox_claim_wait_seconds=MetricValue.as_float(
                prepared.get("prepared_sandbox_claim_wait_seconds")
            ),
            prepared_sandbox_estimated_saved_seconds=MetricValue.as_float(
                prepared.get("prepared_sandbox_estimated_saved_seconds")
            ),
            prepared_sandbox_hit=bool(prepared.get("prepared_sandbox_hit", False)),
            prepared_sandbox_miss=bool(prepared.get("prepared_sandbox_miss", False)),
            worker_ready_seconds=(
                phase_seconds.get(CasePhase.WORKER_BOOTSTRAP, 0.0) + phase_seconds.get(CasePhase.WORKER_READY, 0.0)
            ),
            main_agent_seconds=phase_seconds.get(CasePhase.MAIN_AGENT, 0.0)
            or phase_seconds.get(CasePhase.CONTROLLER_SUBMIT, 0.0),
            evaluator_seconds=artifact_metrics.evaluator_seconds or phase_seconds.get(CasePhase.EVALUATOR, 0.0),
            cleanup_seconds=(
                phase_seconds.get(CasePhase.CLEANUP, 0.0) + phase_seconds.get(CasePhase.SANDBOX_TERMINATE, 0.0)
            ),
            retry_count=max(snapshot.attempt - 1, 0),
            artifact_count=artifact_metrics.artifact_count,
            screenshot_count=artifact_metrics.screenshot_count,
            artifact_bytes=artifact_metrics.artifact_bytes,
            command_count=artifact_metrics.command_count,
            tool_call_count=artifact_metrics.tool_call_count,
            prompt_tokens=artifact_metrics.prompt_tokens,
            completion_tokens=artifact_metrics.completion_tokens,
            failure_category=(
                None if expected_infeasible_success else self._coalesce_failure_category(snapshot, timings)
            ),
            attempt_failure_category_breakdown=(
                MetricValue.as_int_map(snapshot.metrics.get("attempt_failure_category_breakdown"))
                if not expected_infeasible_success
                else {}
            ),
            error="" if expected_infeasible_success else snapshot.error,
            recovery_action=snapshot.recovery_action,
            retry_budget_remaining=snapshot.retry_budget_remaining,
            last_reconciled_status=snapshot.last_reconciled_status,
            cleanup_status=snapshot.cleanup_status,
            terminal_action=str(snapshot.metrics.get("terminal_action") or ""),
            terminal_reason=terminal_reason,
            terminal_retryable=bool(snapshot.metrics.get("terminal_retryable", False)),
            terminal_scope=str(snapshot.metrics.get("terminal_scope") or ""),
            terminal_limit=MetricValue.as_int(snapshot.metrics.get("terminal_limit")),
            terminal_current=MetricValue.as_int(snapshot.metrics.get("terminal_current")),
            evaluator_safety_risk_level=str(
                evaluator_metrics.get("evaluator_safety_risk_level")
                or artifact_evaluator_metrics.get("evaluator_safety_risk_level")
                or snapshot.metrics.get("evaluator_safety_risk_level")
                or ""
            ),
            evaluator_safety_finding_count=MetricValue.as_int(
                evaluator_metrics.get("evaluator_safety_finding_count")
                or snapshot.metrics.get("evaluator_safety_finding_count")
            ),
            evaluator_efficiency_rating=MetricValue.as_int(
                evaluator_metrics.get("evaluator_efficiency_rating")
                or artifact_evaluator_metrics.get("evaluator_efficiency_rating")
                or snapshot.metrics.get("evaluator_efficiency_rating")
            ),
            evaluator_diagnosis_category_breakdown=(
                MetricValue.as_int_map(evaluator_metrics.get("evaluator_diagnosis_category_breakdown"))
                or MetricValue.as_int_map(artifact_evaluator_metrics.get("evaluator_diagnosis_category_breakdown"))
                or MetricValue.as_int_map(snapshot.metrics.get("evaluator_diagnosis_category_breakdown"))
            ),
            evaluator_unnecessary_step_count=MetricValue.as_int(
                evaluator_metrics.get("evaluator_unnecessary_step_count")
                or snapshot.metrics.get("evaluator_unnecessary_step_count")
            ),
            evaluator_repeated_action_count=MetricValue.as_int(
                evaluator_metrics.get("evaluator_repeated_action_count")
                or snapshot.metrics.get("evaluator_repeated_action_count")
            ),
            evaluator_detour_count=MetricValue.as_int(
                evaluator_metrics.get("evaluator_detour_count") or snapshot.metrics.get("evaluator_detour_count")
            ),
            benchmark_hook_id=str(snapshot.metrics.get("benchmark_hook_id") or ""),
            benchmark_status=str(snapshot.metrics.get("benchmark_status") or ""),
            benchmark_score=MetricValue.as_float(snapshot.metrics.get("benchmark_score")),
            benchmark_passed=_bool_or_none(snapshot.metrics.get("benchmark_passed")),
            benchmark_error=str(snapshot.metrics.get("benchmark_error") or ""),
            reward_artifact_path=str(snapshot.metrics.get("reward_artifact_path") or ""),
            main_trace_dir=artifact.main_trace_dir if artifact is not None else "",
            main_steps_path=artifact.main_steps_path if artifact is not None else "",
            main_llm_trace_path=artifact.main_llm_trace_path if artifact is not None else "",
            evaluator_trace_dir=artifact.evaluator_trace_dir if artifact is not None else "",
            evaluator_report_path=artifact.evaluator_report_path if artifact is not None else "",
            evaluator_evidence_manifest_path=artifact.evaluator_evidence_manifest_path if artifact is not None else "",
            evaluator_llm_trace_path=artifact.evaluator_llm_trace_path if artifact is not None else "",
            sandbox_log_dir=artifact.sandbox_log_dir if artifact is not None else "",
        )

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str) and item]

    @staticmethod
    def _artifact_evaluator_metrics(artifact: CaseArtifactTelemetry | None) -> dict[str, object]:
        if artifact is None:
            return {}
        return {
            "evaluator_safety_risk_level": artifact.evaluator_safety_risk_level,
            "evaluator_efficiency_rating": artifact.evaluator_efficiency_rating,
            "evaluator_diagnosis_category_breakdown": artifact.evaluator_diagnosis_category_breakdown,
        }

    @staticmethod
    def _phase_seconds(timings: list[PhaseTiming]) -> dict[CasePhase, float]:
        result: dict[CasePhase, float] = {}
        for timing in timings:
            result[timing.phase] = result.get(timing.phase, 0.0) + timing.duration_seconds
        return result

    @classmethod
    def _case_total_seconds(cls, snapshot: CaseTelemetrySnapshot, timings: list[PhaseTiming]) -> float:
        if snapshot.total_seconds > 0:
            return snapshot.total_seconds
        snapshot_start = TimeValue.parse(snapshot.started_at)
        snapshot_finish = TimeValue.parse(snapshot.finished_at or snapshot.updated_at)
        if snapshot_start is not None and snapshot_finish is not None:
            return max((snapshot_finish - snapshot_start).total_seconds(), 0.0)
        timing_starts = [TimeValue.parse(timing.started_at) for timing in timings if timing.started_at]
        timing_finishes = [TimeValue.parse(timing.finished_at) for timing in timings if timing.finished_at]
        timing_starts = [item for item in timing_starts if item is not None]
        timing_finishes = [item for item in timing_finishes if item is not None]
        if timing_starts and timing_finishes:
            return max((max(timing_finishes) - min(timing_starts)).total_seconds(), 0.0)
        return sum(timing.duration_seconds for timing in timings)

    @staticmethod
    def _coalesce_failure_category(
        snapshot: CaseTelemetrySnapshot,
        timings: list[PhaseTiming],
    ) -> FailureCategory | None:
        if snapshot.status == CaseStatus.SUCCEEDED:
            return None
        timing_failure = next(
            (timing.failure_category for timing in reversed(timings) if timing.failure_category), None
        )
        if snapshot.failure_category is None or snapshot.failure_category == FailureCategory.UNKNOWN:
            return timing_failure or snapshot.failure_category
        return snapshot.failure_category
