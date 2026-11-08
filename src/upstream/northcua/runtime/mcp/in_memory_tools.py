"""Bridge model-visible in-memory MCP servers into direct NexAU tools."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from nexau.archs.tool import Tool

from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua.runtime.mcp.result_adapter import normalize_tool_result
from northcua.runtime.mcp.types import MCPToolResultPayload
from northcua.transport.ws.runtime_direct_tools import compile_runtime_direct_tool_surface
from northcua_runtime_shared.tool_contracts import ToolSurfaceMode

logger = logging.getLogger(__name__)


class InMemoryToolImplementation(Protocol):
    """Callable shape used by direct NexAU wrappers around FastMCP tools."""

    async def __call__(self, **params: object) -> MCPToolResultPayload:
        """Invoke a bridged MCP tool with JSON-style keyword parameters."""
        ...


@dataclass(frozen=True, slots=True)
class InMemoryDerivedToolSurface:
    """Direct-tool surface synthesized from in-memory MCP servers.

    Attributes:
        tools: NexAU direct tools generated from registered FastMCP servers.
        externalized_server_names: Server names converted into direct tools.
    """

    tools: list[Tool]
    externalized_server_names: list[str]


async def build_in_memory_tool_surface(
    raw_server_configs: Sequence[Mapping[str, object]],
) -> InMemoryDerivedToolSurface:
    """Build direct NexAU tools for model-visible in-memory MCP servers.

    Args:
        raw_server_configs: Raw MCP server configs from an agent definition.

    Returns:
        Direct-tool surface and the server names that were externalized.
    """

    surface = compile_runtime_direct_tool_surface(
        [dict(raw_server) for raw_server in raw_server_configs],
        mode=ToolSurfaceMode.LOCAL_IN_MEMORY,
        implementation_factory=_build_tool_implementation,
    )
    return InMemoryDerivedToolSurface(
        tools=surface.tools,
        externalized_server_names=surface.externalized_server_names,
    )


def _build_tool_implementation(
    server_name: str,
    tool_name: str,
) -> InMemoryToolImplementation:
    """Create one async direct-tool implementation that proxies into FastMCP."""

    async def _implementation(**params: object) -> MCPToolResultPayload:
        """Invoke the FastMCP tool and normalize its direct-tool result.

        Args:
            **params: Tool parameters passed by NexAU.

        Returns:
            JSON-serializable normalized tool result.
        """

        try:
            validated_params = dict(params)
            server = NorthCUAMCPRegistry.build(
                server_name,
                context=MCPFactoryContext(process_name=f"northcua-in-memory:{server_name}"),
            )
            result = await server.call_tool(tool_name, validated_params)
            normalized = normalize_tool_result(result)
            if isinstance(normalized, dict):
                if normalized.get("error") and normalized.get("status") != "error":
                    normalized = {"status": "error", **normalized}
                return normalized
            return {"result": normalized}
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("In-memory MCP tool '%s' on server '%s' failed", tool_name, server_name)
            return {
                "status": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "tool_name": tool_name,
                "server_name": server_name,
            }

    return _implementation
