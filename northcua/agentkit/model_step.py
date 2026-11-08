"""Shared model/tool step execution for AgentKit agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.context.adapters import (
    ProviderContextBudgetGuard,
    install_provider_context_budget_guard,
    is_context_overflow_error,
)
from northcua.agentinfra.context.bootstrap import ContextSideEffectFence, prepare_context_messages
from northcua.agentinfra.providers.llm_fallback import (
    agent_config_for_llm_candidate,
    conservative_context_budget_inputs,
    llm_fallback_candidates,
)
from northcua.agentinfra.providers.reasoning_replay import (
    ReasoningReplayProjector,
    reasoning_replay_policy_from_config,
)
from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.context.strategies import ContextBudgetExceededError, ContextSummaryError
from northcua.agentkernel.json_types import ProviderToolChoice
from northcua.agentkernel.tools.surface import ToolSurfaceService, normalize_allowed_tool_names
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkit.agent_config import clone_agent_config_with_hooks
from northcua.app.logging import log_runtime_event
from northcua.core.config import NorthCUAAppConfig
from northcua.core.json_types import JsonObject
from northcua.transport.ws.message_seed import supports_stepwise_local_execution
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

logger = logging.getLogger(__name__)
_REASONING_REPLAY_PROJECTOR = ReasoningReplayProjector()
EnvelopeT = TypeVar("EnvelopeT")
EnvelopeBuilder = Callable[[list[dict[str, object]], dict[str, object] | str | None], EnvelopeT]
MessageExtractor = Callable[[EnvelopeT], list[Message]]
AgentConfigMutator = Callable[[AgentConfig], None]


@dataclass(slots=True)
class AgentKitModelStepComponents:
    """Dependencies required by the shared model step executor.

    Attributes:
        app_config: Resolved NorthCUA application configuration.
        agent_config: Model/agent configuration cloned for each provider try.
        execution_transport: Transport that prepares and runs NexAU agents.
        user_id: User id used to scope prepared model sessions.
        transport_capabilities: Local or remote tool-execution capabilities.
        tool_surface_service: Builder for provider tool descriptors.
    """

    app_config: NorthCUAAppConfig
    agent_config: AgentConfig
    execution_transport: NorthCUAWebSocketTransport
    user_id: str
    transport_capabilities: AgentTransportCapabilities
    tool_surface_service: ToolSurfaceService


@dataclass(slots=True)
class AgentKitModelStepResult(Generic[EnvelopeT]):
    """Captured result of one AgentKit model step.

    Attributes:
        trace_state: Captured request/response/tool execution state.
        response_text: User-visible assistant text after normalization.
        meta: Provider metadata returned by the transport.
        envelope: Prompt envelope that was sent to the provider.
    """

    trace_state: TraceCaptureState
    response_text: str
    meta: JsonObject
    envelope: EnvelopeT


class AgentKitModelStepExecutor:
    """Prepare a model agent, expose tools, run the model, and capture traces.

    This is the common harness path for low-code agents. Agent-specific code
    supplies only prompt projection and context metadata; tool execution,
    provider fallback, trace hooks, and iteration budgeting stay centralized.
    """

    _MIN_TOOL_CALL_ITERATIONS = 3

    def __init__(self, components: AgentKitModelStepComponents) -> None:
        self.app_config = components.app_config
        self.agent_config = components.agent_config
        self.execution_transport = components.execution_transport
        self.user_id = components.user_id
        self.transport_capabilities = components.transport_capabilities
        self.tool_surface_service = components.tool_surface_service

    async def run_step(
        self,
        *,
        agent_kind: str,
        session_id: str,
        step_index: int,
        build_envelope: EnvelopeBuilder[EnvelopeT],
        envelope_messages: MessageExtractor[EnvelopeT],
        context: JsonObject,
        remote: bool = False,
        externalize_tools: bool = False,
        configure_agent: AgentConfigMutator | None = None,
        build_shadow_envelope: EnvelopeBuilder[EnvelopeT] | None = None,
        tool_choice_override: ProviderToolChoice = None,
    ) -> AgentKitModelStepResult[EnvelopeT]:
        """Run one model/tool step with provider fallback and trace capture.

        Args:
            agent_kind: Registry kind used in logs and trace metadata.
            session_id: Session id for model and tool execution state.
            step_index: One-based step index in the active round.
            build_envelope: Callback that builds messages from tool surface.
            envelope_messages: Callback extracting provider messages from the
                agent-specific envelope type.
            context: Structured runtime context passed to tool invocation.
            remote: Whether the transport should prepare a remote-capable agent.
            externalize_tools: Whether tool calls should be routed outside the
                local model process.
            configure_agent: Optional mutator for agent-specific config knobs.
            tool_choice_override: Optional provider-neutral override for tool choice.

        Returns:
            Captured step result containing prompt envelope, text, metadata, and
            trace state.

        Raises:
            Exception: Re-raises the last provider/model preparation or
                execution failure after all configured fallback candidates fail.
        """
        candidates = llm_fallback_candidates(self.agent_config)
        primary_llm_config = candidates[0].llm_config
        context_mode = self.app_config.context_management.resolve(agent_kind).mode
        logical_agent_path = _logical_agent_path(context, agent_kind=agent_kind)
        conservative_context_limit: int | None = None
        conservative_output_reserve: int | None = None
        if context_mode != ContextManagementMode.OFF:
            conservative_context_limit, conservative_output_reserve = conservative_context_budget_inputs(
                self.agent_config,
                candidates,
            )
        context_artifact_root = _context_artifact_root(context)
        cached_context_messages: list[Message] | None = None
        cached_context_source_messages: list[Message] | None = None
        cached_hard_input_budget: int | None = None
        shadow_observed = False
        last_model_error: Exception | None = None
        for attempt_index, candidate in enumerate(candidates):
            candidate_recovery_is_side_effect_safe = False
            side_effect_fence = ContextSideEffectFence(mode=context_mode)
            trace_state = TraceCaptureState()
            trace_capture_service = TraceCaptureService(candidate.llm_config)
            before_model_hooks, after_model_hooks, after_tool_hooks = trace_capture_service.build_hooks(trace_state)
            provider_guard = (
                ProviderContextBudgetGuard(model=str(candidate.llm_config.model or "gpt-4o"))
                if context_mode == ContextManagementMode.ENFORCE
                else None
            )
            agent_config = self._prepare_agent_config(
                before_model_hooks=before_model_hooks,
                after_model_hooks=after_model_hooks,
                after_tool_hooks=after_tool_hooks,
                before_tool_hooks=(
                    [side_effect_fence.before_tool] if context_mode == ContextManagementMode.ENFORCE else []
                ),
            )
            if configure_agent is not None:
                configure_agent(agent_config)
            agent_config = agent_config_for_llm_candidate(agent_config, candidate)
            resolved_tool_choice_override = _apply_provider_tool_choice_override(
                agent_config,
                tool_choice_override,
            )
            try:
                prepared_agent = await self.execution_transport.prepare_agent(
                    user_id=self.user_id,
                    session_id=session_id,
                    agent_config=agent_config,
                    remote=remote,
                    externalize_tools=externalize_tools,
                    transport_capabilities=self.transport_capabilities,
                )
                candidate_recovery_is_side_effect_safe = remote or supports_stepwise_local_execution(prepared_agent)
                tool_surface = self.tool_surface_service.activate_for_agent(
                    prepared_agent,
                    tool_choice_policy=str(context.get("tool_choice_policy", "auto")),
                    allowed_tool_names=normalize_allowed_tool_names(context.get("allowed_tool_names")),
                )
                if resolved_tool_choice_override is not None:
                    tool_surface.tool_choice = resolved_tool_choice_override
                trace_state.provider_tools = list(tool_surface.tools)
                trace_state.provider_tool_choice = tool_surface.tool_choice
                envelope = build_envelope(tool_surface.tools, tool_surface.tool_choice)
                shadow_envelope = None
                if context_mode == ContextManagementMode.SHADOW and build_shadow_envelope is not None:
                    try:
                        shadow_envelope = build_shadow_envelope(tool_surface.tools, tool_surface.tool_choice)
                    except Exception:
                        logger.warning("Shadow context envelope build failed open", exc_info=True)
                tracer = prepared_agent.global_storage.get("tracer") or agent_config.resolved_tracer
                tracer_baseline = trace_capture_service.snapshot_tracer(tracer)
                captured_span_ids: set[str] | None = None
                try:
                    messages = envelope_messages(envelope)
                    round_num = _positive_context_int(context, "round_num", default=1)
                    if context_mode == ContextManagementMode.ENFORCE and cached_context_messages is not None:
                        messages = list(cached_context_messages)
                    elif context_mode == ContextManagementMode.SHADOW and shadow_observed:
                        # Provider still receives this candidate's untouched
                        # legacy envelope; the logical step audit is single-shot.
                        messages = envelope_messages(envelope)
                    else:
                        source_messages = messages
                        prepared_context = await prepare_context_messages(
                            app_config=self.app_config,
                            agent_kind=agent_kind,
                            user_id=self.user_id,
                            session_id=session_id,
                            task_epoch=_task_epoch(context),
                            logical_agent_path=logical_agent_path,
                            logical_step_id=f"{session_id}:{agent_kind}:{round_num}:{step_index}",
                            round_num=round_num,
                            messages=source_messages,
                            shadow_messages=(
                                envelope_messages(shadow_envelope) if shadow_envelope is not None else None
                            ),
                            tools=list(tool_surface.tools),
                            agent_config=agent_config,
                            prepared_agent=prepared_agent,
                            primary_llm_config=primary_llm_config,
                            metadata={
                                "agent_kind": agent_kind,
                                "round_num": round_num,
                                "step_index": step_index,
                                "candidate_name": candidate.name,
                            },
                            context_limit_override=conservative_context_limit,
                            output_reserve_override=conservative_output_reserve,
                            artifact_root_override=context_artifact_root,
                        )
                        messages = prepared_context.messages
                        if context_mode == ContextManagementMode.SHADOW:
                            shadow_observed = True
                        if context_mode == ContextManagementMode.ENFORCE:
                            cached_context_messages = list(messages)
                            cached_context_source_messages = list(source_messages)
                            if prepared_context.result is None or prepared_context.result.budget is None:
                                raise RuntimeError("enforce context preparation returned no resolved budget")
                            cached_hard_input_budget = prepared_context.result.budget.hard_input_budget
                    messages = _REASONING_REPLAY_PROJECTOR.project_messages(
                        messages,
                        api_type=str(candidate.llm_config.api_type),
                        policy=reasoning_replay_policy_from_config(candidate.llm_config),
                    )
                    if context_mode == ContextManagementMode.ENFORCE:
                        if provider_guard is None:
                            raise RuntimeError("enforce context provider guard was not initialized")
                        if cached_hard_input_budget is None:
                            raise RuntimeError("enforce context preparation returned no provider guard budget")
                        provider_guard.configure(
                            hard_input_budget=cached_hard_input_budget,
                            tools=list(tool_surface.tools),
                        )
                        install_provider_context_budget_guard(prepared_agent.executor.llm_caller, provider_guard)
                    try:
                        raw_result, _agent = await self.execution_transport.run_local_round(
                            messages=messages,
                            user_id=self.user_id,
                            session_id=session_id,
                            agent_config=agent_config,
                            agent=prepared_agent,
                            context=context,
                            externalize_tools=externalize_tools,
                            transport_capabilities=self.transport_capabilities,
                        )
                    except Exception as exc:
                        if (
                            context_mode != ContextManagementMode.ENFORCE
                            or not candidate_recovery_is_side_effect_safe
                            or not is_context_overflow_error(exc)
                        ):
                            raise
                        recovered_context = await prepare_context_messages(
                            app_config=self.app_config,
                            agent_kind=agent_kind,
                            user_id=self.user_id,
                            session_id=session_id,
                            task_epoch=_task_epoch(context),
                            logical_agent_path=logical_agent_path,
                            logical_step_id=f"{session_id}:{agent_kind}:{round_num}:{step_index}:overflow-recovery",
                            round_num=round_num,
                            messages=cached_context_source_messages or envelope_messages(envelope),
                            tools=list(tool_surface.tools),
                            agent_config=agent_config,
                            prepared_agent=prepared_agent,
                            primary_llm_config=primary_llm_config,
                            metadata={
                                "agent_kind": agent_kind,
                                "round_num": round_num,
                                "step_index": step_index,
                                "candidate_name": candidate.name,
                                "provider_overflow_recovery": True,
                            },
                            recover_provider_overflow=True,
                            context_limit_override=conservative_context_limit,
                            output_reserve_override=conservative_output_reserve,
                            artifact_root_override=context_artifact_root,
                        )
                        cached_context_messages = list(recovered_context.messages)
                        if recovered_context.result is None or recovered_context.result.budget is None:
                            raise RuntimeError("overflow recovery returned no resolved budget")
                        if provider_guard is None:
                            raise RuntimeError("enforce context provider guard was not initialized")
                        cached_hard_input_budget = recovered_context.result.budget.hard_input_budget
                        provider_guard.configure(
                            hard_input_budget=cached_hard_input_budget,
                            tools=list(tool_surface.tools),
                        )
                        raw_result, _agent = await self.execution_transport.run_local_round(
                            messages=recovered_context.messages,
                            user_id=self.user_id,
                            session_id=session_id,
                            agent_config=agent_config,
                            agent=prepared_agent,
                            context=context,
                            externalize_tools=externalize_tools,
                            transport_capabilities=self.transport_capabilities,
                        )
                    captured_span_ids = trace_capture_service.new_tracer_span_ids(tracer, tracer_baseline)
                    trace_capture_service.enrich_from_tracer_span_ids(trace_state, tracer, captured_span_ids)
                finally:
                    if captured_span_ids is None:
                        captured_span_ids = trace_capture_service.new_tracer_span_ids_best_effort(
                            tracer, tracer_baseline
                        )
                    cleared_span_count = trace_capture_service.clear_tracer_spans_best_effort(tracer, captured_span_ids)
                    if cleared_span_count:
                        log_runtime_event(
                            logger,
                            "agentkit_model_step_tracer_spans_pruned",
                            level=logging.DEBUG,
                            agent_kind=agent_kind,
                            session_id=session_id,
                            step_index=step_index,
                            candidate_index=candidate.index,
                            candidate_name=candidate.name,
                            cleared_span_count=cleared_span_count,
                        )
                response_text, meta = raw_result if isinstance(raw_result, tuple) else (str(raw_result), {})
                response_text = self.normalize_response_text(response_text=response_text, trace_state=trace_state)
                log_runtime_event(
                    logger,
                    "agentkit_model_step_selected",
                    level=logging.DEBUG,
                    agent_kind=agent_kind,
                    session_id=session_id,
                    step_index=step_index,
                    candidate_index=candidate.index,
                    candidate_name=candidate.name,
                    tool_execution_count=len(trace_state.tool_executions),
                )
                return AgentKitModelStepResult(
                    trace_state=trace_state,
                    response_text=response_text,
                    meta=meta if isinstance(meta, dict) else {},
                    envelope=envelope,
                )
            except Exception as exc:
                if isinstance(exc, (ContextBudgetExceededError, ContextSummaryError)):
                    raise
                side_effect_fence.raise_if_candidate_replay_unsafe(exc)
                if (
                    context_mode == ContextManagementMode.ENFORCE
                    and not candidate_recovery_is_side_effect_safe
                    and is_context_overflow_error(exc)
                ):
                    raise ContextBudgetExceededError(
                        "provider overflow in exact/full-loop execution cannot be replayed safely"
                    ) from exc
                last_model_error = exc
                if attempt_index >= len(candidates) - 1:
                    raise
                log_runtime_event(
                    logger,
                    "agentkit_model_step_failed",
                    level=logging.WARNING,
                    agent_kind=agent_kind,
                    session_id=session_id,
                    step_index=step_index,
                    candidate_index=candidate.index,
                    candidate_name=candidate.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        raise RuntimeError(f"No LLM candidate was available for {agent_kind} step execution") from last_model_error

    def _prepare_agent_config(
        self,
        *,
        before_model_hooks: list[object],
        after_model_hooks: list[object],
        after_tool_hooks: list[object],
        before_tool_hooks: list[object],
    ) -> AgentConfig:
        agent_config = clone_agent_config_with_hooks(
            self.agent_config,
            before_model_hooks=before_model_hooks,
            after_model_hooks=after_model_hooks,
            after_tool_hooks=after_tool_hooks,
            before_tool_hooks=before_tool_hooks,
        )
        agent_config.max_iterations = max(
            int(agent_config.max_iterations or 0),
            int(self.app_config.execution.max_agent_iterations_per_round),
            self._MIN_TOOL_CALL_ITERATIONS,
        )
        return agent_config

    @staticmethod
    def normalize_response_text(*, response_text: str, trace_state: TraceCaptureState) -> str:
        """Prefer captured assistant text over transport fallback errors.

        Args:
            response_text: Text returned by the transport wrapper.
            trace_state: Captured provider response and tool-call trace.

        Returns:
            Assistant text suitable for terminal panels and prompt history.
        """
        normalized = response_text.strip()
        if (
            trace_state.tool_executions
            and normalized.startswith("[Error:")
            and ("Maximum iteration limit" in normalized)
        ):
            return trace_state.assistant_text.strip()
        return response_text or trace_state.assistant_text


def _apply_provider_tool_choice_override(
    agent_config: AgentConfig,
    override: ProviderToolChoice,
) -> ProviderToolChoice:
    """Apply one tool-choice override to the actual candidate LLM request config."""

    if override is None:
        return None
    llm_config = agent_config.llm_config
    if llm_config is None:
        raise RuntimeError("tool choice override requires an LLM config")
    resolved: ProviderToolChoice = override
    if override == "required" and llm_config.api_type == "anthropic_chat_completion":
        resolved = {"type": "any"}
    llm_config.set_param("tool_choice", resolved)
    return resolved


def _positive_context_int(context: JsonObject, key: str, *, default: int) -> int:
    value = context.get(key)
    return value if isinstance(value, int) and value > 0 else default


def _task_epoch(context: JsonObject) -> str:
    import hashlib

    task_id = context.get("task_id")
    if isinstance(task_id, (str, int)) and str(task_id).strip():
        return f"task-{task_id}"
    query = context.get("user_query") or context.get("query")
    if not isinstance(query, str) or not query.strip():
        evaluator_kind = context.get("evaluator_kind")
        round_num = context.get("round_num")
        if isinstance(evaluator_kind, str) and isinstance(round_num, int):
            return f"evaluator-{evaluator_kind}-round-{round_num}"
        return "session-task"
    digest = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:16]
    return f"task-{digest}"


def _context_artifact_root(context: JsonObject) -> Path | None:
    value = context.get("context_artifact_root")
    return Path(value).expanduser() if isinstance(value, str) and value.strip() else None


def _logical_agent_path(context: JsonObject, *, agent_kind: str) -> str:
    for key in ("logical_agent_path", "agent_path", "subagent_path", "agent_instance_id"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return agent_kind
