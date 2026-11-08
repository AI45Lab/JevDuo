"""Root-package desktop bridge plugin for `northcua_runtime_shared`.

This plugin keeps the standalone device worker aligned with the existing root
NorthCUA MCP registry without making `northcua_runtime_shared` import the root
runtime directly. When the root package is installed, the corresponding
entry-point loads this module and registers the bridge implementation below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from northcua.core.coordinates import CoordinatePolicy, CoordinateTarget, resolve_coordinate_policy
from northcua.core.enums import CoordinateSpace
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.desktop_native.state import DesktopNativeServerState
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua.runtime.mcp.result_adapter import normalize_tool_result
from northcua_runtime_shared import register_desktop_mcp_bridge
from northcua_runtime_shared.desktop_bridge import DesktopMcpBridge, McpServer


class RootNorthCUADesktopMcpBridge:
    """Desktop MCP bridge backed by the root NorthCUA MCP registry."""

    def ensure_registered(self) -> None:
        """Ensure the built-in desktop MCP servers are registered."""

        ensure_builtin_mcp_servers_registered()

    def build_server(self, *, server_name: str, process_name: str) -> McpServer:
        """Build one registered FastMCP desktop server.

        Args:
            server_name: Root NorthCUA MCP registry name.
            process_name: Diagnostic process label attached to the factory context.

        Returns:
            FastMCP-compatible server instance exposed through the shared bridge.
        """

        return cast(
            McpServer,
            NorthCUAMCPRegistry.build(
                server_name,
                context=MCPFactoryContext(process_name=process_name),
            ),
        )

    def is_registered(self, server_name: str) -> bool:
        """Return whether the root MCP registry knows about one server.

        Args:
            server_name: Root MCP registry name to check.

        Returns:
            True when the server is currently registered.
        """

        return bool(NorthCUAMCPRegistry.is_registered(server_name))

    def normalize_tool_result(self, tool_result: object) -> dict[str, object] | str:
        """Normalize one raw MCP tool result into JSON-friendly output.

        Args:
            tool_result: Raw FastMCP result object.

        Returns:
            JSON-style mapping or plain text result.
        """

        normalized = normalize_tool_result(tool_result)
        return normalized if isinstance(normalized, (dict, str)) else str(normalized)

    def apply_command_metadata(self, metadata: Mapping[str, object]) -> None:
        """Apply per-command desktop metadata to the root MCP runtime state.

        Args:
            metadata: Command metadata carried through the v1 device protocol.
        """

        policy = resolve_coordinate_policy(
            CoordinateTarget.DESKTOP,
            context_values={str(key): str(value) for key, value in metadata.items()},
            fallback_policy=CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
        )
        DesktopNativeServerState().set_coordinate_policy(policy)


def register() -> None:
    """Register the root-package desktop bridge with the shared bridge registry."""

    register_desktop_mcp_bridge(RootNorthCUADesktopMcpBridge())
