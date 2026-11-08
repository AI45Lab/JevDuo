"""Action-server factory for desktop-native MCP tools."""

from __future__ import annotations

from typing import Callable

from fastmcp import FastMCP

from northcua.core.coordinates import CoordinatePolicy, CoordinateTarget, resolve_coordinate_policy
from northcua.core.enums import CoordinateSpace
from northcua.runtime.mcp.registry import MCPFactoryContext

from .action_tools import _register_desktop_native_action_tools
from .backend import DesktopNativeBackend
from .state import DesktopNativeServerState


def create_desktop_native_action_mcp_server(
    context: MCPFactoryContext,
    *,
    backend_factory: Callable[[], DesktopNativeBackend],
) -> FastMCP:
    """Create the desktop-native action MCP server.

    Args:
        context: MCP factory context with coordinate policy overrides.
        backend_factory: Factory for the desktop-native backend.

    MCP schema:
        The server name remains ``UFO Desktop Native Action MCP Server`` and
        exposes the existing action tools: ``screenshot``, ``click``,
        ``double_click``, ``drag``, ``move``, ``scroll``, ``type``,
        ``keypress``, and ``wait``. Action results are dictionaries with
        ``text``, ``screenshot_path``, and ``screenshot_format`` keys.
        Coordinates are interpreted through the resolved desktop coordinate
        policy, defaulting to image pixels.

    Platform behavior:
        The desktop-native backend validates platform support while the server
        is built. Tool-level fallbacks are fail-open only for non-critical
        screenshot/control paths that can safely return a desktop screenshot or
        an empty list; input actions surface backend failures as MCP
        ``ToolError`` instances.

    Returns:
        FastMCP server exposing desktop action tools.

    Raises:
        DesktopNativeEnvironmentError: If the backend factory rejects the
            current desktop platform or session.
        DesktopNativeError: If backend initialization fails before the server
            can expose tools.
    """

    state = DesktopNativeServerState()
    coordinate_policy = resolve_coordinate_policy(
        CoordinateTarget.DESKTOP,
        context_values=context.extra,
        fallback_policy=CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
    )
    state.set_coordinate_policy(coordinate_policy)
    backend = state.get_backend(backend_factory=backend_factory)
    action_mcp = FastMCP("UFO Desktop Native Action MCP Server")
    _register_desktop_native_action_tools(action_mcp, state, backend)
    return action_mcp


__all__ = ["create_desktop_native_action_mcp_server"]
