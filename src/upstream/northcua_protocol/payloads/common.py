"""Shared protocol payload helpers and aliases."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class AgentKind(str, Enum):
    """Built-in protocol agent kind constants.

    Task payloads accept arbitrary registry-driven string kinds, but keeping
    these constants preserves the public ``AgentKind.DESKTOP`` style API for
    built-in agents.
    """

    DESKTOP = "desktop"
    WEB = "web"


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Returns:
        Current UTC timestamp for protocol payload defaults.
    """

    return datetime.now(timezone.utc)


__all__ = ["AgentKind", "utc_now"]
