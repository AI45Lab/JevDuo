"""Controller control-plane services and actor boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.controller.control_plane.actors import DeviceActor, SessionActor
    from northcua.controller.control_plane.connections import ConnectionRegistry
    from northcua.controller.control_plane.leases import LeaseManager
    from northcua.controller.control_plane.registry import DeviceRegistry
    from northcua.controller.control_plane.scheduler import Scheduler


def __getattr__(name: str) -> object:
    exports = {
        "ConnectionRegistry": ("northcua.controller.control_plane.connections", "ConnectionRegistry"),
        "DeviceActor": ("northcua.controller.control_plane.actors", "DeviceActor"),
        "DeviceRegistry": ("northcua.controller.control_plane.registry", "DeviceRegistry"),
        "LeaseManager": ("northcua.controller.control_plane.leases", "LeaseManager"),
        "Scheduler": ("northcua.controller.control_plane.scheduler", "Scheduler"),
        "SessionActor": ("northcua.controller.control_plane.actors", "SessionActor"),
    }
    if name not in exports:
        raise AttributeError(name)
    module_name, attr_name = exports[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


__all__ = [
    "ConnectionRegistry",
    "DeviceActor",
    "DeviceRegistry",
    "LeaseManager",
    "Scheduler",
    "SessionActor",
]
