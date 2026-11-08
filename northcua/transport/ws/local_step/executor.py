"""Local exact-message and one-step model execution."""

from __future__ import annotations

import logging

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.context_value import ContextValue
from nexau.core.messages import Message

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject
from northcua.runtime.mcp.invocation_context import MCPInvocationContext, use_mcp_invocation_context

from ..message_seed import (
    message_image_count,
    supports_stepwise_local_execution,
)
from .exact_messages import ExactMessageRoundRunner
from .models import (
    AgentRoundResult,
    BuildRuntimeAgentConfig,
    ExactMessagesRunner,
    LocalSingleModelStepRunner,
    LocalStepModelResult,
    PrepareAgent,
    RunAgentOnce,
)
from .single_model import SingleModelStepRunner

logger = logging.getLogger(__name__)


class LocalStepExecutor:
    """Execute one local round using exact-message or stepwise agent paths."""

    def __init__(
        self,
        *,
        prepare_agent: PrepareAgent,
        run_agent_once: RunAgentOnce,
        build_runtime_agent_config: BuildRuntimeAgentConfig,
        run_round_with_exact_messages: ExactMessagesRunner | None = None,
        run_local_single_model_step: LocalSingleModelStepRunner | None = None,
    ) -> None:
        """Initialize local step execution callbacks.

        Args:
            prepare_agent: Callback that creates a runtime agent.
            run_agent_once: Callback for one regular agent turn.
            build_runtime_agent_config: Callback that resolves runtime config.
            run_round_with_exact_messages: Optional exact-message override.
            run_local_single_model_step: Optional one-step override.
        """

        self._prepare_agent = prepare_agent
        self._run_agent_once = run_agent_once
        self._build_runtime_agent_config = build_runtime_agent_config
        self._run_round_with_exact_messages_override = run_round_with_exact_messages
        self._run_local_single_model_step_override = run_local_single_model_step
        self._exact_message_runner = ExactMessageRoundRunner(
            prepare_agent=prepare_agent,
            run_agent_once=run_agent_once,
        )
        self._single_model_step_runner = SingleModelStepRunner()

    async def run_round(
        self,
        *,
        messages: list[Message],
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        agent: Agent | None = None,
        context: JsonObject | None = None,
        externalize_tools: bool = False,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> AgentRoundResult:
        """Execute one local/in-process round using the provided messages.

        Args:
            messages: Exact NorthCUA-authored message list for this round.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Base agent config to adapt for local execution.
            agent: Optional existing runtime agent.
            context: Optional transport context merged into NexAU context.
            externalize_tools: Compatibility flag accepted by callers; local
                rounds intentionally ignore externalization.
            transport_capabilities: Optional runtime transport feature flags.

        Returns:
            The agent result and the runtime agent when one is available.
        """

        runtime_config = await self._build_runtime_agent_config(
            agent_config=agent_config,
            remote=False,
            externalize_tools=False,
            transport_capabilities=transport_capabilities,
        )
        if externalize_tools:
            log_runtime_event(
                logger,
                "transport_local_externalize_tools_ignored",
                level=logging.DEBUG,
                user_id=user_id,
                session_id=session_id,
            )
        if agent is not None and not supports_stepwise_local_execution(agent):
            log_runtime_event(
                logger,
                "transport_run_local_round_exact_messages",
                level=logging.DEBUG,
                user_id=user_id,
                session_id=session_id,
                externalize_tools=externalize_tools,
                message_count=len(messages),
                image_count=message_image_count(messages),
            )
            with use_mcp_invocation_context(MCPInvocationContext(session_id=session_id)):
                return await self._run_round_with_exact_messages_entry(
                    messages=messages,
                    user_id=user_id,
                    session_id=session_id,
                    agent_config=runtime_config,
                    agent=agent,
                    context=context,
                )
        runtime_agent = agent
        if runtime_agent is None:
            runtime_agent = await self._prepare_agent(
                user_id=user_id,
                session_id=session_id,
                agent_config=runtime_config,
                remote=False,
                externalize_tools=False,
                transport_capabilities=transport_capabilities,
            )
        if not supports_stepwise_local_execution(runtime_agent):
            with use_mcp_invocation_context(MCPInvocationContext(session_id=session_id)):
                return await self._run_round_with_exact_messages_entry(
                    messages=messages,
                    user_id=user_id,
                    session_id=session_id,
                    agent_config=runtime_config,
                    agent=runtime_agent,
                    context=context,
                )
        with use_mcp_invocation_context(MCPInvocationContext(session_id=session_id)):
            step_result = await self._run_local_single_model_step_entry(
                messages=messages,
                agent=runtime_agent,
                context=context,
            )
        if step_result.pending_tool_calls:
            return (
                step_result.response_text,
                {
                    # Keep these serialized field names stable: downstream
                    # round execution code keys on this exact pause payload.
                    "stop_reason": step_result.stop_reason,
                    "pending_tool_calls": step_result.pending_tool_calls,
                    "agent_state": step_result.agent_state,
                },
            ), runtime_agent
        return step_result.response_text, runtime_agent

    async def _run_round_with_exact_messages_entry(
        self,
        *,
        messages: list[Message],
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        agent: Agent | None = None,
        context: JsonObject | None = None,
        variables: ContextValue | None = None,
    ) -> AgentRoundResult:
        """Run the exact-message path, honoring an injected override.

        Args:
            messages: Exact message sequence to seed.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Runtime agent config.
            agent: Optional prepared runtime agent to reuse.
            context: Optional runtime context passed through to NexAU.
            variables: Optional NexAU context variables.

        Returns:
            Exact-message runner result.
        """

        if self._run_round_with_exact_messages_override is not None:
            return await self._run_round_with_exact_messages_override(
                messages=messages,
                user_id=user_id,
                session_id=session_id,
                agent_config=agent_config,
                agent=agent,
                context=context,
                variables=variables,
            )
        return await self._exact_message_runner.run(
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            agent_config=agent_config,
            agent=agent,
            context=context,
            variables=variables,
        )

    async def _run_local_single_model_step_entry(
        self,
        *,
        messages: list[Message],
        agent: Agent,
        context: JsonObject | None = None,
    ) -> LocalStepModelResult:
        """Run one local model step, honoring an injected override.

        Args:
            messages: Exact message sequence for the model call.
            agent: Prepared runtime agent.
            context: Optional runtime context merged into NexAU context.

        Returns:
            Local step model result.
        """

        if self._run_local_single_model_step_override is not None:
            return await self._run_local_single_model_step_override(
                messages=messages,
                agent=agent,
                context=context,
            )
        return await self._single_model_step_runner.run(
            messages=messages,
            agent=agent,
            context=context,
        )

    async def _run_round_with_exact_messages(
        self,
        *,
        messages: list[Message],
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        agent: Agent | None = None,
        context: JsonObject | None = None,
        variables: ContextValue | None = None,
    ) -> tuple[str | tuple[str, JsonObject], Agent]:
        """Compatibility hook for transports that still call the old private seam."""

        return await self._exact_message_runner.run(
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            agent_config=agent_config,
            agent=agent,
            context=context,
            variables=variables,
        )

    async def _run_local_single_model_step(
        self,
        *,
        messages: list[Message],
        agent: Agent,
        context: JsonObject | None = None,
    ) -> LocalStepModelResult:
        """Compatibility hook for transports that still call the old private seam."""

        return await self._single_model_step_runner.run(
            messages=messages,
            agent=agent,
            context=context,
        )


__all__ = ["LocalStepExecutor"]
