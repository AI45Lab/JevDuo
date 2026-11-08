"""Controller control-plane primitives for remote CUA harness infrastructure."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.controller.api.server import ControllerServer
    from northcua.controller.config import ControllerConfig
    from northcua.controller.control_plane.connections import ConnectionRegistry
    from northcua.controller.control_plane.leases import LeaseManager
    from northcua.controller.control_plane.registry import DeviceRegistry
    from northcua.controller.control_plane.scheduler import Scheduler
    from northcua.controller.execution.command_bus import CommandBus
    from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
    from northcua.controller.execution.sdk import ControllerSDK
    from northcua.controller.execution.tasks import TaskService
    from northcua.controller.storage.persistence import SQLiteControllerStore
    from northcua.controller.storage.queue import TaskQueue


def __getattr__(name: str) -> object:
    """Lazily expose controller entrypoints without package import cycles."""

    exports = {
        "CommandBus": ("northcua.controller.execution.command_bus", "CommandBus"),
        "ConnectionRegistry": ("northcua.controller.control_plane.connections", "ConnectionRegistry"),
        "ControllerConfig": ("northcua.controller.config", "ControllerConfig"),
        "ControllerSDK": ("northcua.controller.execution.sdk", "ControllerSDK"),
        "ControllerServer": ("northcua.controller.api.server", "ControllerServer"),
        "DeviceRegistry": ("northcua.controller.control_plane.registry", "DeviceRegistry"),
        "LeaseManager": ("northcua.controller.control_plane.leases", "LeaseManager"),
        "RemoteCommandDispatcher": ("northcua.controller.execution.remote_dispatcher", "RemoteCommandDispatcher"),
        "SQLiteControllerStore": ("northcua.controller.storage.persistence", "SQLiteControllerStore"),
        "Scheduler": ("northcua.controller.control_plane.scheduler", "Scheduler"),
        "TaskQueue": ("northcua.controller.storage.queue", "TaskQueue"),
        "TaskService": ("northcua.controller.execution.tasks", "TaskService"),
    }
    if name not in exports:
        raise AttributeError(name)
    module_name, attr_name = exports[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


__all__ = [
    "CommandBus",
    "ConnectionRegistry",
    "ControllerConfig",
    "ControllerSDK",
    "ControllerServer",
    "DeviceRegistry",
    "LeaseManager",
    "RemoteCommandDispatcher",
    "SQLiteControllerStore",
    "Scheduler",
    "TaskQueue",
    "TaskService",
]
