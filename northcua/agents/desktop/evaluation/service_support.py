"""Prompt/tool compatibility helpers for desktop round evaluation service."""

from __future__ import annotations

from typing import Protocol, cast

from nexau.archs.tool.tool import Tool
from nexau.core.messages import Message

from northcua.agentkit.evaluator.routing import (
    EvaluatorClientToolDispatcher,
    ExternalEvaluatorToolDispatcher,
    RemoteEvaluatorClientToolDispatcher,
)
from northcua.agentkit.evaluator.runtime import AgentKitEvaluatorRunner, EvaluatorRunContext
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import (
    EvaluationControllability,
    EvaluationMode,
    EvaluationOutcome,
    EvaluationToolMode,
    EvaluationVerdict,
)
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore

from .phase_runner import DesktopEvaluatorPhaseRunner, PhaseRunResult
from .prompting import DesktopEvaluatorPromptBuilder
from .protocol import (
    EvaluationCriterion,
    EvaluationEvidenceManifest,
    EvaluationJudgeMetadata,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
)


class _RoundEvaluatorSupportHost(Protocol):
    """Host surface required by ``RoundEvaluatorSupportMixin``."""

    @property
    def app_config(self) -> NorthCUAAppConfig:
        """Runtime application configuration.

        Returns:
            Application configuration that controls evaluator mode, tool policy,
            fail-open behavior, and artifact paths.
        """
        ...

    @property
    def artifact_store(self) -> NorthCUAArtifactStore:
        """Evaluation artifact store.

        Returns:
            Store that owns persisted round records, evaluator evidence, and
            report artifacts for the current desktop session.
        """
        ...

    @property
    def evaluator_runner(self) -> AgentKitEvaluatorRunner:
        """AgentKit evaluator runner.

        Returns:
            Shared AgentKit evaluator runtime used to build deterministic tools
            and run final-report tool calls.
        """
        ...

    @property
    def phase_runner(self) -> DesktopEvaluatorPhaseRunner:
        """Legacy phase runner.

        Returns:
            Phase runner used by compatibility paths that still execute
            extracted desktop evaluator phases.
        """
        ...

    @property
    def prompt_builder(self) -> DesktopEvaluatorPromptBuilder:
        """Evaluator prompt builder.

        Returns:
            Prompt builder that projects round bundles and evidence manifests
            into evaluator model messages.
        """
        ...


class RoundEvaluatorSupportMixin:
    """Compatibility helpers kept out of the evaluator service orchestrator."""

    def _build_agentkit_messages(
        self: _RoundEvaluatorSupportHost,
        *,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        criteria: list[EvaluationCriterion],
        projection_level: str = "standard",
    ) -> list[Message]:
        """Build the single-pass AgentKit evaluator prompt."""
        return self.prompt_builder.build_prompt(
            bundle=bundle,
            evidence_manifest=evidence_manifest,
            criteria=criteria,
            projection_level=projection_level,
        ).messages

    def _build_evaluator_tools(
        self: _RoundEvaluatorSupportHost,
        bundle: RoundEvaluationBundle,
        evidence_manifest: EvaluationEvidenceManifest,
        step_logs: list[StepLogRecord],
    ) -> list[Tool]:
        """Build deterministic evaluator tools when the configured mode allows."""
        if not self.app_config.evaluation.tools.enabled:
            return []
        if self.app_config.evaluation.tools.mode == EvaluationToolMode.NONE:
            return []
        if self.app_config.evaluation.tools.mode != EvaluationToolMode.DETERMINISTIC_ONLY:
            return []
        step_trace_by_session_step = {
            step.session_step: step.model_dump(mode="json") for step in step_logs if step.round_num == bundle.round_num
        }
        toolset = self.evaluator_runner.build_toolset_with_options(
            context=EvaluatorRunContext(
                bundle=bundle,
                evidence_manifest=evidence_manifest,
                artifact_root=self.artifact_store.root,
                step_trace_by_session_step=step_trace_by_session_step,
            ),
            include_finalizer=False,
            require_finalizer=False,
        )
        return list(toolset.tools)

    async def _run_phase(
        self: _RoundEvaluatorSupportHost,
        *,
        phase_name: str,
        messages: list[Message],
        tools: list[Tool],
        evaluation_mode: EvaluationMode,
        session_id: str,
        round_num: int,
    ) -> PhaseRunResult:
        """Run one legacy evaluator phase through the extracted phase runner."""
        return await self.phase_runner.run_phase(
            phase_name=phase_name,
            messages=messages,
            tools=tools,
            evaluation_mode=evaluation_mode,
            session_id=session_id,
            round_num=round_num,
        )

    @staticmethod
    def _coerce_client_tool_dispatcher(
        dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None,
    ) -> EvaluatorClientToolDispatcher | None:
        if dispatcher is None:
            return None
        if hasattr(dispatcher, "dispatch"):
            return cast(EvaluatorClientToolDispatcher, dispatcher)
        return RemoteEvaluatorClientToolDispatcher(cast(ExternalEvaluatorToolDispatcher, dispatcher))

    @staticmethod
    def _failed_record(*, session_id: str, round_num: int, round_request: str, reason: str) -> RoundEvaluationRecord:
        """Build a fail-open evaluation record."""
        return RoundEvaluationRecord(
            session_id=session_id,
            round_num=round_num,
            round_request=round_request,
            overall_verdict=EvaluationVerdict.UNCERTAIN,
            process_rating=1,
            outcome_label=EvaluationOutcome.UNCERTAIN,
            completion_rating=1,
            confidence_rating=1,
            controllability=EvaluationControllability.UNKNOWN,
            reason=reason,
            evidence_manifest=EvaluationEvidenceManifest(),
            judge_metadata=EvaluationJudgeMetadata(mode=EvaluationMode.ARTIFACT_AWARE, needs_more_evidence=True),
        )

    def _load_round_records(self: _RoundEvaluatorSupportHost) -> list[RoundEvaluationRecord]:
        """Load previously persisted round evaluation records."""
        path = self.artifact_store.round_evaluation_log_path
        if not path.exists():
            return []
        records: list[RoundEvaluationRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(RoundEvaluationRecord.model_validate_json(line))
        return records
