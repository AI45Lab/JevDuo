"""AgentKit-backed desktop round evaluation execution."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, cast

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.runtime import EvaluatorRunContext, EvaluatorRunResult
from northcua.app.logging import log_runtime_event
from northcua.core.enums import RuntimeMode
from northcua.core.models import StepLogRecord

from .completed_events import emit_agentkit_final_report_completed
from .protocol import EvaluationCriterion, EvaluationEvidenceManifest, RoundEvaluationBundle, RoundEvaluationRecord
from .terminal_status import EVALUATION_STATUS_OK, evaluation_metadata

if TYPE_CHECKING:
    from northcua.agentkit.evaluator.routing import EvaluatorClientToolDispatcher, ExternalEvaluatorToolDispatcher

    from .service import EvaluationResultPayload, RoundEvaluatorService
logger = logging.getLogger(__name__)


class DesktopAgentKitRoundRunner:
    """Run one desktop round through the AgentKit final-report evaluator."""

    def __init__(self, service: "RoundEvaluatorService") -> None:
        """Create a round runner bound to the compatibility service facade."""
        self.service = service

    async def run(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        projection_level: str = "standard",
        client_tool_dispatcher: "EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None" = None,
    ) -> tuple[RoundEvaluationRecord, "EvaluationResultPayload"]:
        """Run one AgentKit final-report evaluator pass.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number being evaluated.
            step_logs: Step records available for this round.
            client_tool_dispatcher: Optional live client tool dispatcher.

        Returns:
            Normalized round record and compatibility result payload.
        """
        started_at = time.perf_counter()
        bundle = self._build_bundle(round_request=round_request, round_num=round_num, step_logs=step_logs)
        criteria, evidence_manifest = self._select_evidence(bundle)
        run_result, actual_projection_level = await self._run_agentkit(
            bundle=bundle,
            criteria=criteria,
            evidence_manifest=evidence_manifest,
            step_logs=step_logs,
            projection_level=projection_level,
            client_tool_dispatcher=client_tool_dispatcher,
        )
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        record = self._record_with_metadata(record=run_result.record, duration_ms=duration_ms)
        emit_agentkit_final_report_completed(
            service=self.service,
            round_num=round_num,
            bundle=bundle,
            record=record,
            run_result=run_result,
            duration_ms=duration_ms,
        )
        return (
            record,
            _compat_result_payload(
                service=self.service,
                bundle=bundle,
                criteria=criteria,
                evidence_manifest=evidence_manifest,
                record=record,
                evaluation_metadata=evaluation_metadata(
                    status=EVALUATION_STATUS_OK,
                    attempts=1,
                    projection_level=actual_projection_level,
                ),
            ),
        )

    def _build_bundle(
        self, *, round_request: str, round_num: int, step_logs: list[StepLogRecord]
    ) -> RoundEvaluationBundle:
        session_id = step_logs[0].session_id if step_logs else ""
        self.service._emit_event(
            name="round_started",
            session_id=session_id,
            round_num=round_num,
            payload={"step_count": len(step_logs), "round_request": round_request},
        )
        bundle = self.service.bundle_adapter.build_round_bundle(
            round_num=round_num, round_request=round_request, step_logs=step_logs
        )
        bundle_payload: JsonObject = {
            "step_count": len(bundle.step_refs),
            "screenshot_ref_count": len(bundle.screenshot_refs),
            "a11y_ref_count": len(bundle.a11y_refs),
            "runtime_ref_count": len(bundle.runtime_event_refs),
        }
        self.service.artifact_store.write_text(
            self.service.artifact_store.eval_round_bundle_path(round_num),
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        self.service.structured_artifact_sink.write_bundle(round_num=round_num, bundle=bundle)
        self.service.structured_artifact_sink.append_runtime_event(
            round_num=round_num, event="bundle_built", payload={"session_id": bundle.session_id, **bundle_payload}
        )
        self.service._emit_event(
            name="bundle_built", session_id=bundle.session_id, round_num=round_num, payload=bundle_payload
        )
        log_runtime_event(
            logger,
            "round_evaluation_bundle_built",
            round_num=round_num,
            session_id=bundle.session_id,
            step_count=len(bundle.step_refs),
            screenshot_ref_count=len(bundle.screenshot_refs),
            a11y_ref_count=len(bundle.a11y_refs),
            runtime_ref_count=len(bundle.runtime_event_refs),
        )
        return bundle

    def _select_evidence(
        self, bundle: RoundEvaluationBundle
    ) -> tuple[list[EvaluationCriterion], EvaluationEvidenceManifest]:
        criteria = [self.service.rubric_generator.fallback_criterion(bundle.round_request)]
        evidence_manifest = self.service.selector.select(bundle=bundle, criteria=criteria)
        evidence_payload: JsonObject = {
            "selected_ref_count": len(evidence_manifest.refs),
            "image_ref_count": sum((1 for ref in evidence_manifest.refs if ref.kind.value == "image")),
            "pinned_ref_count": sum((1 for ref in evidence_manifest.refs if ref.pinned)),
            "selected_refs": [ref.id for ref in evidence_manifest.refs],
            "pinned_refs": [ref.id for ref in evidence_manifest.refs if ref.pinned],
        }
        self.service.artifact_store.write_text(
            self.service.artifact_store.eval_round_selection_path(bundle.round_num),
            json.dumps(evidence_manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        self.service.structured_artifact_sink.write_evidence_manifest(
            round_num=bundle.round_num, manifest=evidence_manifest
        )
        self.service.structured_artifact_sink.append_runtime_event(
            round_num=bundle.round_num, event="evidence_selected", payload=evidence_payload
        )
        self.service._emit_event(
            name="evidence_selected", session_id=bundle.session_id, round_num=bundle.round_num, payload=evidence_payload
        )
        log_runtime_event(
            logger,
            "round_evaluation_evidence_selected",
            round_num=bundle.round_num,
            session_id=bundle.session_id,
            selected_ref_count=len(evidence_manifest.refs),
            image_ref_count=sum((1 for ref in evidence_manifest.refs if ref.kind.value == "image")),
        )
        return (criteria, evidence_manifest)

    async def _run_agentkit(
        self,
        *,
        bundle: RoundEvaluationBundle,
        criteria: list[EvaluationCriterion],
        evidence_manifest: EvaluationEvidenceManifest,
        step_logs: list[StepLogRecord],
        projection_level: str,
        client_tool_dispatcher: "EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None",
    ) -> tuple[EvaluatorRunResult, str]:
        step_trace_by_session_step = {
            step.session_step: cast(
                JsonObject, self.service.structured_artifact_sink.redact_payload(step.model_dump(mode="json"))
            )
            for step in step_logs
            if step.round_num == bundle.round_num
        }
        prompt = self.service.prompt_builder.build_prompt(
            bundle=bundle,
            evidence_manifest=evidence_manifest,
            criteria=criteria,
            projection_level=projection_level,
        )
        run_result = await self.service.evaluator_runner.run_round(
            context=EvaluatorRunContext(
                bundle=bundle,
                evidence_manifest=evidence_manifest,
                artifact_root=self.service.artifact_store.root,
                step_trace_by_session_step=step_trace_by_session_step,
                remote=self.service.app_config.websocket.runtime_mode == RuntimeMode.REMOTE,
            ),
            messages=prompt.messages,
            trace_context={
                "phase": "agentkit_final_report",
                "evaluation_mode": self.service.app_config.evaluation.mode.value,
                "evaluation_projection_level": prompt.projection_level,
                "evaluation_estimated_tokens": prompt.estimated_tokens,
                "evaluation_target_tokens": prompt.target_tokens,
                "evaluation_hard_tokens": prompt.hard_tokens,
            },
            client_tool_dispatcher=self.service._coerce_client_tool_dispatcher(client_tool_dispatcher),
        )
        return run_result, prompt.projection_level

    def _record_with_metadata(self, *, record: RoundEvaluationRecord, duration_ms: float) -> RoundEvaluationRecord:
        return record.model_copy(
            update={
                "judge_metadata": record.judge_metadata.model_copy(
                    update={
                        "mode": self.service.app_config.evaluation.mode,
                        "model": str(getattr(self.service.agent_config.llm_config, "model", "") or ""),
                        "duration_ms": duration_ms,
                        "needs_more_evidence": record.judge_metadata.needs_more_evidence,
                    }
                )
            }
        )


__all__ = ["DesktopAgentKitRoundRunner"]


def _compat_result_payload(
    *,
    service: "RoundEvaluatorService",
    bundle: RoundEvaluationBundle,
    criteria: list[EvaluationCriterion],
    evidence_manifest: EvaluationEvidenceManifest,
    record: RoundEvaluationRecord,
    evaluation_metadata: JsonObject,
) -> "EvaluationResultPayload":
    return {
        "bundle": service.structured_artifact_sink.redact_payload(bundle.model_dump(mode="json")),
        "rubric": service.structured_artifact_sink.redact_payload(
            {"criteria": [criterion.model_dump(mode="json") for criterion in criteria]}
        ),
        "selection": service.structured_artifact_sink.redact_payload(evidence_manifest.model_dump(mode="json")),
        "record": service.structured_artifact_sink.redact_payload(record.model_dump(mode="json")),
        "evaluation_status": evaluation_metadata.get("status", EVALUATION_STATUS_OK),
        "evaluation_error": {},
        "evaluation_attempts": evaluation_metadata.get("attempts", 1),
        "evaluation_projection_level": evaluation_metadata.get("projection_level", "standard"),
        "traces": [],
    }
