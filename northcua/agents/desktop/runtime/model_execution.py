"""Prepared-agent execution helpers for desktop model steps."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.context.bootstrap import ContextSideEffectFence
from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentkernel.tracing.capture import TraceCaptureState
from northcua.agents.desktop.round_execution.tool_dispatch import (
    CommandExecutorProtocol,
    LocalExternalToolDispatcher,
    RemoteExternalCommandExecutor,
)
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject
from northcua.core.models import ToolExecutionRecord
from northcua.transport.ws.remote_round import ExternalToolDispatcher
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from .contracts import DesktopModelStopReason, DesktopRuntimeEventName

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DesktopPreparedAgentExecutor:
    """Execute a prepared NexAU agent and handle external tool pauses.

    Attributes:
        user_id: NexAU user id used for local/remote execution.
        transport_capabilities: Runtime feature flags for the transport layer.
        execution_transport: Local/remote NexAU execution transport.
        local_tool_dispatcher: Dispatcher that runs pending desktop tool calls.
        round_coordinator: Desktop state facade for pending resume results.
        local_device_executor: Local command executor for desktop actions.
    """

    user_id: str
    transport_capabilities: AgentTransportCapabilities
    execution_transport: NorthCUAWebSocketTransport
    local_tool_dispatcher: LocalExternalToolDispatcher
    round_coordinator: NorthCUARoundCoordinator
    local_device_executor: CommandExecutorProtocol

    async def execute(
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
        llm_candidate_name: str,
        side_effect_fence: ContextSideEffectFence,
    ) -> None:
        """Execute one prepared agent turn.

        Args:
            messages: Provider messages for the model step.
            agent_config: Candidate-specific NexAU agent config.
            prepared_agent: Existing prepared agent instance.
            is_remote: Whether the turn runs through remote tool dispatch.
            remote_tool_dispatcher: Remote dispatcher required for remote mode.
            trace_state: Mutable trace capture state to enrich with tool results.
            session_id: Active desktop session id.
            round_num: One-based desktop round number.
            session_step: One-based session step.
            round_step: One-based round step.
            trace_context_metadata: JSON metadata passed through transport.
            llm_candidate_name: Fallback candidate being executed.

        Raises:
            RuntimeError: If remote mode is selected without a dispatcher.
        """

        if is_remote:
            await self._execute_remote(
                messages=messages,
                agent_config=agent_config,
                prepared_agent=prepared_agent,
                remote_tool_dispatcher=remote_tool_dispatcher,
                trace_state=trace_state,
                session_id=session_id,
                round_num=round_num,
                session_step=session_step,
                round_step=round_step,
                trace_context_metadata=trace_context_metadata,
                llm_candidate_name=llm_candidate_name,
                side_effect_fence=side_effect_fence,
            )
            return
        await self._execute_local(
            messages=messages,
            agent_config=agent_config,
            prepared_agent=prepared_agent,
            trace_state=trace_state,
            session_id=session_id,
            round_num=round_num,
            session_step=session_step,
            round_step=round_step,
            trace_context_metadata=trace_context_metadata,
            llm_candidate_name=llm_candidate_name,
            side_effect_fence=side_effect_fence,
        )

    async def _execute_remote(
        self,
        *,
        messages: list[Message],
        agent_config: AgentConfig,
        prepared_agent: Agent,
        remote_tool_dispatcher: ExternalToolDispatcher | None,
        trace_state: TraceCaptureState,
        session_id: str,
        round_num: int,
        session_step: int,
        round_step: int,
        trace_context_metadata: JsonObject,
        llm_candidate_name: str,
        side_effect_fence: ContextSideEffectFence,
    ) -> None:
        if remote_tool_dispatcher is None:
            raise RuntimeError("Remote runtime mode requires a tool dispatcher callback.")
        result, _agent = await self.execution_transport.run_remote_round(
            messages=messages,
            user_id=self.user_id,
            session_id=session_id,
            agent_config=agent_config,
            dispatch_external_calls=remote_tool_dispatcher,
            agent=prepared_agent,
            context=trace_context_metadata,
            transport_capabilities=self.transport_capabilities,
        )
        await self._handle_external_tool_pause(
            result=result,
            trace_state=trace_state,
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            session_step=session_step,
            prepared_agent=prepared_agent,
            device_executor=RemoteExternalCommandExecutor(remote_tool_dispatcher),
            llm_candidate_name=llm_candidate_name,
            event_name=DesktopRuntimeEventName.REMOTE_EXTERNAL_TOOLS_COMPLETED.value,
            side_effect_fence=side_effect_fence,
        )

    async def _execute_local(
        self,
        *,
        messages: list[Message],
        agent_config: AgentConfig,
        prepared_agent: Agent,
        trace_state: TraceCaptureState,
        session_id: str,
        round_num: int,
        session_step: int,
        round_step: int,
        trace_context_metadata: JsonObject,
        llm_candidate_name: str,
        side_effect_fence: ContextSideEffectFence,
    ) -> None:
        result, _agent = await self.execution_transport.run_local_round(
            messages=messages,
            user_id=self.user_id,
            session_id=session_id,
            agent_config=agent_config,
            agent=prepared_agent,
            context=trace_context_metadata,
            externalize_tools=False,
            transport_capabilities=self.transport_capabilities,
        )
        await self._handle_external_tool_pause(
            result=result,
            trace_state=trace_state,
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            session_step=session_step,
            prepared_agent=prepared_agent,
            device_executor=self.local_device_executor,
            llm_candidate_name=llm_candidate_name,
            event_name=DesktopRuntimeEventName.LOCAL_EXTERNAL_TOOLS_COMPLETED.value,
            side_effect_fence=side_effect_fence,
        )

    async def _handle_external_tool_pause(
        self,
        *,
        result: object,
        trace_state: TraceCaptureState,
        session_id: str,
        round_num: int,
        round_step: int,
        session_step: int,
        prepared_agent: object,
        device_executor: CommandExecutorProtocol,
        llm_candidate_name: str,
        event_name: str,
        side_effect_fence: ContextSideEffectFence,
    ) -> None:
        if not isinstance(result, tuple):
            return
        _response_text, meta = result
        if not isinstance(meta, dict):
            return
        pending_tool_calls = list(meta.get("pending_tool_calls") or [])
        log_runtime_event(
            logger,
            DesktopRuntimeEventName.EXTERNAL_TOOL_PAUSE_RECEIVED.value,
            level=logging.DEBUG,
            session_id=session_id,
            session_step=session_step,
            stop_reason=meta.get("stop_reason"),
            pending_tool_call_count=len(pending_tool_calls),
            llm_candidate=llm_candidate_name,
        )
        if meta.get("stop_reason") != DesktopModelStopReason.EXTERNAL_TOOL_CALL.value:
            return
        agent_state = meta.get("agent_state")
        self.local_tool_dispatcher.set_device_executor(device_executor)
        # Mark before dispatch: a tool may mutate the environment and then
        # raise, in which case after-tool tracing never runs.
        side_effect_fence.mark_external_tool_dispatch()
        local_results, local_tool_executions = await self.local_tool_dispatcher.execute(
            session_id=session_id,
            round_num=round_num,
            round_step=round_step,
            pending_tool_calls=cast(list[JsonObject], pending_tool_calls),
            agent=prepared_agent,
            agent_state=agent_state,
        )
        # The next runtime step replays these command rows through history; they
        # are not a separate remote loop, so shared runtime remains counter owner.
        self.round_coordinator.state.pending_resume_results = list(local_results)
        merge_local_tool_executions(trace_state, local_tool_executions)
        log_runtime_event(
            logger,
            event_name,
            session_id=session_id,
            session_step=session_step,
            pending_tool_call_count=len(pending_tool_calls),
            result_count=len(local_results),
            call_ids=[result.call_id for result in local_results],
            llm_candidate=llm_candidate_name,
        )


def provider_tool_name(tool: object) -> object:
    """Return a provider tool name for compact runtime logging.

    Args:
        tool: Provider tool spec object, usually an OpenAI-style dictionary.

    Returns:
        Tool name when present, otherwise ``None``.
    """

    if not isinstance(tool, dict):
        return None
    direct_name = tool.get("name")
    function = tool.get("function")
    if isinstance(function, dict):
        return direct_name or function.get("name")
    return direct_name


def merge_local_tool_executions(
    trace_state: TraceCaptureState,
    local_tool_executions: list[ToolExecutionRecord],
) -> None:
    """Merge authoritative local dispatch results into trace capture state.

    Args:
        trace_state: Trace capture state collected from NexAU hooks.
        local_tool_executions: Command results from desktop dispatch adapters.
    """

    if not local_tool_executions:
        return
    if not trace_state.tool_executions:
        trace_state.tool_executions.extend(local_tool_executions)
        return
    trace_index_by_call_id = {
        execution.tool_call_id: index for index, execution in enumerate(trace_state.tool_executions)
    }
    for local_execution in local_tool_executions:
        index = trace_index_by_call_id.get(local_execution.tool_call_id)
        if index is None:
            trace_state.tool_executions.append(local_execution)
            continue
        existing = trace_state.tool_executions[index]
        if existing.tool_output is None and existing.llm_tool_output is None:
            trace_state.tool_executions[index] = local_execution


__all__ = ["DesktopPreparedAgentExecutor", "merge_local_tool_executions", "provider_tool_name"]
