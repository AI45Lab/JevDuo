"""Public round-execution service entrypoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import RoundExecutionService


def __getattr__(name: str):
    """Lazily expose heavy round-execution service composition."""

    if name == "RoundExecutionService":
        from .service import RoundExecutionService

        return RoundExecutionService
    raise AttributeError(name)


__all__ = ["RoundExecutionService"]
