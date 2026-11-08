"""Desktop evaluator structured artifact sink."""

from __future__ import annotations

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator import EvaluatorArtifactWriter

from .protocol import (
    EvaluationEvidenceManifest,
    EvaluationToolCheck,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
    RoundEvaluationReport,
    RoundEvaluationTraceRecord,
)

_SENSITIVE_KEY_FRAGMENTS = ("authorization", "api_key", "apikey", "secret", "token")


class DesktopEvaluationArtifactSink:
    """Persist desktop evaluator sidecars through the AgentKit layout.

    Args:
        writer: Shared writer resolving `evaluations/<kind>/round_XXXX`
            structured paths.
    """

    def __init__(self, writer: EvaluatorArtifactWriter) -> None:
        self.writer = writer

    def write_bundle(self, *, round_num: int, bundle: RoundEvaluationBundle) -> None:
        """Persist the canonical round bundle.

        Args:
            round_num: Evaluated round number.
            bundle: Typed bundle consumed by evaluator phases.
        """

        self.writer.write_bundle(round_num=round_num, payload=bundle)

    def write_evidence_manifest(
        self,
        *,
        round_num: int,
        manifest: EvaluationEvidenceManifest,
    ) -> None:
        """Persist the selected evidence manifest.

        Args:
            round_num: Evaluated round number.
            manifest: Evidence refs visible to the judge.
        """

        self.writer.write_evidence_manifest(round_num=round_num, payload=manifest)

    def append_phase_trace(
        self,
        *,
        round_num: int,
        trace: RoundEvaluationTraceRecord,
        duration_ms: float,
        tool_check_count: int,
    ) -> None:
        """Append one evaluator phase trace and summary runtime event.

        Args:
            round_num: Evaluated round number.
            trace: Provider/tool trace captured for the phase.
            duration_ms: Phase wall-clock duration in milliseconds.
            tool_check_count: Number of tool checks captured in the phase.
        """

        self.writer.append_llm_trace(round_num=round_num, payload=self._redact_trace(trace))
        self.append_runtime_event(
            round_num=round_num,
            event=f"{trace.phase}_completed",
            payload={
                "duration_ms": duration_ms,
                "cost": trace.cost,
                "tool_check_count": tool_check_count,
            },
        )

    def append_tool_checks(self, *, round_num: int, tool_checks: list[EvaluationToolCheck]) -> None:
        """Append deterministic evaluator tool checks.

        Args:
            round_num: Evaluated round number.
            tool_checks: Captured deterministic checks from evaluator phases.
        """

        for check in tool_checks:
            self.writer.append_tool_trace(round_num=round_num, payload=check.model_dump(mode="json"))

    def append_runtime_event(self, *, round_num: int, event: str, payload: JsonObject) -> None:
        """Append one structured evaluator runtime event.

        Args:
            round_num: Evaluated round number.
            event: Stable event name.
            payload: JSON-serializable event attributes.
        """

        self.writer.append_runtime_event(round_num=round_num, payload={"event": event, **payload})

    def write_final_report(self, record: RoundEvaluationRecord) -> None:
        """Persist the final report projection for one compatibility record.

        Args:
            record: Persisted desktop round evaluation record.
        """

        report = RoundEvaluationReport(
            overall_verdict=record.overall_verdict,
            process_rating=record.process_rating,
            outcome_label=record.outcome_label,
            completion_rating=record.completion_rating,
            confidence_rating=record.confidence_rating,
            controllability=record.controllability,
            reason=record.reason,
            criteria=record.criteria,
            diagnoses=record.diagnoses,
            side_effects=record.side_effects,
            safety=record.safety,
            efficiency=record.efficiency,
            first_divergence_step=record.first_divergence_step,
            first_divergence_reason=record.first_divergence_reason,
            first_divergence_evidence_refs=record.first_divergence_evidence_refs,
            needs_more_evidence=record.judge_metadata.needs_more_evidence,
        )
        self.writer.write_final_report(round_num=record.round_num, payload=report)

    @classmethod
    def _redact_trace(cls, trace: RoundEvaluationTraceRecord) -> RoundEvaluationTraceRecord:
        """Return a structured trace safe for duplicated sidecar logs.

        Args:
            trace: Captured evaluator LLM trace.

        Returns:
            Trace with embedded image data URLs replaced by placeholders.
        """

        return trace.model_copy(
            update={
                "request": cls._redact_json_object(trace.request),
                "response": cls._redact_json_object(trace.response),
            }
        )

    @classmethod
    def _redact_json_object(cls, payload: JsonObject) -> JsonObject:
        return {key: cls._redact_json_value(value, key=key) for key, value in payload.items()}

    @classmethod
    def _redact_json_value(cls, value: object, *, key: str = "") -> object:
        if key and any(fragment in key.lower() for fragment in _SENSITIVE_KEY_FRAGMENTS):
            return "[redacted sensitive value]"
        if isinstance(value, str) and value.startswith("data:image/"):
            return f"[redacted image data url; chars={len(value)}]"
        if isinstance(value, dict):
            return {str(item_key): cls._redact_json_value(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_json_value(item) for item in value]
        return value

    @classmethod
    def redact_payload(cls, payload: object) -> object:
        """Redact duplicated evaluator payloads before legacy persistence.

        Args:
            payload: JSON-like payload to redact.

        Returns:
            Redacted payload preserving the original shape.
        """

        return cls._redact_json_value(payload)


__all__ = ["DesktopEvaluationArtifactSink"]
