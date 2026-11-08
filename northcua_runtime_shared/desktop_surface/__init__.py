"""Public accessors for the authoritative desktop MCP tool surface.

The source-of-truth contracts live in small server-scoped modules. Descriptor
accessors are compatibility projections for the controller/device protocol.
"""

from __future__ import annotations

from northcua_protocol.capabilities import ToolDescriptor
from northcua_runtime_shared.desktop_surface.a11y_contracts import desktop_a11y_contracts
from northcua_runtime_shared.desktop_surface.a11y import A11Y_TOOL_REGISTRATIONS
from northcua_runtime_shared.desktop_surface.action_contracts import desktop_action_contracts
from northcua_runtime_shared.desktop_surface.actions import ACTION_TOOL_REGISTRATIONS
from northcua_runtime_shared.desktop_surface.data_contracts import desktop_data_contracts
from northcua_runtime_shared.desktop_surface.data import DATA_TOOL_REGISTRATIONS
from northcua_runtime_shared.desktop_surface.models import DesktopToolRegistration, TextTargetKind
from northcua_runtime_shared.tool_contracts import ToolContract

_DESKTOP_TOOL_REGISTRATIONS: tuple[DesktopToolRegistration, ...] = (
    *A11Y_TOOL_REGISTRATIONS,
    *DATA_TOOL_REGISTRATIONS,
    *ACTION_TOOL_REGISTRATIONS,
)


def desktop_tool_registrations() -> list[DesktopToolRegistration]:
    """Return compatibility desktop tool registrations.

    Returns:
        Copy of desktop MCP registration projections in stable server/tool order.
    """

    return list(_DESKTOP_TOOL_REGISTRATIONS)


def desktop_tool_contracts() -> list[ToolContract]:
    """Return source-of-truth desktop tool contracts in stable order."""

    return [
        *desktop_a11y_contracts(),
        *desktop_data_contracts(),
        *desktop_action_contracts(),
    ]


def desktop_wire_tool_descriptors() -> list[ToolDescriptor]:
    """Return all desktop wire descriptor projections in stable server/tool order.

    Returns:
        Deep-copied descriptors safe for callers to serialize or validate.
    """

    return [registration.descriptor.model_copy(deep=True) for registration in _DESKTOP_TOOL_REGISTRATIONS]


def desktop_server_names() -> tuple[str, ...]:
    """Return distinct desktop MCP server names in stable order.

    Returns:
        Tuple of MCP server names without duplicates.
    """

    return tuple(dict.fromkeys(registration.server_name for registration in _DESKTOP_TOOL_REGISTRATIONS))


def desktop_tool_server_name(tool_id: str) -> str | None:
    """Return the MCP server name responsible for one desktop tool id.

    Args:
        tool_id: Protocol-facing desktop tool id.

    Returns:
        MCP server name responsible for the tool, or ``None`` if unknown.
    """

    for registration in _DESKTOP_TOOL_REGISTRATIONS:
        if registration.descriptor.tool_id == tool_id:
            return registration.server_name
    return None


__all__ = [
    "DesktopToolRegistration",
    "TextTargetKind",
    "desktop_tool_contracts",
    "desktop_server_names",
    "desktop_wire_tool_descriptors",
    "desktop_tool_registrations",
    "desktop_tool_server_name",
]
