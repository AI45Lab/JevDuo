"""Data-server factory for desktop-native MCP tools."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from northcua.core.coordinates import CoordinatePolicy, CoordinateTarget, resolve_coordinate_policy
from northcua.core.enums import CoordinateSpace
from northcua.runtime.mcp.registry import MCPFactoryContext

from .backend import DesktopNativeBackend
from .data_tools import _register_desktop_native_data_tools
from .state import DesktopNativeServerState


def create_desktop_native_data_mcp_server(
    context: MCPFactoryContext,
    *,
    backend_factory: Callable[[], DesktopNativeBackend],
) -> FastMCP:
    """Create the desktop-native data MCP server.

    Args:
        context: MCP factory context with coordinate policy overrides.
        backend_factory: Factory for the desktop-native backend.

    MCP schema:
        The server name remains ``UFO Desktop Native Data MCP Server`` and
        exposes observation tools that return either PNG data URLs or JSON-style
        metadata dictionaries/lists. The screenshot tools return strings shaped
        as ``data:image/png;base64,...`` rather than artifact paths.

    Platform behavior:
        Active-window screenshot capture is fail-open: when the platform cannot
        capture the active window but can still capture the desktop, the tool
        returns a primary-desktop screenshot and refreshes the coordinate frame.
        Control extraction is also fail-open in the cross-platform v1 backend
        and may return an empty list on unsupported platforms. Backend
        exceptions that prevent producing the documented shape are surfaced as
        MCP ``ToolError`` instances.

    Returns:
        FastMCP server exposing desktop observation tools.

    Raises:
        DesktopNativeEnvironmentError: If the backend factory rejects the
            current desktop platform or session.
        DesktopNativeError: If backend initialization fails before the server
            can expose tools.
    """

    state = DesktopNativeServerState()
    state.set_coordinate_policy(
        resolve_coordinate_policy(
            CoordinateTarget.DESKTOP,
            context_values=context.extra,
            fallback_policy=CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
        )
    )
    backend = state.get_backend(backend_factory=backend_factory)
    data_mcp = FastMCP("UFO Desktop Native Data MCP Server")
    _register_desktop_native_data_tools(data_mcp, state, backend)
    return data_mcp


__all__ = ["create_desktop_native_data_mcp_server"]
