"""Desktop model-step adapter for the shared runtime."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.context.adapters import (
    ProviderContextBudgetGuard,
    install_provider_context_budget_guard,
    is_context_overflow_error,
)
from northcua.agentinfra.context.bootstrap import ContextSideEffectFence, prepare_context_messages
from northcua.agentinfra.providers.llm_fallback import (
    LLMFallbackCandidate,
    agent_config_for_llm_candidate,
    conservative_context_budget_inputs,
)
from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentinfra.runtime.artifacts import sanitize_session_id
from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.context.strategies import ContextBudgetExceededError, ContextSummaryError
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkernel.tracing.capture import TraceCaptureService, TraceCaptureState
from northcua.agents.desktop.prompt.envelope import HistoryTurn
from northcua.agents.desktop.round_execution.agent_prep import RoundAgentPreparer
from northcua.agents.desktop.round_execution.tool_dispatch import (
    CommandExecutorProtocol,
    LocalExternalToolDispatcher,
)
from northcua.agents.desktop.services.tool_results import ToolResultService
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.app.logging import log_runtime_event
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import RuntimeMode
from northcua.core.json_types import JsonObject
from northcua.transport.ws.message_seed import supports_stepwise_local_execution
from northcua.transport.ws.remote_round import ExternalToolDispatcher
from northcua.transport.ws.transport import NorthCUAWebSocketTransport
from northcua_runtime_shared.execution import RuntimeExecutionError

from .contracts import (
    DesktopModelStepResult,
    DesktopRuntimeEventName,
    DesktopStepObservation,
)
from .model_execution import DesktopPreparedAgentExecutor, provider_tool_name
from .prompt import DesktopPromptProjector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ContextPlanCache:
    messages: list[Message] | None = None
    source_messages: list[Message] | None = None
    hard_input_budget: int | None = None
    shadow_observed: bool = False


@dataclass(slots=True)
class DesktopModelStepRunner:
    """Run one desktop model step, including LLM fallback and tool pauses.

    Attributes:
        app_config: Desktop app configuration.
        user_id: NexAU user id used for session-scoped model calls.
        transport_capabilities: Runtime feature flags for local/remote transport.
        agent_preparer: Adapter that builds per-candidate NexAU agent configs.
        trace_capture_service: Trace builder for the selected fallback candidate.
        tool_surface_service: Service that exposes provider tool specs.
        tool_result_service: Service that converts executions into artifacts.
        local_tool_dispatcher: Dispatcher used for external tool pauses.
        round_coordinator: Desktop state facade used for resume results.
        execution_transport: Local/remote NexAU execution transport.
        local_device_executor: Local command executor for desktop tool calls.
    """

    app_config: NorthCUAAppConfig
    user_id: str
    transport_capabilities: AgentTransportCapabilities
    agent_preparer: RoundAgentPreparer
    trace_capture_service: TraceCaptureService
    tool_surface_service: ToolSurfaceService
    tool_result_service: ToolResultService
    local_tool_dispatcher: LocalExternalToolDispatcher
    round_coordinator: NorthCUARoundCoordinator
    execution_transport: NorthCUAWebSocketTransport
    local_device_executor: CommandExecutorProtocol

    async def run(
        self,
        *,
        task: str,
        session_id: str,
        round_num: int,
        round_step: int,
        session_step: int,
        step_budget: JsonObject | None,
        tool_choice_policy: str = "auto",
        allowed_tool_names: tuple[str, ...] = (),
        observation: DesktopStepObservation,
        history_turns: list[HistoryTurn],
        prompt_projector: DesktopPromptProjector,
        trace_context_metadata: JsonObject,
        remote_tool_dispatcher: ExternalToolDispatcher | None,
    ) -> DesktopModelStepResult:
        """Run one model transaction and return the selected trace state.

        Args:
            task: User task text.
            session_id: Active desktop session id.
            round_num: One-based desktop round number.
            round_step: One-based step within the round.
            session_step: One-based step within the session.
            step_budget: Prompt-safe budget metadata for the current step.
            tool_choice_policy: Neutral per-task model tool-selection policy.
            observation: Typed observation for prompt projection.
            history_turns: Replay turns from prior steps.
            prompt_projector: Prompt adapter used after tool surface resolution.
            trace_context_metadata: JSON metadata passed into model/tool traces.
            remote_tool_dispatcher: Optional remote command dispatcher callback.

        Returns:
            Selected model trace state and persisted LLM trace record.

        Raises:
            Exception: Re-raises the final LLM fallback candidate failure.
            RuntimeError: If no fallback candidate is available.
        """

        is_remote = self.app_config.websocket.runtime_mode == RuntimeMode.REMOTE
        candidates = self.agent_preparer.llm_fallback_candidates()
        context_config = getattr(self.app_config, "context_management", None)
        context_mode = (
            context_config.resolve("desktop").mode if context_config is not None else ContextManagementMode.OFF
        )
        conservative_context_limit: int | None = None
        conservative_output_reserve: int | None = None
        if context_mode != ContextManagementMode.OFF:
            base_agent_config = getattr(self.agent_preparer, "base_agent_config", None)
            if not isinstance(base_agent_config, AgentConfig):
                raise ValueError("desktop context enforcement requires the base AgentConfig")
            conservative_context_limit, conservative_output_reserve = conservative_context_budget_inputs(
                base_agent_config,
                candidates,
            )
        context_plan_cache = _ContextPlanCache()
        last_model_error: Exception | None = None
        for attempt_index, candidate in enumerate(candidates):
            side_effect_fence = ContextSideEffectFence(mode=context_mode)
            try:
                return await self._run_candidate(
                    candidate=candidate,
                    task=task,
                    session_id=session_id,
                    round_num=round_num,
                    round_step=round_step,
                    session_step=session_step,
                    step_budget=step_budget,
                    tool_choice_policy=tool_choice_policy,
                    allowed_tool_names=allowed_tool_names,
                    is_remote=is_remote,
                    observation=observation,
                    history_turns=history_turns,
                    prompt_projector=prompt_projector,
                    trace_context_metadata=trace_context_metadata,
                    remote_tool_dispatcher=remote_tool_dispatcher,
                    context_plan_cache=context_plan_cache,
                    conservative_context_limit=conservative_context_limit,
                    conservative_output_reserve=conservative_output_reserve,
                    context_mode=context_mode,
                    side_effect_fence=side_effect_fence,
                )
            except Exception as exc:
                if isinstance(exc, (ContextBudgetExceededError, ContextSummaryError, RuntimeExecutionError)):
                    raise
                side_effect_fence.raise_if_candidate_replay_unsafe(exc)
                last_model_error = exc
                is_final = attempt_index >= len(candidates) - 1
                log_runtime_event(
                    logger,
                    DesktopRuntimeEventName.LLM_FALLBACK_ATTEMPT_FAILED.value,
                    level=logging.WARNING,
                    session_id=session_id,
                    session_step=session_step,
                    candidate_index=candidate.index,
                    candidate_name=candidate.name,
                    is_primary=candidate.is_primary,
                    is_final=is_final,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if is_final:
                    raise
                continue
        raise RuntimeError("No LLM candidate was available for round execution") from last_model_error

    async def _run_candidate(
        self,
        *,
        candidate: LLMFallbackCandidate,
        task: str,
        session_id: str,
        round_num: int,
        round_step: int,
        session_step: int,
        step_budget: JsonObject | None,
        tool_choice_policy: str,
        allowed_tool_names: tuple[str, ...],
        is_remote: bool,
        observation: DesktopStepObservation,
        history_turns: list[HistoryTurn],
        prompt_projector: DesktopPromptProjector,
        trace_context_metadata: JsonObject,
        remote_tool_dispatcher: ExternalToolDispatcher | None,
        context_plan_cache: _ContextPlanCache,
        conservative_context_limit: int | None,
        conservative_output_reserve: int | None,
        context_mode: ContextManagementMode,
        side_effect_fence: ContextSideEffectFence,
    ) -> DesktopModelStepResult:
        """Run one LLM fallback candidate from prompt projection through trace capture."""

        trace_state = TraceCaptureState()
        attempt_trace_service = TraceCaptureService(candidate.llm_config)
        before_model_hooks, after_model_hooks, after_tool_hooks = attempt_trace_service.build_hooks(trace_state)
        provider_guard = (
            ProviderContextBudgetGuard(model=str(candidate.llm_config.model or "gpt-4o"))
            if context_mode == ContextManagementMode.ENFORCE
            else None
        )
        agent_config = self.agent_preparer.prepare_agent_config(
            before_model_hooks=before_model_hooks,
            after_model_hooks=after_model_hooks,
            after_tool_hooks=after_tool_hooks,
            before_tool_hooks=[side_effect_fence.before_tool] if context_mode == ContextManagementMode.ENFORCE else [],
        )
        agent_config = agent_config_for_llm_candidate(agent_config, candidate)
        agent_config.max_iterations = self.app_config.execution.max_agent_iterations_per_round
        self._log_candidate_started(candidate, session_id=session_id, session_step=session_step)
        prepared_agent = await self.execution_transport.prepare_agent(
            user_id=self.user_id,
            session_id=session_id,
            agent_config=agent_config,
            remote=is_remote,
            externalize_tools=False,
            transport_capabilities=self.transport_capabilities,
        )
        tool_surface = self.tool_surface_service.activate_for_agent(
            prepared_agent,
            tool_choice_policy=tool_choice_policy,
            allowed_tool_names=allowed_tool_names,
        )
        trace_state.provider_tools = list(tool_surface.tools)
        trace_state.provider_tool_choice = tool_surface.tool_choice
        log_runtime_event(
            logger,
            DesktopRuntimeEventName.TOOL_SURFACE_READY.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            remote=is_remote,
            tool_count=len(tool_surface.tools),
            tool_names=[provider_tool_name(tool) for tool in tool_surface.tools],
            tool_choice=tool_surface.tool_choice,
            llm_candidate=candidate.name,
        )
        prompt_bundle = prompt_projector.project(
            task=task,
            session_id=session_id,
            session_step=session_step,
            step_budget=step_budget,
            observation=observation,
            history_turns=history_turns,
            tools=list(tool_surface.tools),
            tool_choice=tool_surface.tool_choice,
            trace_context_metadata=trace_context_metadata,
            llm_candidate_name=candidate.name,
        )
        tracer = prepared_agent.global_storage.get("tracer") or agent_config.resolved_tracer
        tracer_baseline = attempt_trace_service.snapshot_tracer(tracer)
        log_runtime_event(
            logger,
            DesktopRuntimeEventName.TRACER_SNAPSHOT.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            baseline_span_count=len(tracer_baseline),
            llm_candidate=candidate.name,
        )
        captured_span_ids: set[str] | None = None
        try:
            primary_llm_config = self.agent_preparer.llm_fallback_candidates()[0].llm_config
            task_digest = hashlib.sha256(task.strip().encode("utf-8")).hexdigest()[:16]
            logical_agent_path = _desktop_logical_agent_path(trace_context_metadata)
            if context_mode == ContextManagementMode.ENFORCE and context_plan_cache.messages is not None:
                effective_messages = list(context_plan_cache.messages)
            elif context_mode == ContextManagementMode.SHADOW and context_plan_cache.shadow_observed:
                effective_messages = list(prompt_bundle.messages)
            else:
                prepared_context = await prepare_context_messages(
                    app_config=self.app_config,
                    agent_kind="desktop",
                    user_id=self.user_id,
                    session_id=session_id,
                    task_epoch=f"task-{task_digest}",
                    logical_agent_path=logical_agent_path,
                    logical_step_id=f"{session_id}:desktop:{round_num}:{session_step}",
                    round_num=round_num,
                    messages=prompt_bundle.messages,
                    shadow_messages=prompt_bundle.shadow_messages,
                    tools=list(tool_surface.tools),
                    agent_config=agent_config,
                    prepared_agent=prepared_agent,
                    primary_llm_config=primary_llm_config,
                    metadata={
                        "agent_kind": "desktop",
                        "round_num": round_num,
                        "round_step": round_step,
                        "session_step": session_step,
                        "candidate_name": candidate.name,
                    },
                    context_limit_override=conservative_context_limit,
                    output_reserve_override=conservative_output_reserve,
                )
                effective_messages = prepared_context.messages
                if context_mode == ContextManagementMode.SHADOW:
                    context_plan_cache.shadow_observed = True
                if context_mode == ContextManagementMode.ENFORCE:
                    context_plan_cache.messages = list(effective_messages)
                    context_plan_cache.source_messages = list(prompt_bundle.messages)
                    if prepared_context.result is None or prepared_context.result.budget is None:
                        raise RuntimeError("enforce context preparation returned no resolved budget")
                    context_plan_cache.hard_input_budget = prepared_context.result.budget.hard_input_budget
            if context_mode == ContextManagementMode.ENFORCE:
                if provider_guard is None:
                    raise RuntimeError("enforce context provider guard was not initialized")
                if context_plan_cache.hard_input_budget is None:
                    raise RuntimeError("enforce context preparation returned no provider guard budget")
                provider_guard.configure(
                    hard_input_budget=context_plan_cache.hard_input_budget,
                    tools=list(tool_surface.tools),
                )
                install_provider_context_budget_guard(prepared_agent.executor.llm_caller, provider_guard)
            try:
                await self._execute_prepared_context(
                    messages=effective_messages,
                    agent_config=agent_config,
                    prepared_agent=prepared_agent,
                    is_remote=is_remote,
                    remote_tool_dispatcher=remote_tool_dispatcher,
                    trace_state=trace_state,
                    session_id=session_id,
                    round_num=round_num,
                    session_step=session_step,
                    round_step=round_step,
                    trace_context_metadata=trace_context_metadata,
                    candidate=candidate,
                    side_effect_fence=side_effect_fence,
                )
            except Exception as exc:
                can_retry_without_replaying_tools = is_remote or supports_stepwise_local_execution(prepared_agent)
                if (
                    context_mode == ContextManagementMode.ENFORCE
                    and not can_retry_without_replaying_tools
                    and is_context_overflow_error(exc)
                ):
                    raise ContextBudgetExceededError(
                        "provider overflow in exact/full-loop execution cannot be replayed safely"
                    ) from exc
                if (
                    context_mode != ContextManagementMode.ENFORCE
                    or not can_retry_without_replaying_tools
                    or not is_context_overflow_error(exc)
                ):
                    raise
                recovered_context = await prepare_context_messages(
                    app_config=self.app_config,
                    agent_kind="desktop",
                    user_id=self.user_id,
                    session_id=session_id,
                    task_epoch=f"task-{task_digest}",
                    logical_agent_path=logical_agent_path,
                    logical_step_id=f"{session_id}:desktop:{round_num}:{session_step}:overflow-recovery",
                    round_num=round_num,
                    messages=context_plan_cache.source_messages or prompt_bundle.messages,
                    tools=list(tool_surface.tools),
                    agent_config=agent_config,
                    prepared_agent=prepared_agent,
                    primary_llm_config=primary_llm_config,
                    metadata={
                        "agent_kind": "desktop",
                        "round_num": round_num,
                        "round_step": round_step,
                        "session_step": session_step,
                        "candidate_name": candidate.name,
                        "provider_overflow_recovery": True,
                    },
                    recover_provider_overflow=True,
                    context_limit_override=conservative_context_limit,
                    output_reserve_override=conservative_output_reserve,
                )
                context_plan_cache.messages = list(recovered_context.messages)
                if recovered_context.result is None or recovered_context.result.budget is None:
                    raise RuntimeError("overflow recovery returned no resolved budget")
                context_plan_cache.hard_input_budget = recovered_context.result.budget.hard_input_budget
                provider_guard.configure(
                    hard_input_budget=context_plan_cache.hard_input_budget,
                    tools=list(tool_surface.tools),
                )
                await self._execute_prepared_context(
                    messages=recovered_context.messages,
                    agent_config=agent_config,
                    prepared_agent=prepared_agent,
                    is_remote=is_remote,
                    remote_tool_dispatcher=remote_tool_dispatcher,
                    trace_state=trace_state,
                    session_id=session_id,
                    round_num=round_num,
                    session_step=session_step,
                    round_step=round_step,
                    trace_context_metadata=trace_context_metadata,
                    candidate=candidate,
                    side_effect_fence=side_effect_fence,
                )
            captured_span_ids = attempt_trace_service.new_tracer_span_ids(tracer, tracer_baseline)
            attempt_trace_service.enrich_from_tracer_span_ids(trace_state, tracer, captured_span_ids)
            self._log_candidate_selected(candidate, session_id=session_id, session_step=session_step)
            llm_trace = self.trace_capture_service.build_trace_record(trace_state)
            return DesktopModelStepResult(
                trace_state=trace_state,
                llm_trace=llm_trace,
                llm_candidate_name=str(candidate.name),
            )
        finally:
            span_ids_to_clear = captured_span_ids
            if span_ids_to_clear is None:
                span_ids_to_clear = attempt_trace_service.new_tracer_span_ids_best_effort(tracer, tracer_baseline)
            cleared_span_count = attempt_trace_service.clear_tracer_spans_best_effort(tracer, span_ids_to_clear)
            if cleared_span_count:
                log_runtime_event(
                    logger,
                    "round_tracer_spans_pruned",
                    level=logging.DEBUG,
                    session_id=session_id,
                    session_step=session_step,
                    cleared_span_count=cleared_span_count,
                    llm_candidate=candidate.name,
                )

    def _log_candidate_started(self, candidate: LLMFallbackCandidate, *, session_id: str, session_step: int) -> None:
        """Log fallback candidate selection before model execution."""

        log_runtime_event(
            logger,
            DesktopRuntimeEventName.LLM_FALLBACK_ATTEMPT_STARTED.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            candidate_index=candidate.index,
            candidate_name=candidate.name,
            is_primary=candidate.is_primary,
            model=getattr(candidate.llm_config, "model", None),
            api_type=getattr(candidate.llm_config, "api_type", None),
        )

    def _log_candidate_selected(self, candidate: LLMFallbackCandidate, *, session_id: str, session_step: int) -> None:
        """Log the fallback candidate that produced the accepted trace."""

        log_runtime_event(
            logger,
            DesktopRuntimeEventName.LLM_FALLBACK_ATTEMPT_SELECTED.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            candidate_index=candidate.index,
            candidate_name=candidate.name,
            is_primary=candidate.is_primary,
        )

    def _prepared_agent_executor(self) -> DesktopPreparedAgentExecutor:
        """Build the execution adapter for the selected LLM fallback attempt."""

        return DesktopPreparedAgentExecutor(
            user_id=self.user_id,
            transport_capabilities=self.transport_capabilities,
            execution_transport=self.execution_transport,
            local_tool_dispatcher=self.local_tool_dispatcher,
            round_coordinator=self.round_coordinator,
            local_device_executor=self.local_device_executor,
        )

    async def _execute_prepared_context(
        self,
        *,
        messages: list[Message],
        agent_config: AgentConfig,
        prepared_agent: Agent,
        is_remote: bool,
        remote_tool_dispatcher: ExternalToolDispatcher | None,
        trace_state: TraceCaptureState,
        session_id: str,
        round_num: int,
        session_step: int,
        round_step: int,
        trace_context_metadata: JsonObject,
        candidate: LLMFallbackCandidate,
        side_effect_fence: ContextSideEffectFence,
    ) -> None:
        from northcua.agentinfra.providers.no_tool_retry import scoped_no_tool_retry
        from northcua.agentinfra.providers.response_recovery import response_recovery_scope

        with (
            response_recovery_scope(
                prepared_agent, enabled=getattr(self.app_config.execution, "model_response_recovery", False)
            ),
            scoped_no_tool_retry(
                prepared_agent,
                attempts=self.app_config.execution.no_tool_call_attempts,
                audit_dir=Path(self.app_config.artifacts.root_dir)
                / sanitize_session_id(session_id)
                / "no_tool_attempts",
                logical_step=session_step,
            ),
        ):
            await self._prepared_agent_executor().execute(
                messages=messages,
                agent_config=agent_config,
                prepared_agent=prepared_agent,
                is_remote=is_remote,
                remote_tool_dispatcher=remote_tool_dispatcher,
                trace_state=trace_state,
                session_id=session_id,
                round_num=round_num,
                session_step=session_step,
                round_step=round_step,
                trace_context_metadata=trace_context_metadata,
                llm_candidate_name=candidate.name,
                side_effect_fence=side_effect_fence,
            )


__all__ = ["DesktopModelStepRunner"]


def _desktop_logical_agent_path(metadata: JsonObject) -> str:
    for key in ("logical_agent_path", "agent_path", "agent_instance_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "desktop"
