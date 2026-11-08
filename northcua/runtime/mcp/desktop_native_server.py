"""Cross-platform desktop-native MCP server facade."""

from __future__ import annotations

import platform

from fastmcp import FastMCP

from northcua.runtime.mcp.desktop_native import (
    DesktopCoordinateFrame,
    DesktopNativeBackend,
    DesktopNativeEnvironmentError,
    DesktopNativeError,
    DesktopNativePoint,
    DesktopNativeServerState,
    PyAutoGuiDesktopBackend,
    backend_error,
    create_desktop_native_backend,
    save_data_url_to_path,
)
from northcua.runtime.mcp.desktop_native.action_server import (
    create_desktop_native_action_mcp_server as _create_desktop_native_action_mcp_server,
)
from northcua.runtime.mcp.desktop_native.data_server import (
    create_desktop_native_data_mcp_server as _create_desktop_native_data_mcp_server,
)
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua_runtime_shared.desktop_surface.action_contracts import desktop_action_contracts
from northcua_runtime_shared.desktop_surface.data_contracts import desktop_data_contracts


@NorthCUAMCPRegistry.register_decorator(
    "DesktopNativeDataExecutor",
    tool_contracts=desktop_data_contracts(),
)
def create_desktop_native_data_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the cross-platform desktop-native data MCP server.

    Args:
        context: MCP factory context supplied by the registry.

    Returns:
        FastMCP server registered as ``DesktopNativeDataExecutor``.

    Raises:
        DesktopNativeEnvironmentError: If desktop-native backend creation is
            not supported for the current platform/session.
        DesktopNativeError: If backend initialization fails.
    """

    return _create_desktop_native_data_mcp_server(
        context,
        backend_factory=create_desktop_native_backend,
    )


@NorthCUAMCPRegistry.register_decorator(
    "DesktopNativeActionExecutor",
    tool_contracts=desktop_action_contracts(),
)
def create_desktop_native_action_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the cross-platform desktop-native action MCP server.

    Args:
        context: MCP factory context supplied by the registry.

    Returns:
        FastMCP server registered as ``DesktopNativeActionExecutor``.

    Raises:
        DesktopNativeEnvironmentError: If desktop-native backend creation is
            not supported for the current platform/session.
        DesktopNativeError: If backend initialization fails.
    """

    return _create_desktop_native_action_mcp_server(
        context,
        backend_factory=create_desktop_native_backend,
    )


__all__ = [
    "DesktopCoordinateFrame",
    "DesktopNativeBackend",
    "DesktopNativeEnvironmentError",
    "DesktopNativeError",
    "DesktopNativePoint",
    "DesktopNativeServerState",
    "PyAutoGuiDesktopBackend",
    "backend_error",
    "create_desktop_native_action_mcp_server",
    "create_desktop_native_backend",
    "create_desktop_native_data_mcp_server",
    "save_data_url_to_path",
]
