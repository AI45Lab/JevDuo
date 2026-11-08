"""Desktop tool profiles that bind descriptors to execution servers."""

from __future__ import annotations

from dataclasses import dataclass

from northcua_protocol.capabilities import ToolDescriptor
from northcua_runtime_shared.desktop_surface import desktop_tool_contracts
from northcua_runtime_shared.tool_contracts import ToolContract, ToolProfile, ToolSurfaceResolver, ToolVisibility


@dataclass(frozen=True, slots=True)
class DesktopToolProfile:
    """A contract profile plus the server route for each tool.

    The controller validates commands against contract projections while the device worker
    dispatches the same tool ids through ``server_by_tool_id``. Keeping both in
    one object prevents tool schemas from drifting away from runtime
    execution.

    Attributes:
        contracts: Tool contracts visible to the selected desktop runtime profile.
        server_by_tool_id: Mapping from tool id to MCP server route name.
    """

    contracts: tuple[ToolContract, ...]
    server_by_tool_id: dict[str, str]

    @property
    def tools(self) -> tuple[ToolDescriptor, ...]:
        """Return wire descriptor projections for protocol advertisement."""

        return tuple(contract.to_descriptor() for contract in self.contracts)


def default_desktop_tool_profile() -> DesktopToolProfile:
    """Build the canonical desktop tool profile from static registrations.

    Returns:
        Profile containing every statically registered desktop tool.
    """

    return _profile_from_contracts(
        "agent.desktop.default.runtime",
        [
            contract
            for contract in desktop_tool_contracts()
            if contract.visibility in {ToolVisibility.MODEL_VISIBLE, ToolVisibility.RUNTIME_ONLY}
        ],
    )


def _profile_from_contracts(profile_name: str, contracts: list[ToolContract]) -> DesktopToolProfile:
    surface = ToolSurfaceResolver(tuple(contracts)).resolve(ToolProfile(name=profile_name))
    return DesktopToolProfile(
        contracts=surface.contracts,
        server_by_tool_id=dict(surface.route_by_tool_id),
    )
