"""Built-in MCP server for cross-platform desktop accessibility capture."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp import FastMCP

from northcua.core.desktop_runtime_environment import desktop_backend_from_environment
from northcua.core.enums import AccessibilityScope
from northcua.runtime.accessibility import AccessibilityCaptureRequest, AccessibilityCollector
from northcua.runtime.accessibility.osworld import OSWorldAccessibilityBackend
from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua_runtime_shared.desktop_surface.a11y_contracts import (
    DesktopA11yCapabilitiesParams,
    DesktopAccessibilityTreeParams,
    desktop_a11y_contracts,
)

logger = logging.getLogger("northcua.runtime.mcp.accessibility")

A11Y_CAPABILITIES_TOOL_NAME = "get_accessibility_capabilities"
A11Y_CAPTURE_TOOL_NAME = "capture_accessibility_tree"


@NorthCUAMCPRegistry.register_decorator(
    "DesktopNativeA11yExecutor",
    tool_contracts=desktop_a11y_contracts(),
)
def create_desktop_native_a11y_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the cross-platform accessibility data MCP server.

    Args:
        context: MCP factory context with optional platform metadata.

    Returns:
        FastMCP server exposing accessibility capture tools.
    """

    if desktop_backend_from_environment() == "osworld":
        collector = AccessibilityCollector(backend=OSWorldAccessibilityBackend(), platform_name="osworld")
    else:
        collector = AccessibilityCollector(platform_name=context.extra.get("platform_name"))
    a11y_mcp = FastMCP("UFO Desktop Native Accessibility MCP Server")
    register_contract_handlers(a11y_mcp, desktop_a11y_contracts(), _desktop_a11y_handler_map(collector))
    return a11y_mcp


def _desktop_a11y_handler_map(collector: AccessibilityCollector) -> dict[str, Callable[..., object]]:
    return {
        A11Y_CAPABILITIES_TOOL_NAME: lambda params: _get_accessibility_capabilities(params, collector),
        A11Y_CAPTURE_TOOL_NAME: lambda params: _capture_accessibility_tree(params, collector),
    }


def _get_accessibility_capabilities(
    params: DesktopA11yCapabilitiesParams,
    collector: AccessibilityCollector,
) -> dict[str, object]:
    del params
    logger.info("DesktopNativeA11yExecutor reporting accessibility capabilities")
    return collector.capabilities().model_dump(mode="json")


def _capture_accessibility_tree(
    params: DesktopAccessibilityTreeParams,
    collector: AccessibilityCollector,
) -> dict[str, object]:
    request = AccessibilityCaptureRequest(
        scope=AccessibilityScope(str(params.scope)),
        max_depth=params.max_depth,
        max_children_per_node=params.max_children_per_node,
        max_nodes=params.max_nodes,
        include_raw_payload=params.include_raw_payload,
        capture_timeout_seconds=params.capture_timeout_seconds,
    )
    logger.info(
        "DesktopNativeA11yExecutor capturing accessibility tree (scope=%s, max_depth=%s, max_nodes=%s)",
        request.scope.value,
        request.max_depth,
        request.max_nodes,
    )
    snapshot = collector.capture(request)
    return snapshot.model_dump(mode="json")
