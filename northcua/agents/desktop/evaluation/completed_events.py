"""Completed-event projection for desktop round evaluator results."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.runtime import EvaluatorRunResult

from .protocol import RoundEvaluationBundle, RoundEvaluationRecord

if TYPE_CHECKING:
    from .service import RoundEvaluatorService


def emit_agentkit_final_report_completed(
    *,
    service: "RoundEvaluatorService",
    round_num: int,
    bundle: RoundEvaluationBundle,
    record: RoundEvaluationRecord,
    run_result: EvaluatorRunResult,
    duration_ms: float,
) -> None:
    """Emit structured and UI-facing completion events for an evaluator round.

    Args:
        service: Desktop round evaluator service that owns artifact/event sinks.
        round_num: Evaluated round number.
        bundle: Evidence bundle used by the evaluator.
        record: Normalized final-report record.
        run_result: Runtime result containing terminal decision and toolset.
        duration_ms: Evaluator wall-clock runtime in milliseconds.
    """

    shared_payload: JsonObject = {
        "duration_ms": duration_ms,
        "tool_count": len(run_result.toolset.tools),
        "tool_check_count": len(record.tool_checks),
        "verdict": record.overall_verdict.value,
        "completion_rating": record.completion_rating,
        "safety_risk_level": record.safety.risk_level.value,
        "safety_finding_count": len(record.safety.findings),
        "efficiency_rating": record.efficiency.rating,
        "efficiency_unnecessary_step_count": record.efficiency.unnecessary_step_count,
        "efficiency_repeated_action_count": record.efficiency.repeated_action_count,
        "efficiency_detour_count": record.efficiency.detour_count,
        "efficiency_direct_path_available": record.efficiency.direct_path_available,
        "diagnosis_count": len(record.diagnoses),
        "diagnosis_category_breakdown": _diagnosis_category_breakdown(record),
        "needs_more_evidence": record.judge_metadata.needs_more_evidence,
        "terminal_decision": run_result.terminal_decision.to_json(),
    }
    service.structured_artifact_sink.append_runtime_event(
        round_num=round_num,
        event="agentkit_final_report_completed",
        payload=shared_payload,
    )
    service._emit_event(
        name="agentkit_final_report_completed",
        session_id=bundle.session_id,
        round_num=round_num,
        payload={
            **shared_payload,
            "process_rating": record.process_rating,
            "confidence_rating": record.confidence_rating,
            "outcome_label": record.outcome_label.value,
            "controllability": record.controllability.value,
            "first_divergence_step": record.first_divergence_step,
        },
    )


def _diagnosis_category_breakdown(record: RoundEvaluationRecord) -> JsonObject:
    """Return diagnosis category counts for terminal and telemetry events."""

    counts = Counter(diagnosis.category.value for diagnosis in record.diagnoses)
    return {category: count for category, count in sorted(counts.items())}
