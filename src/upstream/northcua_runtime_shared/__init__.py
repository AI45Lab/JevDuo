"""Shared runtime registries and protocols for standalone NorthCUA components."""

from northcua_runtime_shared.desktop_bridge import (
    apply_desktop_command_metadata,
    build_registered_mcp_server,
    clear_desktop_mcp_bridge,
    ensure_desktop_mcp_servers_registered,
    is_registered_mcp_server,
    normalize_mcp_tool_result,
    register_desktop_mcp_bridge,
)
from northcua_runtime_shared.desktop_surface import (
    DesktopToolRegistration,
    desktop_server_names,
    desktop_tool_contracts,
    desktop_tool_registrations,
    desktop_tool_server_name,
    desktop_wire_tool_descriptors,
)
from northcua_runtime_shared.registries import (
    ArtifactHandlerRegistry,
    CapabilityDescriptor,
    CapabilityDescriptorRegistry,
    RuntimeAdapterRegistry,
    ToolSurfaceRegistry,
)
from northcua_runtime_shared.runtime import RuntimeAdapter, RuntimeHealth
from northcua_runtime_shared.validation import validate_command_against_tool

__all__ = [
    "ArtifactHandlerRegistry",
    "apply_desktop_command_metadata",
    "CapabilityDescriptor",
    "CapabilityDescriptorRegistry",
    "clear_desktop_mcp_bridge",
    "DesktopToolRegistration",
    "RuntimeAdapter",
    "RuntimeAdapterRegistry",
    "RuntimeHealth",
    "ToolSurfaceRegistry",
    "build_registered_mcp_server",
    "desktop_server_names",
    "desktop_tool_contracts",
    "desktop_tool_registrations",
    "desktop_tool_server_name",
    "desktop_wire_tool_descriptors",
    "ensure_desktop_mcp_servers_registered",
    "is_registered_mcp_server",
    "normalize_mcp_tool_result",
    "register_desktop_mcp_bridge",
    "validate_command_against_tool",
]
