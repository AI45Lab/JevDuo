"""Generic NorthCUA web-agent runtime package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .application import WebAgentApplication
    from .facade import WebAgentFacade
    from .runner import WebAgentRunner


def __getattr__(name: str) -> object:
    """Lazily expose common web-agent classes without import-time cycles."""

    if name == "WebAgentApplication":
        from .application import WebAgentApplication

        return WebAgentApplication
    if name == "WebAgentFacade":
        from .facade import WebAgentFacade

        return WebAgentFacade
    if name == "WebAgentRunner":
        from .runner import WebAgentRunner

        return WebAgentRunner
    raise AttributeError(name)


__all__ = ["WebAgentApplication", "WebAgentFacade", "WebAgentRunner"]
