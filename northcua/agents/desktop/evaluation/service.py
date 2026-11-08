"""Round and session evaluator orchestration for the desktop agent.

The service coordinates deterministic evidence collection, evaluator model
phases, trace capture, JSONL persistence, and terminal rollups without changing
the round execution protocol that produced the original step records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agentkit.evaluator import EvaluatorArtifactLayout, EvaluatorArtifactWriter
from northcua.agentkit.evaluator.routing import (
    EvaluatorClientToolDispatcher,
    ExternalEvaluatorToolDispatcher,
)
from northcua.agentkit.evaluator.runtime import AgentKitEvaluatorRunner
from northcua.agentkit.model_step import AgentKitModelStepComponents, AgentKitModelStepExecutor
from northcua.app.logging import log_runtime_event, runtime_logging_context
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import (
    EvaluationSchedule,
)
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from .adapter import DesktopRoundEvidenceAdapter
from .artifacts import DesktopEvaluationArtifactSink
from .diagnosis import DiagnosisBuilder
from .events import DesktopEvaluationEventCallback
from .judge import JudgePipeline
from .phase_runner import DesktopEvaluatorPhaseRunner, PhaseRunResult
from .prompting import DesktopEvaluatorPromptBuilder, EvaluatorPromptBudgetExceeded
from .protocol import RoundEvaluationRecord
from .rollup import SessionEvaluationRollupService
from .round_runner import DesktopAgentKitRoundRunner
from .rubric import RubricGenerator
from .selector import EvidenceSelector
from .service_support import RoundEvaluatorSupportMixin
from .service_tasks import RoundEvaluatorTaskManagementMixin
from .spec import desktop_round_evaluator_spec
from .terminal_status import (
    EVALUATION_PROJECTION_STANDARD,
    EVALUATION_STATUS_OK,
    EVALUATION_STATUS_PENDING,
    agent_terminal_payload,
    attach_evaluation_metadata_to_step,
    evaluation_metadata,
)

logger = logging.getLogger(__name__)
type EvaluationResultPayload = JsonObject
_PhaseRunResult = PhaseRunResult


class RoundEvaluatorService(RoundEvaluatorTaskManagementMixin, RoundEvaluatorSupportMixin):
    """Own round-level asynchronous evaluation and session rollups.

    The orchestrator intentionally serializes record writes while still
    exposing async scheduling to callers. Phase order, JSON field names, and
    prompt/tool contracts are part of the persisted evaluator protocol.
    """

    def __init__(
        self,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        artifact_store: NorthCUAArtifactStore,
        execution_transport: NorthCUAWebSocketTransport,
        session_manager: SessionManager,
        user_id: str,
        transport_capabilities: AgentTransportCapabilities | None = None,
        tool_surface_service: ToolSurfaceService | None = None,
        event_callback: DesktopEvaluationEventCallback | None = None,
    ) -> None:
        """Initialize evaluator dependencies and phase helpers.

        Args:
            app_config: Runtime application configuration.
            agent_config: Base NexAU agent configuration cloned for evaluation.
            artifact_store: Store for evaluator inputs, traces, and results.
            execution_transport: Transport used to run evaluator agents.
            session_manager: Session manager shared with round execution.
            user_id: User id used when preparing evaluator sessions.
        """
        self.app_config = app_config
        self.agent_config = agent_config
        self.artifact_store = artifact_store
        self.execution_transport = execution_transport
        self.session_manager = session_manager
        self.user_id = user_id
        self.transport_capabilities = transport_capabilities or AgentTransportCapabilities()
        self.tool_surface_service = tool_surface_service or ToolSurfaceService()
        self.event_callback = event_callback
        self.evaluator_spec = desktop_round_evaluator_spec(app_config)
        structured_artifact_writer = EvaluatorArtifactWriter(
            EvaluatorArtifactLayout(session_root=artifact_store.root, evaluator_kind=self.evaluator_spec.kind)
        )
        self.structured_artifact_sink = DesktopEvaluationArtifactSink(structured_artifact_writer)
        self.model_step_executor = AgentKitModelStepExecutor(
            AgentKitModelStepComponents(
                app_config=app_config,
                agent_config=agent_config,
                execution_transport=execution_transport,
                user_id=user_id,
                transport_capabilities=self.transport_capabilities,
                tool_surface_service=self.tool_surface_service,
            )
        )
        self.evaluator_runner = AgentKitEvaluatorRunner(
            spec=self.evaluator_spec,
            artifact_writer=structured_artifact_writer,
            model_step_executor=self.model_step_executor,
        )
        self.bundle_adapter = DesktopRoundEvidenceAdapter(
            artifact_store=artifact_store, config=app_config.evaluation.bundle
        )
        self.bundle_builder = self.bundle_adapter.bundle_builder
        self.prompt_builder = DesktopEvaluatorPromptBuilder(
            artifact_root=artifact_store.root,
            prompt_budget=app_config.evaluation.prompt_budget,
        )
        template_root = Path(__file__).parent / "templates"
        self.rubric_generator = RubricGenerator(template_root)
        self.selector = EvidenceSelector(app_config.evaluation.selector, policy=self.evaluator_spec.evidence_policy)
        self.judge_pipeline = JudgePipeline(template_root, artifact_root=artifact_store.root)
        self.diagnosis_builder = DiagnosisBuilder(app_config.evaluation.judge)
        self.rollup_service = SessionEvaluationRollupService()
        if agent_config.llm_config is None:
            raise ValueError("RoundEvaluatorService requires an evaluator LLM config.")
        self.trace_capture_service = TraceCaptureService(agent_config.llm_config)
        self.phase_runner = DesktopEvaluatorPhaseRunner(
            agent_config=agent_config,
            execution_transport=execution_transport,
            user_id=user_id,
            trace_capture_service=self.trace_capture_service,
        )
        self.round_runner = DesktopAgentKitRoundRunner(self)
        self._pending_tasks: set[asyncio.Task[RoundEvaluationRecord | None]] = set()
        self._completed_failures: list[BaseException] = []
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Return whether round evaluation should currently run.

        Returns:
            ``True`` when evaluation is enabled and not explicitly disabled by
            schedule policy.
        """
        return self.app_config.evaluation.enabled and self.app_config.evaluation.schedule != EvaluationSchedule.DISABLED

    def effective_schedule(self) -> EvaluationSchedule:
        """Return the schedule after evaluator tool-placement safety coercion.

        Returns:
            Effective evaluation schedule.
        """
        return self.evaluator_spec.effective_schedule()

    async def evaluate_round_and_maybe_rollup(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        terminal: bool,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> RoundEvaluationRecord | None:
        """Evaluate a round and optionally write the terminal session rollup.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number to evaluate.
            step_logs: Persisted step logs available to the evaluator.
            terminal: Whether this is the final round in the session.

        Returns:
            The round record, or ``None`` when evaluation is disabled.
        """
        record = await self.evaluate_round(
            round_request=round_request,
            round_num=round_num,
            step_logs=step_logs,
            terminal=terminal,
            client_tool_dispatcher=client_tool_dispatcher,
        )
        if terminal:
            await self.rollup_session()
        return record

    async def evaluate_round(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        terminal: bool = False,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> RoundEvaluationRecord | None:
        """Evaluate one persisted round and append one JSONL record.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number to evaluate.
            step_logs: Persisted step logs available to the evaluator.

        Returns:
            Persisted round evaluation record, or ``None`` when disabled.

        Raises:
            Exception: Re-raises evaluator failures when fail-open is disabled.
        """
        if not self.enabled:
            return None
        async with self._lock:
            session_id = step_logs[0].session_id if step_logs else ""
            with runtime_logging_context(log_path=self.artifact_store.eval_runtime_log_path, session_id=session_id):
                started_at = time.perf_counter()
                try:
                    record, result_payload = await self._evaluate_round_with_timeout(
                        round_request=round_request,
                        round_num=round_num,
                        step_logs=step_logs,
                        terminal=terminal,
                        client_tool_dispatcher=client_tool_dispatcher,
                    )
                except Exception as exc:
                    log_runtime_event(
                        logger, "round_evaluation_failed", error=str(exc), round_num=round_num, session_id=session_id
                    )
                    if not self.app_config.evaluation.fail_open:
                        raise
                    record = self._failed_record(
                        session_id=session_id,
                        round_num=round_num,
                        round_request=round_request,
                        reason=f"Evaluator failed open: {exc}",
                    )
                    result_payload = {"record": record.model_dump(mode="json"), "error": str(exc)}
                self.artifact_store.append_jsonl(
                    self.artifact_store.round_evaluation_log_path, record.model_dump(mode="json")
                )
                self.structured_artifact_sink.write_final_report(record)
                self.artifact_store.write_text(
                    self.artifact_store.eval_round_result_path(round_num),
                    json.dumps(result_payload, ensure_ascii=False, indent=2),
                )
                log_runtime_event(
                    logger,
                    "round_evaluation_completed",
                    round_num=round_num,
                    session_id=session_id,
                    verdict=record.overall_verdict,
                    completion_rating=record.completion_rating,
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                    output_path=self.artifact_store.eval_round_result_path(round_num),
                )
                return record

    async def rollup_session(self) -> None:
        """Build and persist a session-level evaluation rollup."""
        records = self._load_round_records()
        rollup = self.rollup_service.build(records)
        with runtime_logging_context(log_path=self.artifact_store.eval_runtime_log_path, session_id=rollup.session_id):
            self.artifact_store.write_text(
                self.artifact_store.session_evaluation_rollup_path,
                json.dumps(rollup.model_dump(mode="json"), ensure_ascii=False, indent=2),
            )
            log_runtime_event(
                logger,
                "session_evaluation_rollup_written",
                session_id=rollup.session_id,
                round_count=rollup.round_count,
                output_path=self.artifact_store.session_evaluation_rollup_path,
            )

    async def _evaluate_round_with_timeout(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        terminal: bool,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> tuple[RoundEvaluationRecord, EvaluationResultPayload]:
        """Run one evaluation with the configured round timeout.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number to evaluate.
            step_logs: Persisted step logs available to the evaluator.

        Returns:
            Persisted record and compatibility payload.

        Raises:
            TimeoutError: If the evaluator does not complete within the
                configured timeout.
        """
        timeout_seconds = self.app_config.evaluation.round_timeout_seconds
        projection_levels = [EVALUATION_PROJECTION_STANDARD]
        if self.app_config.evaluation.evaluator_only_retry_attempts > 0:
            projection_levels.append("tight")
        errors: list[BaseException] = []
        attempts_completed = 0
        last_projection_level = projection_levels[0]
        for attempt_index, projection_level in enumerate(projection_levels, start=1):
            attempts_completed = attempt_index
            last_projection_level = projection_level
            try:
                record, payload = await asyncio.wait_for(
                    self._evaluate_round_impl(
                        round_request=round_request,
                        round_num=round_num,
                        step_logs=step_logs,
                        projection_level=projection_level,
                        client_tool_dispatcher=client_tool_dispatcher,
                    ),
                    timeout=timeout_seconds,
                )
                payload.update(
                    {
                        "evaluation_status": EVALUATION_STATUS_OK,
                        "evaluation_attempts": attempt_index,
                        "evaluation_projection_level": str(
                            payload.get("evaluation_projection_level") or projection_level
                        ),
                        "agent_terminal": agent_terminal_payload(step_logs[-1] if step_logs else None),
                    }
                )
                if step_logs:
                    attach_evaluation_metadata_to_step(
                        step_logs[-1],
                        evaluation_metadata(
                            status=EVALUATION_STATUS_OK,
                            attempts=attempt_index,
                            projection_level=str(payload.get("evaluation_projection_level") or projection_level),
                        ),
                    )
                return record, payload
            except TimeoutError as exc:
                errors.append(TimeoutError(f"Evaluator round {round_num} timed out after {timeout_seconds:.2f}s"))
                log_runtime_event(
                    logger,
                    "round_evaluation_attempt_failed",
                    round_num=round_num,
                    projection_level=projection_level,
                    attempt=attempt_index,
                    error_type=type(errors[-1]).__name__,
                    error=str(errors[-1]),
                )
                # A tighter projection cannot recover an evaluator that already
                # consumed the full wall-clock budget. Preserve the Agent
                # terminal immediately instead of waiting for a second timeout.
                break
            except Exception as exc:
                if not _is_evaluator_infra_error(exc):
                    raise
                errors.append(exc)
                log_runtime_event(
                    logger,
                    "round_evaluation_attempt_failed",
                    round_num=round_num,
                    projection_level=projection_level,
                    attempt=attempt_index,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if not _is_evaluator_projection_retryable(exc):
                    break
        final_error = errors[-1] if errors else RuntimeError("Evaluator did not run")
        if terminal and self.app_config.evaluation.protect_agent_terminal:
            return self._pending_evaluation_record(
                round_request=round_request,
                round_num=round_num,
                step_logs=step_logs,
                error=final_error,
                attempts=attempts_completed,
                projection_level=last_projection_level,
            )
        raise final_error

    async def _evaluate_round_impl(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        projection_level: str = EVALUATION_PROJECTION_STANDARD,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> tuple[RoundEvaluationRecord, EvaluationResultPayload]:
        """Run one AgentKit final-report evaluator pass.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number to evaluate.
            step_logs: Persisted step logs available to the evaluator.

        Returns:
            The normalized round record and a JSON-serializable result payload.
        """
        return await self.round_runner.run(
            round_request=round_request,
            round_num=round_num,
            step_logs=step_logs,
            projection_level=projection_level,
            client_tool_dispatcher=client_tool_dispatcher,
        )

    def _pending_evaluation_record(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        error: BaseException,
        attempts: int,
        projection_level: str,
    ) -> tuple[RoundEvaluationRecord, EvaluationResultPayload]:
        session_id = step_logs[0].session_id if step_logs else ""
        reason = f"Evaluator pending after Agent terminal: {type(error).__name__}: {error}"
        record = self._failed_record(
            session_id=session_id,
            round_num=round_num,
            round_request=round_request,
            reason=reason,
        )
        metadata = evaluation_metadata(
            status=EVALUATION_STATUS_PENDING,
            attempts=attempts,
            projection_level=projection_level,
            error=str(error),
            error_type=type(error).__name__,
        )
        if step_logs:
            attach_evaluation_metadata_to_step(step_logs[-1], metadata)
        payload: EvaluationResultPayload = {
            "record": record.model_dump(mode="json"),
            "agent_terminal": agent_terminal_payload(step_logs[-1] if step_logs else None),
            "evaluation_status": EVALUATION_STATUS_PENDING,
            "evaluation_error": metadata,
            "evaluation_attempts": attempts,
            "evaluation_projection_level": projection_level,
        }
        log_runtime_event(
            logger,
            "round_evaluation_pending_after_agent_terminal",
            round_num=round_num,
            session_id=session_id,
            attempts=attempts,
            projection_level=projection_level,
            error_type=type(error).__name__,
            error=str(error),
        )
        return record, payload


def _is_evaluator_infra_error(exc: BaseException) -> bool:
    """Return whether an evaluator failure should not invalidate Agent terminal."""

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, EvaluatorPromptBudgetExceeded)):
        return True
    message = str(exc).lower()
    markers = (
        "context length",
        "context_length",
        "context deadline",
        "context budget",
        "maximum context",
        "max context",
        "prompt projection",
        "exceeds hard budget",
        "timed out",
        "timeout",
        "deadline exceeded",
        "without calling final_report",
        "final_report tool rejected",
        "final_report_tool_error",
        "missing_final_report",
        "unknownevaluatortool",
        "clienttoolunavailable",
        "client tool",
        "client_bash_readonly",
        "client_screenshot",
        "gui_inspect",
        "remote result",
        "websocket",
        "transport",
    )
    return any(marker in message for marker in markers)


def _is_evaluator_projection_retryable(exc: BaseException) -> bool:
    """Return whether a tighter prompt projection can recover the failure."""

    if isinstance(exc, EvaluatorPromptBudgetExceeded):
        return True
    message = str(exc).lower()
    markers = (
        "context length",
        "context_length",
        "context budget",
        "maximum context",
        "max context",
        "prompt projection",
        "exceeds hard budget",
    )
    return any(marker in message for marker in markers)
