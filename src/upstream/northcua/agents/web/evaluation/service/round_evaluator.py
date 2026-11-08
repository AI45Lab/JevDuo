"""Web round evaluator service built on the shared AgentKit evaluator runner."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.artifacts import EvaluatorArtifactWriter
from northcua.agentkit.evaluator.protocol import (
    EvaluationEvidenceManifest,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
)
from northcua.agentkit.evaluator.routing import EvaluatorClientToolDispatcher
from northcua.agentkit.evaluator.runtime import AgentKitEvaluatorRunner, EvaluatorRunContext, EvaluatorRunResult

from ..artifacts import WebEvaluatorArtifactSink
from ..offline import load_web_offline_evaluation_bundle
from ..prompts import WebEvaluatorPromptBuilder
from .report_normalizer import normalize_web_final_report_page_state
from .spec import build_web_round_evaluator_spec

if TYPE_CHECKING:
    from northcua.agentkit.model_step import AgentKitModelStepExecutor


@dataclass(slots=True)
class WebRoundEvaluatorService:
    """Run a Web round evaluator from persisted artifacts."""

    model_step_executor: "AgentKitModelStepExecutor"
    prompt_builder: WebEvaluatorPromptBuilder = field(default_factory=WebEvaluatorPromptBuilder)

    async def evaluate_artifact_root(
        self,
        artifact_root: Path,
        *,
        round_num: int = 1,
        remote: bool = False,
        max_steps: int = 12,
        force_finalizer_after_steps: int = 8,
        enable_live_probe: bool = False,
        allow_client_mutating_tools: bool = False,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | None = None,
    ) -> EvaluatorRunResult:
        """Evaluate one persisted Web run using artifact-only or bound live probes."""

        if enable_live_probe and client_tool_dispatcher is None:
            raise ValueError("Web live probe evaluation requires a bound EvaluatorClientToolDispatcher.")
        spec = build_web_round_evaluator_spec(
            max_steps=max_steps,
            force_finalizer_after_steps=force_finalizer_after_steps,
            enable_live_probe=enable_live_probe,
            allow_client_mutating_tools=allow_client_mutating_tools,
        )
        started_at = time.perf_counter()
        offline_bundle = load_web_offline_evaluation_bundle(
            artifact_root,
            round_num=round_num,
            evidence_policy=spec.evidence_policy,
        )
        artifact_writer = WebEvaluatorArtifactSink(artifact_root=artifact_root).writer()
        self._append_bundle_built(
            writer=artifact_writer,
            bundle=offline_bundle.bundle,
        )
        self._append_evidence_selected(
            writer=artifact_writer,
            bundle=offline_bundle.bundle,
            evidence_manifest=offline_bundle.evidence_manifest,
        )
        runner = AgentKitEvaluatorRunner(
            spec=spec,
            artifact_writer=artifact_writer,
            model_step_executor=self.model_step_executor,
        )
        context = EvaluatorRunContext(
            bundle=offline_bundle.bundle,
            evidence_manifest=offline_bundle.evidence_manifest,
            artifact_root=offline_bundle.artifact_root,
            step_trace_by_session_step=offline_bundle.step_trace_by_session_step,
            remote=remote,
        )
        messages = self.prompt_builder.build_messages(
            bundle=offline_bundle.bundle,
            evidence_manifest=offline_bundle.evidence_manifest,
            artifact_root=offline_bundle.artifact_root,
        )
        run_result = await runner.run_round(
            context=context,
            messages=messages,
            trace_context={"phase": "agentkit_final_report", "evaluation_mode": "web_artifact"},
            externalize_tools=remote,
            client_tool_dispatcher=client_tool_dispatcher,
        )
        normalize_web_final_report_page_state(
            writer=artifact_writer,
            round_num=round_num,
            step_trace_by_session_step=offline_bundle.step_trace_by_session_step,
        )
        self._append_final_report_completed(
            writer=artifact_writer,
            bundle=offline_bundle.bundle,
            record=run_result.record,
            run_result=run_result,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
        return run_result

    @staticmethod
    def _append_bundle_built(*, writer: EvaluatorArtifactWriter, bundle: RoundEvaluationBundle) -> None:
        payload: JsonObject = {
            "event": "bundle_built",
            "session_id": bundle.session_id,
            "step_count": len(bundle.step_refs),
            "screenshot_ref_count": len(bundle.screenshot_refs),
            "dom_ref_count": len(bundle.dom_refs),
            "runtime_ref_count": len(bundle.runtime_event_refs),
        }
        writer.append_runtime_event(round_num=bundle.round_num, payload=payload)

    @staticmethod
    def _append_evidence_selected(
        *,
        writer: EvaluatorArtifactWriter,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
    ) -> None:
        payload: JsonObject = {
            "event": "evidence_selected",
            "selected_ref_count": len(evidence_manifest.refs),
            "image_ref_count": sum(1 for ref in evidence_manifest.refs if ref.kind.value == "image"),
            "dom_ref_count": sum(1 for ref in evidence_manifest.refs if ref.kind.value == "dom"),
            "pinned_ref_count": sum(1 for ref in evidence_manifest.refs if ref.pinned),
            "selected_refs": [ref.id for ref in evidence_manifest.refs],
            "pinned_refs": [ref.id for ref in evidence_manifest.refs if ref.pinned],
        }
        writer.append_runtime_event(round_num=bundle.round_num, payload=payload)

    @staticmethod
    def _append_final_report_completed(
        *,
        writer: EvaluatorArtifactWriter,
        bundle: RoundEvaluationBundle,
        record: RoundEvaluationRecord,
        run_result: EvaluatorRunResult,
        duration_ms: float,
    ) -> None:
        payload: JsonObject = {
            "event": "agentkit_final_report_completed",
            "duration_ms": duration_ms,
            "tool_count": len(run_result.toolset.tools),
            "tool_check_count": len(record.tool_checks),
            "verdict": record.overall_verdict.value,
            "completion_rating": record.completion_rating,
            "process_rating": record.process_rating,
            "confidence_rating": record.confidence_rating,
            "outcome_label": record.outcome_label.value,
            "controllability": record.controllability.value,
            "first_divergence_step": record.first_divergence_step,
            "safety_risk_level": record.safety.risk_level.value,
            "safety_finding_count": len(record.safety.findings),
            "efficiency_rating": record.efficiency.rating,
            "efficiency_unnecessary_step_count": record.efficiency.unnecessary_step_count,
            "efficiency_repeated_action_count": record.efficiency.repeated_action_count,
            "efficiency_detour_count": record.efficiency.detour_count,
            "efficiency_direct_path_available": record.efficiency.direct_path_available,
            "diagnosis_count": len(record.diagnoses),
            "needs_more_evidence": record.judge_metadata.needs_more_evidence,
            "terminal_decision": run_result.terminal_decision.to_json(),
            "session_id": bundle.session_id,
        }
        writer.append_runtime_event(round_num=bundle.round_num, payload=payload)
