"""NexAU TransportBase-backed execution transport for NorthCUA websocket runtime."""

from __future__ import annotations

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.context_value import ContextValue
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import DatabaseEngine
from nexau.archs.transports.base import TransportBase
from nexau.core.messages import Message

from northcua.agentinfra.nexau_compat.adapters.runtime import NexAURuntimeAdapter
from northcua.agentinfra.providers.prompt_cache import ensure_session_prompt_cache_key
from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.core.json_types import JsonObject

from .agent_config import RuntimeAgentConfigBuilder
from .config import NorthCUAWebSocketTransportConfig
from .local_step import AgentRoundResult, LocalStepExecutor, LocalStepModelResult
from .remote_round import ExternalToolDispatcher, RemoteRoundExecutor

_NEXAU_RUNTIME_ADAPTER = NexAURuntimeAdapter()


class NorthCUAWebSocketTransport(TransportBase[NorthCUAWebSocketTransportConfig]):
    """Execution transport that reuses NexAU's session and agent lifecycle."""

    def __init__(
        self,
        *,
        engine: DatabaseEngine,
        session_manager: SessionManager,
        config: NorthCUAWebSocketTransportConfig,
        default_agent_config: AgentConfig,
        raw_agent_mcp_servers: list[JsonObject] | None = None,
        heartbeat_interval: float = 10.0,
    ) -> None:
        """Initialize the websocket execution transport.

        Args:
            engine: NexAU database engine.
            session_manager: NexAU session manager.
            config: Websocket transport configuration.
            default_agent_config: Default agent config for requests.
            raw_agent_mcp_servers: Optional raw MCP server configs from app configuration.
            heartbeat_interval: Heartbeat interval used by the base transport.
        """

        super().__init__(
            engine=engine,
            config=config,
            default_agent_config=default_agent_config,
            heartbeat_interval=heartbeat_interval,
        )
        self._session_manager = session_manager
        self._running = False
        self._raw_agent_mcp_servers = [dict(server) for server in raw_agent_mcp_servers or []]
        self._runtime_agent_config_builder = RuntimeAgentConfigBuilder(
            raw_agent_mcp_servers=self._raw_agent_mcp_servers
        )
        self._local_step_executor = LocalStepExecutor(
            prepare_agent=lambda **kwargs: self.prepare_agent(**kwargs),
            run_agent_once=lambda **kwargs: self.run_agent_once(**kwargs),
            build_runtime_agent_config=lambda **kwargs: self.build_runtime_agent_config(**kwargs),
            run_round_with_exact_messages=lambda **kwargs: self._run_round_with_exact_messages(**kwargs),
            run_local_single_model_step=lambda **kwargs: self._run_local_single_model_step(**kwargs),
        )
        self._remote_round_executor = RemoteRoundExecutor(
            prepare_agent=lambda **kwargs: self.prepare_agent(**kwargs),
            run_local_single_model_step=lambda **kwargs: self._run_local_single_model_step(**kwargs),
            build_runtime_agent_config=lambda **kwargs: self.build_runtime_agent_config(**kwargs),
        )

    def start(self) -> None:
        """Mark the transport as running.

        Returns:
            None.
        """

        self._running = True

    def stop(self) -> None:
        """Mark the transport as stopped.

        Returns:
            None.
        """

        self._running = False

    async def build_runtime_agent_config(
        self,
        *,
        agent_config: AgentConfig,
        remote: bool = False,
        externalize_tools: bool = False,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> AgentConfig:
        """Resolve the actual runtime config used for one round.

        Args:
            agent_config: Base agent config to adapt.
            remote: Whether the round executes on a remote device.
            externalize_tools: Whether MCP tools should become direct tools.
            transport_capabilities: Optional runtime capability flags.

        Returns:
            Runtime-specific agent config.
        """

        return await self._runtime_agent_config_builder.build(
            agent_config=agent_config,
            remote=remote,
            externalize_tools=externalize_tools,
            transport_capabilities=transport_capabilities,
        )

    async def prepare_agent(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_config: AgentConfig,
        remote: bool = False,
        externalize_tools: bool = False,
        transport_capabilities: AgentTransportCapabilities | None = None,
        variables: ContextValue | None = None,
    ) -> Agent:
        """Create the exact NexAU agent instance that will execute the round.

        Args:
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Base agent config to adapt.
            remote: Whether the round executes on a remote device.
            externalize_tools: Whether MCP tools should become direct tools.
            transport_capabilities: Optional runtime capability flags.
            variables: Optional NexAU context variables.

        Returns:
            Prepared runtime agent instance.
        """

        runtime_config = await self.build_runtime_agent_config(
            agent_config=agent_config,
            remote=remote,
            externalize_tools=externalize_tools,
            transport_capabilities=transport_capabilities,
        )
        config_with_middlewares = self._recursively_apply_middlewares(runtime_config)
        config_with_middlewares = _NEXAU_RUNTIME_ADAPTER.prepare_agent_config(config_with_middlewares)
        _NEXAU_RUNTIME_ADAPTER.install_agent_creation_hook()
        ensure_session_prompt_cache_key(
            agent_config=config_with_middlewares,
            user_id=user_id,
            session_id=session_id,
            remote=remote,
            externalize_tools=externalize_tools,
            transport_capabilities=transport_capabilities,
        )
        agent = await Agent.create(
            config=config_with_middlewares,
            session_manager=self._session_manager,
            user_id=user_id,
            session_id=session_id,
            variables=variables,
        )
        return _NEXAU_RUNTIME_ADAPTER.finalize_created_agent(agent)

    async def run_agent_once(
        self,
        *,
        message: str | list[Message],
        user_id: str,
        session_id: str,
        agent: Agent | None = None,
        agent_config: AgentConfig | None = None,
        context: JsonObject | None = None,
        variables: ContextValue | None = None,
    ) -> AgentRoundResult:
        """Run one NexAU agent turn and return result plus agent.

        Args:
            message: User message or exact message list.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent: Optional existing runtime agent.
            agent_config: Optional agent config for new-agent execution.
            context: Optional runtime context.
            variables: Optional NexAU context variables.

        Returns:
            Agent result paired with the runtime agent when available.
        """

        if agent is not None:
            result = await agent.run_async(message=message, context=context, variables=variables)
            return result, agent
        result = await self.handle_request(
            message=message,
            user_id=user_id,
            session_id=session_id,
            agent_config=agent_config or self._default_agent_config,
            context=context,
            variables=variables,
        )
        return result, None

    async def run_local_round(
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
        """Execute one local in-process round using provided messages.

        Args:
            messages: Exact messages prepared by NorthCUA.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Base agent config.
            agent: Optional existing runtime agent.
            context: Optional runtime context.
            externalize_tools: Compatibility flag for direct-tool externalization.
            transport_capabilities: Optional runtime capability flags.

        Returns:
            Agent result paired with the runtime agent when available.
        """

        return await self._local_step_executor.run_round(
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            agent_config=agent_config,
            agent=agent,
            context=context,
            externalize_tools=externalize_tools,
            transport_capabilities=transport_capabilities,
        )

    async def run_remote_round(
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
        """Execute a remote round with external-tool pause and resume.

        Args:
            messages: Exact messages prepared by NorthCUA.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Base agent config.
            dispatch_external_calls: Callback that sends tool calls to a device.
            agent: Optional existing runtime agent.
            context: Optional runtime context.
            transport_capabilities: Optional runtime capability flags.

        Returns:
            Agent result paired with the runtime agent when available.
        """

        return await self._remote_round_executor.run_round(
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            agent_config=agent_config,
            dispatch_external_calls=dispatch_external_calls,
            agent=agent,
            context=context,
            transport_capabilities=transport_capabilities,
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
    ) -> AgentRoundResult:
        """Run the exact-message compatibility path.

        Args:
            messages: Exact message sequence to seed.
            user_id: NexAU user identifier.
            session_id: NexAU session identifier.
            agent_config: Runtime agent config.
            agent: Optional prepared runtime agent to reuse.
            context: Optional runtime context passed through to NexAU.
            variables: Optional NexAU context variables.

        Returns:
            Exact-message execution result.
        """

        return await self._local_step_executor._run_round_with_exact_messages(
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
        """Run the single-model-step compatibility path.

        Args:
            messages: Exact message sequence for the model call.
            agent: Prepared runtime agent.
            context: Optional runtime context merged into NexAU context.

        Returns:
            Local single-step execution result.
        """

        return await self._local_step_executor._run_local_single_model_step(
            messages=messages,
            agent=agent,
            context=context,
        )
