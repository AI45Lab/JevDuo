"""Legacy evaluator phase execution support for desktop evaluation."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.tool.tool import Tool
from nexau.core.messages import Message

from northcua.agentkernel.tracing.capture import TraceCaptureService, TraceCaptureState
from northcua.agentkit.agent_config import clone_agent_config_with_hooks
from northcua.app.logging import log_runtime_event
from northcua.core.enums import EvaluationMode, EvaluationToolCheckStatus
from northcua.transport.ws.message_seed import SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from .protocol import EvaluationToolCheck, RoundEvaluationTraceRecord

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PhaseRunResult:
    """Captured result from one evaluator agent phase.

    Attributes:
        response_text: Raw evaluator model text for the phase.
        trace: Persistable provider/tool trace for the phase call.
        tool_checks: Deterministic tool calls observed during the phase.
        duration_ms: Wall-clock duration of the phase in milliseconds.
    """

    response_text: str
    trace: RoundEvaluationTraceRecord
    tool_checks: list[EvaluationToolCheck]
    duration_ms: float


class DesktopEvaluatorPhaseRunner:
    """Run legacy JSON evaluator phases through the desktop transport."""

    def __init__(
        self,
        *,
        agent_config: AgentConfig,
        execution_transport: NorthCUAWebSocketTransport,
        user_id: str,
        trace_capture_service: TraceCaptureService,
    ) -> None:
        """Create a phase runner bound to one desktop evaluator service.

        Args:
            agent_config: Base evaluator agent configuration.
            execution_transport: Transport used to prepare and run agents.
            user_id: User id for evaluator agent sessions.
            trace_capture_service: Shared trace capture helper.
        """

        self.agent_config = agent_config
        self.execution_transport = execution_transport
        self.user_id = user_id
        self.trace_capture_service = trace_capture_service

    async def run_phase(
        self,
        *,
        phase_name: str,
        messages: list[Message],
        tools: list[Tool],
        evaluation_mode: EvaluationMode,
        session_id: str,
        round_num: int,
    ) -> PhaseRunResult:
        """Run one isolated evaluator phase through the execution transport.

        Args:
            phase_name: Stable phase name used in trace records and eval session ids.
            messages: Prompt messages for the evaluator agent.
            tools: Deterministic tools exposed to this phase.
            evaluation_mode: Effective evaluation mode for metadata/logging.
            session_id: Source CUA session id.
            round_num: Round number being evaluated.

        Returns:
            Captured phase response, trace, tool checks, and duration.

        Raises:
            RuntimeError: If the evaluator agent does not return text.
        """

        del evaluation_mode
        trace_state = TraceCaptureState()
        before_model_hooks, after_model_hooks, after_tool_hooks = self.trace_capture_service.build_hooks(trace_state)
        agent_config = self.prepare_agent_config(
            tools=tools,
            before_model_hooks=before_model_hooks,
            after_model_hooks=after_model_hooks,
            after_tool_hooks=after_tool_hooks,
        )
        eval_session_id = f"{session_id}::eval::r{round_num}::{phase_name}"
        prepared_agent = await self.execution_transport.prepare_agent(
            user_id=self.user_id,
            session_id=eval_session_id,
            agent_config=agent_config,
            remote=False,
            externalize_tools=False,
        )
        tracer = prepared_agent.global_storage.get("tracer") or agent_config.resolved_tracer
        tracer_baseline = self.trace_capture_service.snapshot_tracer(tracer)
        prepared_agent.history = messages
        started_at = time.perf_counter()
        captured_span_ids: set[str] | None = None
        try:
            result, _ = await self.execution_transport.run_agent_once(
                message=[],
                user_id=self.user_id,
                session_id=eval_session_id,
                agent=prepared_agent,
                agent_config=agent_config,
            )
            captured_span_ids = self.trace_capture_service.new_tracer_span_ids(tracer, tracer_baseline)
            self.trace_capture_service.enrich_from_tracer_span_ids(trace_state, tracer, captured_span_ids)
        finally:
            if captured_span_ids is None:
                captured_span_ids = self.trace_capture_service.new_tracer_span_ids_best_effort(tracer, tracer_baseline)
            cleared_span_count = self.trace_capture_service.clear_tracer_spans_best_effort(tracer, captured_span_ids)
            if cleared_span_count:
                log_runtime_event(
                    logger,
                    "round_evaluation_tracer_spans_pruned",
                    level=logging.DEBUG,
                    session_id=session_id,
                    round_num=round_num,
                    phase=phase_name,
                    cleared_span_count=cleared_span_count,
                )
        llm_trace = self.trace_capture_service.build_trace_record(trace_state)
        if not isinstance(result, str):
            raise RuntimeError(f"Unexpected evaluator phase result for {phase_name}: {type(result).__name__}")
        tool_checks = self.tool_checks_from_trace(trace_state)
        return PhaseRunResult(
            response_text=result,
            trace=RoundEvaluationTraceRecord(
                phase=phase_name,
                request=llm_trace.request,
                response=llm_trace.response,
                usage=llm_trace.usage,
                stop_reason=llm_trace.stop_reason,
                cost=llm_trace.cost,
                tool_checks=tool_checks,
            ),
            tool_checks=tool_checks,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )

    def prepare_agent_config(
        self,
        *,
        tools: list[Tool],
        before_model_hooks: list[object],
        after_model_hooks: list[object],
        after_tool_hooks: list[object],
    ) -> AgentConfig:
        """Clone the base agent config into an evaluator-safe configuration.

        Args:
            tools: Tools available to the evaluator phase.
            before_model_hooks: Trace hooks run before model calls.
            after_model_hooks: Trace hooks run after model calls.
            after_tool_hooks: Trace hooks run after tool calls.

        Returns:
            Evaluator-safe cloned agent config.
        """

        agent_config = clone_agent_config_with_hooks(
            self.agent_config,
            before_model_hooks=before_model_hooks,
            after_model_hooks=after_model_hooks,
            after_tool_hooks=after_tool_hooks,
        )
        agent_config.name = "NorthCUAEvaluator"
        agent_config.description = "Round evaluator"
        agent_config.tools = list(tools)
        agent_config.mcp_servers = []
        global_storage = dict(getattr(agent_config, "global_storage", {}) or {})
        global_storage[SUPPRESS_RAW_AGENT_MCP_SERVERS_KEY] = True
        agent_config.global_storage = global_storage
        agent_config.max_iterations = 4
        return agent_config

    @staticmethod
    def tool_checks_from_trace(state: TraceCaptureState) -> list[EvaluationToolCheck]:
        """Convert captured evaluator tool executions into protocol checks.

        Args:
            state: Captured evaluator trace state.

        Returns:
            Persistable evaluator tool checks.
        """

        checks: list[EvaluationToolCheck] = []
        for execution in state.tool_executions:
            summary = execution.tool_name
            if isinstance(execution.llm_tool_output, str) and execution.llm_tool_output.strip():
                summary = execution.llm_tool_output.strip()
            elif isinstance(execution.tool_output, dict):
                summary = json.dumps(execution.tool_output, ensure_ascii=False)
            checks.append(
                EvaluationToolCheck(
                    name=execution.tool_name,
                    status=(
                        EvaluationToolCheckStatus.FAILURE if execution.is_error else EvaluationToolCheckStatus.SUCCESS
                    ),
                    summary=summary,
                )
            )
        return checks


__all__ = ["DesktopEvaluatorPhaseRunner", "PhaseRunResult"]
