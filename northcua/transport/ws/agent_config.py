"""Runtime agent-config derivation helpers for websocket transport."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.tool import Tool

from northcua.agentinfra.runtime.protocol import AgentTransportCapabilities
from northcua.app.logging import log_runtime_event
from northcua.core.errors import MCPRegistryError
from northcua.core.json_types import JsonObject
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.in_memory_tools import build_in_memory_tool_surface
from northcua.runtime.mcp.server_config import (
    visible_contract_direct_mcp_server_configs,
    visible_external_snapshot_mcp_server_configs,
    visible_in_memory_mcp_server_configs,
    visible_nexau_mcp_server_configs,
    visible_runtime_mcp_server_configs,
)
from northcua_runtime_shared.tool_contracts.runtime_metadata import tool_contract_projection_is_current

from .message_seed import visible_runtime_mcp_servers
from .remote_tools import RemoteToolProxyFactory
from .runtime_direct_tools import derive_remote_tool_surface
from .tool_surface import route_by_tool_name_from_runtime_tools

logger = logging.getLogger(__name__)


class RuntimeAgentConfigBuilder:
    """Build the exact runtime agent config used by local and remote rounds."""

    def __init__(self, *, raw_agent_mcp_servers: list[JsonObject] | None = None) -> None:
        """Initialize the runtime config builder.

        Args:
            raw_agent_mcp_servers: Optional raw MCP server configs from app configuration.
        """

        self._raw_agent_mcp_servers = [dict(server) for server in raw_agent_mcp_servers or []]

    async def build(
        self,
        *,
        agent_config: AgentConfig,
        remote: bool = False,
        externalize_tools: bool = False,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> AgentConfig:
        """Build the agent config for a local or remote execution round.

        Args:
            agent_config: Base agent config to copy.
            remote: Whether this is a remote/device round.
            externalize_tools: Whether local MCP tools should become direct tools.
            transport_capabilities: Runtime transport feature flags.

        Returns:
            Runtime-specific AgentConfig copy.
        """

        ensure_builtin_mcp_servers_registered()
        capabilities = transport_capabilities or AgentTransportCapabilities()
        if remote or externalize_tools:
            return await self._build_remote(
                base_agent_config=agent_config,
                remote=remote,
                externalize_tools=externalize_tools,
                capabilities=capabilities,
            )

        config = agent_config.model_copy(deep=True)
        inherited_tool_count = len(config.tools)
        config.tools = _preserve_contract_projected_tools(config.tools)
        visible_raw_servers = visible_runtime_mcp_servers(
            raw_agent_mcp_servers=self._raw_agent_mcp_servers,
            agent_config=agent_config,
            runtime_visibility_filter=visible_runtime_mcp_server_configs,
        )
        config.mcp_servers = visible_nexau_mcp_server_configs(visible_raw_servers)
        direct_servers = visible_contract_direct_mcp_server_configs(visible_raw_servers)
        in_memory_servers = visible_in_memory_mcp_server_configs(visible_raw_servers)
        direct_server_names = {str(server.get("name") or "") for server in direct_servers}
        direct_tool_servers = [
            *direct_servers,
            *[
                server
                for server in in_memory_servers
                if capabilities.uses_in_memory_mcp and str(server.get("name") or "") not in direct_server_names
            ],
        ]
        if direct_tool_servers:
            local_surface = await build_in_memory_tool_surface(direct_tool_servers)
            config.tools = [*config.tools, *local_surface.tools]
        external_snapshot_servers = visible_external_snapshot_mcp_server_configs(visible_raw_servers)
        if external_snapshot_servers:
            raise MCPRegistryError(
                "Model-visible external MCP snapshots require remote/externalized tool dispatch; "
                "local runtime cannot execute imported MCP snapshots directly."
            )
        _validate_direct_tool_routes(config.tools)
        log_runtime_event(
            logger,
            "transport_runtime_agent_config_built",
            level=logging.DEBUG,
            remote=remote,
            externalize_tools=externalize_tools,
            mcp_server_count=len(config.mcp_servers),
            tool_count=len(config.tools),
            inherited_contract_tool_count=inherited_tool_count,
            in_memory_mcp_server_count=len(in_memory_servers),
            contract_direct_server_count=len(direct_servers),
            external_snapshot_server_count=len(external_snapshot_servers),
            uses_in_memory_mcp=capabilities.uses_in_memory_mcp,
        )
        return config

    async def _build_remote(
        self,
        *,
        base_agent_config: AgentConfig,
        remote: bool,
        externalize_tools: bool,
        capabilities: AgentTransportCapabilities,
    ) -> AgentConfig:
        """Build a remote config with externalized registered MCP tools.

        Args:
            base_agent_config: Base agent config to copy.
            remote: Whether remote mode is active.
            externalize_tools: Whether externalization was explicitly requested.
            capabilities: Runtime transport feature flags.

        Returns:
            Runtime-specific remote AgentConfig copy.
        """

        config = base_agent_config.model_copy(deep=True)
        inherited_tool_count = len(config.tools)
        preserved_tools = _preserve_contract_projected_tools(config.tools)
        config.tools = preserved_tools
        visible_raw_servers = visible_runtime_mcp_servers(
            raw_agent_mcp_servers=self._raw_agent_mcp_servers,
            agent_config=base_agent_config,
            runtime_visibility_filter=visible_runtime_mcp_server_configs,
        )
        config.mcp_servers = visible_nexau_mcp_server_configs(visible_raw_servers)
        external_snapshot_servers = visible_external_snapshot_mcp_server_configs(visible_raw_servers)
        if external_snapshot_servers and not externalize_tools:
            raise MCPRegistryError(
                "Model-visible external MCP snapshots require explicit externalized proxy dispatch; "
                "the standard remote/device command path cannot execute imported MCP snapshots directly."
            )
        proxy_factory = RemoteToolProxyFactory()
        remote_surface = await derive_remote_tool_surface(
            config,
            raw_server_configs=visible_raw_servers,
            implementation_factory=lambda server_name, tool_name: proxy_factory.build(
                tool_name=tool_name,
                server_name=server_name,
            ),
        )
        config.mcp_servers = []
        config.tools = [*preserved_tools, *remote_surface.tools]
        _validate_direct_tool_routes(config.tools)
        log_runtime_event(
            logger,
            "transport_runtime_agent_config_built",
            level=logging.DEBUG,
            remote=remote,
            externalize_tools=externalize_tools,
            mcp_server_count=len(config.mcp_servers),
            tool_count=len(config.tools),
            inherited_contract_tool_count=inherited_tool_count,
            uses_in_memory_mcp=capabilities.uses_in_memory_mcp,
        )
        log_runtime_event(
            logger,
            "transport_remote_agent_config_derived",
            level=logging.DEBUG,
            external_tool_count=len(remote_surface.tools),
        )
        return config


def _validate_direct_tool_routes(tools: Iterable[object]) -> None:
    """Fail fast when direct provider tools would route name-only calls ambiguously."""

    route_by_tool_name_from_runtime_tools(tools)


def _preserve_contract_projected_tools(tools: Iterable[object]) -> list[Tool]:
    """Preserve internal contract projections while rejecting raw direct tools."""

    preserved: list[Tool] = []
    for tool in tools:
        if not isinstance(tool, Tool) or not tool_contract_projection_is_current(tool):
            raise MCPRegistryError(
                "Base AgentConfig.tools may only contain ToolContract-projected tools. "
                "Raw or stale model-visible direct tools are disabled; define owned tools with "
                "ToolContract or import external_mcp_snapshots."
            )
        preserved.append(tool)
    return preserved
