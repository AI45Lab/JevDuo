"""Desktop execution bridge protocol and plugin discovery helpers.

This module is the only place where the standalone data plane asks for an
implementation of the desktop MCP execution surface. It intentionally does not
import root ``northcua`` modules directly. Instead, compatible packages
register a bridge implementation through an explicit API or the
``northcua.runtime_shared.desktop_bridges`` entry-point group.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points
from threading import RLock
from typing import Protocol, cast, runtime_checkable


class McpServer(Protocol):
    """Subset of the FastMCP surface used by the device desktop adapter."""

    async def list_tools(self) -> list[object]:
        """Return tool metadata exposed by the server.

        Returns:
            Raw MCP/FastMCP tool metadata objects.
        """
        ...

    async def call_tool(self, tool_name: str, parameters: dict[str, object]) -> object:
        """Execute one tool.

        Args:
            tool_name: Registered MCP tool id.
            parameters: JSON object passed to the MCP tool.

        Returns:
            Raw MCP/FastMCP tool result.
        """
        ...


@runtime_checkable
class DesktopMcpBridge(Protocol):
    """Pluggable desktop MCP bridge used by the standalone device worker."""

    def ensure_registered(self) -> None:
        """Ensure the backing desktop servers or handlers are registered."""
        ...

    def build_server(self, *, server_name: str, process_name: str) -> McpServer:
        """Build one registered desktop execution surface.

        Args:
            server_name: Registered MCP server name.
            process_name: Process label passed to the bridge implementation.

        Returns:
            Server object exposing ``call_tool``.
        """
        ...

    def is_registered(self, server_name: str) -> bool:
        """Return whether the named desktop execution surface is registered.

        Args:
            server_name: MCP server name to query.

        Returns:
            ``True`` when the bridge can build the server.
        """
        ...

    def normalize_tool_result(self, tool_result: object) -> dict[str, object] | str:
        """Normalize a raw tool result into JSON-friendly output.

        Args:
            tool_result: Raw MCP/FastMCP result object.

        Returns:
            JSON-friendly result mapping or string.
        """
        ...

    def apply_command_metadata(self, metadata: Mapping[str, object]) -> None:
        """Apply per-command runtime metadata before executing a tool.

        Args:
            metadata: Small protocol metadata inherited from the command.
        """
        ...


_bridge_lock = RLock()
_desktop_mcp_bridge: DesktopMcpBridge | None = None
_plugins_loaded = False


def register_desktop_mcp_bridge(bridge: DesktopMcpBridge) -> None:
    """Register the process-global desktop execution bridge.

    Args:
        bridge: Bridge implementation that can build and normalize the desktop
            MCP execution surface.
    """

    global _desktop_mcp_bridge
    with _bridge_lock:
        _desktop_mcp_bridge = bridge


def clear_desktop_mcp_bridge() -> None:
    """Clear the registered desktop bridge.

    Tests use this to exercise entry-point discovery and error paths without
    relying on import order from previous cases.
    """

    global _desktop_mcp_bridge, _plugins_loaded
    with _bridge_lock:
        _desktop_mcp_bridge = None
        _plugins_loaded = False


def _load_desktop_bridge_plugins() -> None:
    """Load entry-point plugins that can register a desktop execution bridge."""

    global _plugins_loaded
    with _bridge_lock:
        if _plugins_loaded:
            return
        for entry_point in entry_points(group="northcua.runtime_shared.desktop_bridges"):
            loaded = entry_point.load()
            produced = loaded()
            if produced is not None:
                register_desktop_mcp_bridge(cast(DesktopMcpBridge, produced))
        _plugins_loaded = True


def _require_bridge() -> DesktopMcpBridge:
    """Return the registered desktop bridge or raise a configuration error."""

    if _desktop_mcp_bridge is None:
        _load_desktop_bridge_plugins()
    if _desktop_mcp_bridge is None:
        raise RuntimeError(
            "No desktop MCP bridge is registered. Install root `northcua` or another package "
            "that provides the `northcua.runtime_shared.desktop_bridges` entry point."
        )
    return _desktop_mcp_bridge


def ensure_desktop_mcp_servers_registered() -> None:
    """Ensure the active desktop bridge has registered its execution surface."""

    _require_bridge().ensure_registered()


def build_registered_mcp_server(*, server_name: str, process_name: str) -> McpServer:
    """Build one registered desktop server through the active bridge.

    Args:
        server_name: Registered MCP server name.
        process_name: Process label passed to the bridge implementation.

    Returns:
        Server object exposing the runtime-shared MCP subset.
    """

    return _require_bridge().build_server(server_name=server_name, process_name=process_name)


def is_registered_mcp_server(server_name: str) -> bool:
    """Return whether the active bridge knows about one desktop server.

    Args:
        server_name: MCP server name to query.

    Returns:
        ``True`` when the active bridge can build the server.
    """

    return _require_bridge().is_registered(server_name)


def normalize_mcp_tool_result(tool_result: object) -> dict[str, object] | str:
    """Normalize one raw desktop tool result through the active bridge.

    Args:
        tool_result: Raw MCP/FastMCP result object.

    Returns:
        JSON-friendly result mapping or string.
    """

    return _require_bridge().normalize_tool_result(tool_result)


def apply_desktop_command_metadata(metadata: Mapping[str, object]) -> None:
    """Apply one command's desktop runtime metadata through the active bridge.

    Args:
        metadata: Small protocol metadata inherited from the command.
    """

    apply_command_metadata = getattr(_require_bridge(), "apply_command_metadata", None)
    if callable(apply_command_metadata):
        apply_command_metadata(metadata)
