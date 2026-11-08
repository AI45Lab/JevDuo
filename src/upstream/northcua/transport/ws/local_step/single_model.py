"""Single model-step runner for externally dispatched tool calls."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from copy import deepcopy
from typing import Any, cast

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.agent_context import AgentContext
from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.execution.hooks import AfterModelHookInput, BeforeAgentHookInput, BeforeModelHookInput
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.archs.session.id_generator import generate_run_id
from nexau.archs.tracer.context import TraceContext
from nexau.core.messages import Message

from northcua.agentinfra.nexau_compat.adapters.providers import LegacyLLMCallRequest, NexAUProviderAdapter
from northcua.agentkernel.tools.surface import active_provider_tool_payload
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject
from northcua.policy_invocation import policy_step_ref, use_policy_step_ref

from ..message_seed import prepend_runtime_system_messages, sync_runtime_agent_structured_tools
from ..tool_surface import pending_tool_calls_from_parsed_response, route_by_tool_name_from_runtime_tools
from .models import LocalStepModelResult
from .trace_context import _build_llm_trace_context, _safe_model_dump, _usage_payload

logger = logging.getLogger(__name__)
_DEFAULT_MODEL_STEP_TIMEOUT_SECONDS = 600.0
_NEXAU_PROVIDER_ADAPTER = NexAUProviderAdapter()


class ModelStepLLMCallError(RuntimeError):
    """Raised when one candidate's single LLM step fails before producing a response."""


class ModelStepLLMTimeoutError(TimeoutError):
    """Raised when one candidate's single LLM step exceeds its retry budget."""


class SingleModelStepRunner:
    """Run one local model call while leaving tool execution externalized."""

    async def run(
        self,
        *,
        messages: list[Message],
        agent: Agent,
        context: JsonObject | None = None,
    ) -> LocalStepModelResult:
        """Run exactly one model step locally without executing tool calls inline.

        Args:
            messages: Exact NorthCUA-authored messages for this model call.
            agent: Prepared runtime agent exposing NexAU stepwise hooks.
            context: Optional transport context merged into the agent context.

        Returns:
            Assistant text, stop reason, pending external tool calls, and state.
        """

        sync_runtime_agent_structured_tools(agent)
        merged_context = self._merged_context(agent=agent, context=context)
        controller_step_ref = policy_step_ref(merged_context)
        with AgentContext(context=merged_context) as agent_ctx:
            agent_state = self._build_agent_state(agent=agent, agent_ctx=agent_ctx)
            current_messages = list(messages)
            middleware_manager = agent.executor.middleware_manager
            if middleware_manager is not None:
                before_agent_input = BeforeAgentHookInput(
                    agent_state=agent_state,
                    messages=current_messages,
                )
                current_messages = await asyncio.to_thread(
                    middleware_manager.run_before_agent,
                    before_agent_input,
                )
            current_messages = prepend_runtime_system_messages(
                agent=agent,
                messages=current_messages,
                merged_context=merged_context,
            )
            # This mirrors NexAU's normal prompt injection boundary: generated
            # system prompts are prepended after before-agent hooks and before
            # before-model hooks, and only when NorthCUA did not already provide
            # a system message.
            before_model_input = BeforeModelHookInput(
                agent_state=agent_state,
                max_iterations=1,
                current_iteration=0,
                messages=current_messages,
            )
            if middleware_manager is not None:
                current_messages = await asyncio.to_thread(
                    middleware_manager.run_before_model,
                    before_model_input,
                )
            active_payload = active_provider_tool_payload(agent)
            allowed_tool_names = (
                {_provider_tool_name(tool) for tool in active_payload} if active_payload is not None else None
            )
            tools_payload = cast(
                list[JsonObject] | None,
                (
                    (active_payload if active_payload is not None else agent.executor.structured_tool_payload)
                    if agent.executor.use_structured_tool_calls
                    else None
                ),
            )
            tracer = agent.global_storage.get("tracer")
            trace_ctx = _build_llm_trace_context(
                tracer=tracer,
                agent=agent,
                messages=current_messages,
                tools_payload=tools_payload,
                context=merged_context,
            )
            if trace_ctx is not None:
                with trace_ctx:
                    return await self._call_model(
                        agent=agent,
                        agent_state=agent_state,
                        current_messages=current_messages,
                        middleware_manager=middleware_manager,
                        tools_payload=tools_payload,
                        trace_ctx=trace_ctx,
                        controller_step_ref=controller_step_ref,
                        allowed_tool_names=allowed_tool_names,
                    )
            return await self._call_model(
                agent=agent,
                agent_state=agent_state,
                current_messages=current_messages,
                middleware_manager=middleware_manager,
                tools_payload=tools_payload,
                trace_ctx=None,
                controller_step_ref=controller_step_ref,
                allowed_tool_names=allowed_tool_names,
            )

    @staticmethod
    def _merged_context(
        *,
        agent: Agent,
        context: JsonObject | None,
    ) -> JsonObject:
        base_context = dict(getattr(agent.config, "initial_context", None) or {})
        if context:
            base_context.update(context)
        return AgentContext.from_sources(
            initial_context=base_context,
            template=getattr(getattr(agent, "_variables", None), "template", None),
        ).context

    @staticmethod
    def _build_agent_state(*, agent: Agent, agent_ctx: AgentContext) -> AgentState:
        run_id = generate_run_id()
        return AgentState(
            agent_name=agent.agent_name,
            agent_id=agent.agent_id,
            run_id=run_id,
            root_run_id=run_id,
            context=agent_ctx,
            global_storage=agent.global_storage,
            tool_registry=agent._tool_registry,
            sandbox_manager=agent.sandbox_manager,
            variables=getattr(agent, "_variables", None),
            team_state=None,
            token_trace_session=getattr(agent, "_token_trace_session", None),
            subagent_manager=agent.executor.subagent_manager,
            skill_registry=getattr(agent, "skill_registry", {}),
        )

    async def _call_model(
        self,
        *,
        agent: Agent,
        agent_state: AgentState,
        current_messages: list[Message],
        middleware_manager: object | None,
        tools_payload: list[JsonObject] | None,
        trace_ctx: TraceContext | None,
        controller_step_ref: str,
        allowed_tool_names: set[str] | None,
    ) -> LocalStepModelResult:
        """Call the model once and optionally enrich the active LLM trace span."""

        timeout_seconds = _model_step_timeout_seconds(agent) * getattr(agent, "_no_tool_call_attempts", 1)
        call_started_at = time.perf_counter()
        llm_config = getattr(agent.config, "llm_config", None)
        model_name = getattr(llm_config, "model", None)
        api_type = getattr(llm_config, "api_type", None)
        configured_timeout_seconds = getattr(llm_config, "timeout", None)
        configured_max_retries = getattr(llm_config, "max_retries", None)
        response_recovery = bool(getattr(agent, "_northcua_response_recovery", False))
        response_retries = _non_negative_int(configured_max_retries) or 0
        if response_recovery:
            # Keep the historical per-attempt 3600s guard and allow backoff.
            timeout_seconds = 3600.0 * (response_retries + 1) + 31.0 * response_retries
        log_runtime_event(
            logger,
            "single_model_step_llm_call_started",
            level=logging.DEBUG,
            timeout_seconds=timeout_seconds,
            configured_timeout_seconds=configured_timeout_seconds,
            configured_max_retries=configured_max_retries,
            message_count=len(current_messages),
            tool_count=len(tools_payload or []),
            model=model_name,
            api_type=api_type,
        )
        try:
            with use_policy_step_ref(controller_step_ref):
                message_baseline = deepcopy(current_messages) if response_recovery else current_messages
                tool_baseline = deepcopy(tools_payload) if response_recovery else tools_payload

                async def provider_call():
                    return await _NEXAU_PROVIDER_ADAPTER.call_llm_async(
                        agent.executor.llm_caller,
                        LegacyLLMCallRequest(
                            messages=deepcopy(message_baseline) if response_recovery else current_messages,
                            api_type=str(api_type or ""),
                            openai_client=agent.openai_client,
                            force_stop_reason=AgentStopReason.SUCCESS,
                            agent_state=agent_state,
                            tool_call_mode=agent.executor.tool_call_mode,
                            tools=deepcopy(tool_baseline) if response_recovery else tools_payload,
                            shutdown_event=agent.executor.shutdown_event,
                            token_trace_session=agent_state.token_trace_session,
                        ),
                    )

                if response_recovery:
                    from northcua.agentinfra.providers.response_recovery import recover_response

                    pending_response = recover_response(
                        provider_call,
                        retries=response_retries,
                        max_tokens=_non_negative_int(getattr(llm_config, "max_tokens", None)) or 0,
                    )
                else:
                    pending_response = provider_call()
                model_response = await asyncio.wait_for(
                    pending_response,
                    timeout=timeout_seconds,
                )
        except TimeoutError:
            elapsed_seconds = time.perf_counter() - call_started_at
            log_runtime_event(
                logger,
                "single_model_step_llm_call_timed_out",
                level=logging.WARNING,
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed_seconds,
                configured_timeout_seconds=configured_timeout_seconds,
                configured_max_retries=configured_max_retries,
                message_count=len(current_messages),
                tool_count=len(tools_payload or []),
                model=model_name,
                api_type=api_type,
            )
            if trace_ctx is not None:
                trace_ctx.set_outputs(
                    {
                        "result": None,
                        "stop_reason": AgentStopReason.ERROR_OCCURRED.value,
                        "error": f"LLM call timed out after {timeout_seconds:.1f} seconds.",
                    }
                )
            raise ModelStepLLMTimeoutError(f"LLM call timed out after {timeout_seconds:.1f} seconds.")
        except asyncio.CancelledError:
            elapsed_seconds = time.perf_counter() - call_started_at
            log_runtime_event(
                logger,
                "single_model_step_llm_call_cancelled",
                level=logging.WARNING,
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed_seconds,
                configured_timeout_seconds=configured_timeout_seconds,
                configured_max_retries=configured_max_retries,
                message_count=len(current_messages),
                tool_count=len(tools_payload or []),
                model=model_name,
                api_type=api_type,
            )
            if trace_ctx is not None:
                trace_ctx.set_outputs(
                    {
                        "result": None,
                        "stop_reason": AgentStopReason.ERROR_OCCURRED.value,
                        "error": "LLM call was cancelled before producing a response.",
                    }
                )
            raise
        except Exception as exc:
            elapsed_seconds = time.perf_counter() - call_started_at
            log_runtime_event(
                logger,
                "single_model_step_llm_call_failed",
                level=logging.WARNING,
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed_seconds,
                configured_timeout_seconds=configured_timeout_seconds,
                configured_max_retries=configured_max_retries,
                message_count=len(current_messages),
                tool_count=len(tools_payload or []),
                model=model_name,
                api_type=api_type,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if trace_ctx is not None:
                trace_ctx.set_outputs(
                    {
                        "result": None,
                        "stop_reason": AgentStopReason.ERROR_OCCURRED.value,
                        "error": str(exc),
                    }
                )
            raise
        elapsed_seconds = time.perf_counter() - call_started_at
        if model_response is None:
            log_runtime_event(
                logger,
                "single_model_step_llm_call_failed",
                level=logging.WARNING,
                timeout_seconds=timeout_seconds,
                elapsed_seconds=elapsed_seconds,
                configured_timeout_seconds=configured_timeout_seconds,
                configured_max_retries=configured_max_retries,
                message_count=len(current_messages),
                tool_count=len(tools_payload or []),
                model=model_name,
                api_type=api_type,
                error_type="NoModelResponse",
                error="LLM caller returned no model response.",
            )
            if trace_ctx is not None:
                trace_ctx.set_outputs({"result": None, "stop_reason": AgentStopReason.ERROR_OCCURRED.value})
            raise ModelStepLLMCallError("LLM caller returned no model response.")
        log_runtime_event(
            logger,
            "single_model_step_llm_call_completed",
            level=logging.DEBUG,
            timeout_seconds=timeout_seconds,
            elapsed_seconds=elapsed_seconds,
            configured_timeout_seconds=configured_timeout_seconds,
            configured_max_retries=configured_max_retries,
            message_count=len(current_messages),
            tool_count=len(tools_payload or []),
            model=model_name,
            api_type=api_type,
        )
        assistant_text = model_response.content or ""
        parsed_response = agent.executor.response_parser.parse_response(model_response)
        assistant_message = model_response.to_ump_message()
        current_messages.append(assistant_message)
        if agent_state.token_trace_session is not None:
            agent_state.token_trace_session.append_model_response(
                output_token_ids=model_response.output_token_ids,
                fallback_messages=[assistant_message],
            )
        after_model_input = AfterModelHookInput(
            agent_state=agent_state,
            max_iterations=1,
            current_iteration=0,
            original_response=assistant_text,
            parsed_response=parsed_response,
            messages=current_messages,
            model_response=model_response,
        )
        force_continue = False
        if middleware_manager is not None:
            parsed_response, current_messages, force_continue = await asyncio.to_thread(
                cast(Any, middleware_manager).run_after_model,
                after_model_input,
            )
        pending_tool_calls = pending_tool_calls_from_parsed_response(
            parsed_response,
            route_by_tool_name=route_by_tool_name_from_runtime_tools(getattr(agent.config, "tools", []) or []),
            allowed_tool_names=allowed_tool_names,
        )
        response_payload = _safe_model_dump(getattr(model_response, "raw_message", None))
        usage_payload = _usage_payload(getattr(model_response, "usage", None))
        if pending_tool_calls:
            if trace_ctx is not None:
                trace_ctx.set_outputs(
                    {
                        "result": response_payload,
                        "assistant_text": assistant_text,
                        "usage": usage_payload,
                        "stop_reason": "EXTERNAL_TOOL_CALL",
                        "pending_tool_call_count": len(pending_tool_calls),
                    }
                )
            return LocalStepModelResult(
                response_text=assistant_text,
                stop_reason="EXTERNAL_TOOL_CALL",
                pending_tool_calls=pending_tool_calls,
                agent_state=agent_state,
                assistant_message=assistant_message,
            )
        stop_reason = "FORCE_CONTINUE" if force_continue else str(AgentStopReason.NO_MORE_TOOL_CALLS.value)
        if trace_ctx is not None:
            trace_ctx.set_outputs(
                {
                    "result": response_payload,
                    "assistant_text": assistant_text,
                    "usage": usage_payload,
                    "stop_reason": stop_reason,
                    "pending_tool_call_count": 0,
                }
            )
        return LocalStepModelResult(
            response_text=assistant_text,
            stop_reason=stop_reason,
            pending_tool_calls=[],
            agent_state=agent_state,
            assistant_message=assistant_message,
        )


def _model_step_timeout_seconds(agent: Agent) -> float:
    raw_env = os.environ.get("NORTHCUA_MODEL_STEP_TIMEOUT_SECONDS", "")
    if raw_env:
        try:
            parsed = float(raw_env)
        except ValueError:
            return _DEFAULT_MODEL_STEP_TIMEOUT_SECONDS
        return parsed if parsed > 0 else _DEFAULT_MODEL_STEP_TIMEOUT_SECONDS

    llm_budget = _model_step_llm_retry_budget_seconds(agent)
    if llm_budget is not None:
        return llm_budget

    return _DEFAULT_MODEL_STEP_TIMEOUT_SECONDS


def _provider_tool_name(tool: JsonObject) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or tool.get("name") or "")
    return str(tool.get("name") or "")


def _model_step_llm_retry_budget_seconds(agent: Agent) -> float | None:
    """Return a hard guard for the currently selected candidate's LLM retries.

    NexAU/OpenAI SDK retry handling lives inside ``call_llm_async``. The
    outer websocket step guard must therefore cover this candidate's retry
    budget, not just one provider request timeout, otherwise it cancels the
    retry loop before the candidate's retry policy can take effect.
    """

    llm_config = getattr(agent.config, "llm_config", None)
    timeout = _positive_float(getattr(llm_config, "timeout", None))
    if timeout is None:
        return None
    max_retries = _non_negative_int(getattr(llm_config, "max_retries", None)) or 0
    return timeout * float(max_retries + 1)


def _positive_float(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


def _non_negative_int(value: object) -> int | None:
    if not isinstance(value, int):
        return None
    return value if value >= 0 else None


__all__ = ["ModelStepLLMCallError", "ModelStepLLMTimeoutError", "SingleModelStepRunner"]
