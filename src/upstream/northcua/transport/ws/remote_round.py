"""Remote model-step helpers for websocket transport."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject

from .local_step import LocalSingleModelStepRunner, LocalStepModelResult, PrepareAgent
from .message_seed import message_image_count
from .remote_tools import use_remote_command_tool_executor
from .result_blocks import RemoteToolDispatchResult

logger = logging.getLogger(__name__)

AgentRoundResult = tuple[str | tuple[str, JsonObject], Agent | None]
BuildRuntimeAgentConfig = Callable[..., Awaitable[AgentConfig]]
ExternalToolDispatcher = Callable[[list[JsonObject]], Awaitable[RemoteToolDispatchResult]]


class RemoteRoundExecutor:
    """Execute one remote model step while leaving commands externalized."""

    def __init__(
        self,
        *,
        prepare_agent: PrepareAgent,
        run_local_single_model_step: LocalSingleModelStepRunner,
        build_runtime_agent_config: BuildRuntimeAgentConfig,
    ) -> None:
        """Initialize remote round execution callbacks.

        Args:
            prepare_agent: Callback that creates a remote runtime agent.
            run_local_single_model_step: Shared one-turn model-step runner.
            build_runtime_agent_config: Callback that resolves runtime config.
        """

        self._prepare_agent = prepare_agent
        self._run_local_single_model_step = run_local_single_model_step
        self._build_runtime_agent_config = build_runtime_agent_config

    async def run_round(
        self,
        *,
        messages: list[Message],
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        dispatch_external_calls: ExternalToolDispatcher,
        agent: Agent | None = None,
        context: JsonObject | None = None,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> AgentRoundResult:
        """Execute one remote model step and return any external tool pause.

        Args:
            messages: Exact NorthCUA-authored messages for the first model call.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Base agent config to adapt for remote execution.
            dispatch_external_calls: Callback bound into remote direct-tool
                proxies. The desktop runtime executes the returned pending calls
                after this method returns, matching local step semantics.
            agent: Optional existing runtime agent.
            context: Optional runtime context for the initial model call.
            transport_capabilities: Optional runtime transport feature flags.

        Returns:
            Text result or ``EXTERNAL_TOOL_CALL`` pause metadata plus the agent.
        """

        runtime_config = await self._build_runtime_agent_config(
            agent_config=agent_config,
            remote=True,
            transport_capabilities=transport_capabilities,
        )

        runtime_agent = agent
        if runtime_agent is None:
            runtime_agent = await self._prepare_agent(
                user_id=user_id,
                session_id=session_id,
                agent_config=runtime_config,
                remote=True,
                externalize_tools=False,
                transport_capabilities=transport_capabilities,
            )
        log_runtime_event(
            logger,
            "transport_run_remote_round_started",
            level=logging.DEBUG,
            user_id=user_id,
            session_id=session_id,
            initial_message_count=len(messages),
            initial_image_count=message_image_count(messages),
        )

        async with use_remote_command_tool_executor(dispatch_external_calls):
            step_result = await self._run_local_single_model_step(
                messages=messages,
                agent=runtime_agent,
                context=context,
            )
        return self._result_from_step(step_result, user_id=user_id, session_id=session_id), runtime_agent

    @staticmethod
    def _result_from_step(
        step_result: LocalStepModelResult,
        *,
        user_id: str,
        session_id: str,
    ) -> str | tuple[str, JsonObject]:
        """Convert a shared model-step result into transport round output."""

        if step_result.pending_tool_calls:
            return (
                step_result.response_text,
                {
                    "stop_reason": step_result.stop_reason,
                    "pending_tool_calls": step_result.pending_tool_calls,
                    "agent_state": step_result.agent_state,
                },
            )
        log_runtime_event(
            logger,
            "transport_run_remote_round_completed",
            level=logging.DEBUG,
            user_id=user_id,
            session_id=session_id,
            result_type="text",
        )
        return step_result.response_text
