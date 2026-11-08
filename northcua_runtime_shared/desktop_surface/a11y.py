"""Desktop accessibility MCP tool descriptor projections."""

from __future__ import annotations

from northcua_runtime_shared.desktop_surface.a11y_contracts import (
    DESKTOP_NATIVE_A11Y_SERVER_NAME,
    desktop_a11y_contracts,
)
from northcua_runtime_shared.desktop_surface.models import DesktopToolRegistration

A11Y_TOOL_REGISTRATIONS: tuple[DesktopToolRegistration, ...] = (
    *(
        DesktopToolRegistration(
            server_name=DESKTOP_NATIVE_A11Y_SERVER_NAME,
            descriptor=contract.to_descriptor(),
        )
        for contract in desktop_a11y_contracts()
    ),
)
