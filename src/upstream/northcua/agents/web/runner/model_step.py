"""Shared model-step execution support for the web-agent runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from northcua.agentkernel.working_memory import WorkingMemoryStore

from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import Message

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.agentkernel.tools.surface import ToolSurfaceService
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkit.builtin_common import tool_ids_from_mcp_server
from northcua.agentkit.model_step import AgentKitModelStepComponents, AgentKitModelStepExecutor
from northcua.core.config import NorthCUAAppConfig
from northcua.core.json_types import JsonObject
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

from ..domain.observations import WebObservationContext
from ..mcp.registration import WEB_AGENT_MCP_REGISTRATION_HOOK
from ..prompt import WebAgentPromptBuilder, WebAgentPromptEnvelope
from ..prompt.visual_window import WebPromptVisualHistoryTurn


@dataclass(slots=True)
class WebAgentModelStepResult:
    """Captured output of one model/tool execution step.

    Attributes:
        trace_state: Captured provider/tool execution trace.
        response_text: User-visible assistant text after normalization.
        meta: Provider metadata returned by the shared executor.
        envelope: Prompt envelope sent for this step.
    """

    trace_state: TraceCaptureState
    response_text: str
    meta: JsonObject
    envelope: WebAgentPromptEnvelope


@dataclass(slots=True)
class WebAgentModelStepComponents:
    """Dependencies required to execute one web-agent model step.

    Attributes:
        app_config: Resolved NorthCUA application config.
        agent_config: Model/agent configuration.
        execution_transport: Transport used to prepare and run model agents.
        user_id: User id used to scope the prepared agent.
        transport_capabilities: Tool execution capabilities for this run.
        prompt_builder: Web-specific prompt envelope builder.
        tool_surface_service: Service that projects MCP tools to provider
            descriptors.
    """

    app_config: NorthCUAAppConfig
    agent_config: AgentConfig
    execution_transport: NorthCUAWebSocketTransport
    user_id: str
    transport_capabilities: AgentTransportCapabilities
    prompt_builder: WebAgentPromptBuilder
    tool_surface_service: ToolSurfaceService
    shadow_prompt_builder: WebAgentPromptBuilder | None = None
    memory_root: Path | None = None


class WebAgentModelStepExecutor:
    """Prepare prompts and delegate model/tool execution to AgentKit."""

    def __init__(self, components: WebAgentModelStepComponents) -> None:
        self.memory_store = WorkingMemoryStore(components.memory_root) if components.memory_root is not None else None
        self.app_config = components.app_config
        self.agent_config = components.agent_config
        self.execution_transport = components.execution_transport
        self.user_id = components.user_id
        self.transport_capabilities = components.transport_capabilities
        self.prompt_builder = components.prompt_builder
        self.tool_surface_service = components.tool_surface_service
        self.shadow_prompt_builder = components.shadow_prompt_builder
        self.step_executor = AgentKitModelStepExecutor(
            AgentKitModelStepComponents(
                app_config=self.app_config,
                agent_config=self.agent_config,
                execution_transport=self.execution_transport,
                user_id=self.user_id,
                transport_capabilities=self.transport_capabilities,
                tool_surface_service=self.tool_surface_service,
            )
        )

    async def run_step(
        self,
        *,
        user_query: str,
        session_id: str,
        step_index: int,
        session_step: int,
        round_num: int = 1,
        runtime_cursor: JsonObject | None = None,
        step_budget: JsonObject | None = None,
        tool_choice_policy: str = "auto",
        allowed_tool_names: tuple[str, ...] = (),
        observation: WebObservationContext,
        prior_messages: list[Message],
        visual_history_turns: list[WebPromptVisualHistoryTurn] | None = None,
    ) -> WebAgentModelStepResult:
        """Run one LLM/tool step with provider fallback support.

        Args:
            user_query: Original user task.
            session_id: Active NorthCUA session id.
            step_index: One-based web-agent step index.
            session_step: One-based step index across the whole session.
            round_num: One-based query/round number for trace metadata.
            runtime_cursor: Optional shared runtime cursor payload.
            step_budget: Optional shared runtime step-budget metadata.
            tool_choice_policy: Neutral per-task model tool-selection policy.
            allowed_tool_names: Exact model-visible web tool allowlist.
            observation: Latest page observation for prompt projection.
            prior_messages: Prompt history from earlier steps.

        Returns:
            Web-specific wrapper around the shared AgentKit model-step result.
        """

        context = self._build_trace_context_metadata(
            session_id=session_id,
            step_index=step_index,
            session_step=session_step,
            round_num=round_num,
            runtime_cursor=runtime_cursor,
            step_budget=step_budget,
            tool_choice_policy=tool_choice_policy,
            allowed_tool_names=allowed_tool_names,
            user_query=user_query,
        )
        context.update(self.prompt_builder.build_system_prompt_context(step_budget=step_budget))
        result = await self.step_executor.run_step(
            agent_kind="web",
            session_id=session_id,
            step_index=step_index,
            build_envelope=lambda tools, tool_choice: self.prompt_builder.build(
                user_query=user_query,
                observation=observation,
                prior_messages=prior_messages,
                tools=tools,
                tool_choice=tool_choice,
                allow_current_observation_image=session_step == 1,
                visual_history_turns=visual_history_turns,
            ),
            build_shadow_envelope=(
                lambda tools, tool_choice: (
                    self.shadow_prompt_builder.build(
                        user_query=user_query,
                        observation=observation,
                        prior_messages=prior_messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        allow_current_observation_image=session_step == 1,
                        visual_history_turns=visual_history_turns,
                    )
                    if self.shadow_prompt_builder is not None
                    else None
                )
            ),
            envelope_messages=lambda envelope: (
                self.memory_store.inject(envelope.messages, round_num=round_num)
                if self.memory_store is not None
                else envelope.messages
            ),
            context=context,
            configure_agent=self._configure_agent_for_step,
        )
        return WebAgentModelStepResult(
            trace_state=result.trace_state,
            response_text=result.response_text,
            meta=result.meta,
            envelope=result.envelope,
        )

    def _build_trace_context_metadata(
        self,
        *,
        session_id: str,
        step_index: int,
        session_step: int,
        round_num: int,
        runtime_cursor: JsonObject | None,
        step_budget: JsonObject | None,
        tool_choice_policy: str,
        allowed_tool_names: tuple[str, ...],
        user_query: str,
    ) -> JsonObject:
        metadata: JsonObject = {
            "agent_kind": "web",
            "artifact_dir": str(self.app_config.artifacts.root_dir),
            "northcua_session_id": session_id,
            "round_num": round_num,
            "round_step": step_index,
            "session_id": session_id,
            "session_step": session_step,
            "user_query": user_query,
        }
        if runtime_cursor is not None:
            metadata["runtime_cursor"] = runtime_cursor
        if step_budget is not None:
            metadata["step_budget"] = step_budget
        metadata["tool_choice_policy"] = tool_choice_policy
        metadata["allowed_tool_names"] = list(allowed_tool_names)
        experiment_id = self.app_config.execution.experiment_id.strip()
        if experiment_id:
            metadata["experiment_id"] = experiment_id
        return metadata

    @staticmethod
    def _configure_agent_for_step(agent_config: AgentConfig) -> None:
        stop_tools = set(agent_config.stop_tools or set())
        stop_tools.update(
            tool_ids_from_mcp_server(
                "WebAgentActionExecutor",
                registration_hook=WEB_AGENT_MCP_REGISTRATION_HOOK,
            )
        )
        agent_config.stop_tools = stop_tools
