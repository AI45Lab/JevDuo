"""Desktop action MCP tool contracts and descriptor projections."""

from __future__ import annotations

from northcua_runtime_shared.desktop_surface.action_contracts import (
    DESKTOP_NATIVE_ACTION_SERVER_NAME,
    desktop_action_contracts,
)
from northcua_runtime_shared.desktop_surface.models import DesktopToolRegistration

ACTION_TOOL_REGISTRATIONS: tuple[DesktopToolRegistration, ...] = tuple(
    DesktopToolRegistration(
        server_name=DESKTOP_NATIVE_ACTION_SERVER_NAME,
        descriptor=contract.to_descriptor(),
    )
    for contract in desktop_action_contracts()
)


__all__ = ["ACTION_TOOL_REGISTRATIONS"]
