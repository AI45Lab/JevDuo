"""Desktop screenshot and window-data MCP tool descriptor projections."""

from __future__ import annotations

from northcua_runtime_shared.desktop_surface.data_contracts import (
    DESKTOP_NATIVE_DATA_SERVER_NAME,
    desktop_data_contracts,
)
from northcua_runtime_shared.desktop_surface.models import DesktopToolRegistration

DATA_TOOL_REGISTRATIONS: tuple[DesktopToolRegistration, ...] = (
    *(
        DesktopToolRegistration(
            server_name=DESKTOP_NATIVE_DATA_SERVER_NAME,
            descriptor=contract.to_descriptor(),
        )
        for contract in desktop_data_contracts()
    ),
)
