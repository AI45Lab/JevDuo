"""Shared tool surface kernel."""

from typing import TYPE_CHECKING

from .surface import ToolSurface, ToolSurfaceBuilder, ToolSurfaceService

if TYPE_CHECKING:
    from .projection import contract_to_nexau_tool

__all__ = ["ToolSurface", "ToolSurfaceBuilder", "ToolSurfaceService", "contract_to_nexau_tool"]


def __getattr__(name: str) -> object:
    if name == "contract_to_nexau_tool":
        from .projection import contract_to_nexau_tool

        return contract_to_nexau_tool
    raise AttributeError(name)
