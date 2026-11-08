"""Desktop computer-use agent domain services and prompt/runtime helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.agents.desktop.application import DesktopApplication, NorthCUAApplication
    from northcua.agents.desktop.facade import NorthCUAAgentFacade

    DesktopAgentFacade = NorthCUAAgentFacade


def __getattr__(name: str) -> object:
    """Lazily expose common desktop agent classes without import-time cycles."""

    if name in {"DesktopApplication", "NorthCUAApplication"}:
        from northcua.agents.desktop.application import DesktopApplication, NorthCUAApplication

        return {"DesktopApplication": DesktopApplication, "NorthCUAApplication": NorthCUAApplication}[name]
    if name in {"DesktopAgentFacade", "NorthCUAAgentFacade"}:
        from northcua.agents.desktop.facade import NorthCUAAgentFacade

        return NorthCUAAgentFacade
    raise AttributeError(name)


__all__ = [
    "DesktopAgentFacade",
    "DesktopApplication",
    "NorthCUAAgentFacade",
    "NorthCUAApplication",
]
