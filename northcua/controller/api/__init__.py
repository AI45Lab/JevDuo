"""Lazy exports for controller API adapters used by CLI and tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.controller.api.server import ControllerServer


def __getattr__(name: str) -> object:
    if name != "ControllerServer":
        raise AttributeError(name)
    from northcua.controller.api.server import ControllerServer

    return ControllerServer


__all__ = ["ControllerServer"]
