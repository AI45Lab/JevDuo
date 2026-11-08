"""Lazy exports for controller execution-plane services and runner plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.controller.execution.command_bus import CommandBus, command_idempotency_key
    from northcua.controller.execution.executor_protocol import ControllerExecutionContext, ControllerTaskExecutor
    from northcua.controller.execution.processes.executor import ProcessTaskExecutorService
    from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
    from northcua.controller.execution.runner_plugins import (
        ControllerRunnerRegistrationContext,
        populate_runner_registry,
    )
    from northcua.controller.execution.runner_registry import RunnerRegistry, TaskExecutorService
    from northcua.controller.execution.sdk import ControllerSDK
    from northcua.controller.execution.tasks import TaskService


def __getattr__(name: str) -> object:
    exports = {
        "CommandBus": ("northcua.controller.execution.command_bus", "CommandBus"),
        "ControllerExecutionContext": (
            "northcua.controller.execution.executor_protocol",
            "ControllerExecutionContext",
        ),
        "ControllerSDK": ("northcua.controller.execution.sdk", "ControllerSDK"),
        "ControllerTaskExecutor": ("northcua.controller.execution.executor_protocol", "ControllerTaskExecutor"),
        "ControllerRunnerRegistrationContext": (
            "northcua.controller.execution.runner_plugins",
            "ControllerRunnerRegistrationContext",
        ),
        "RemoteCommandDispatcher": ("northcua.controller.execution.remote_dispatcher", "RemoteCommandDispatcher"),
        "RunnerRegistry": ("northcua.controller.execution.runner_registry", "RunnerRegistry"),
        "TaskService": ("northcua.controller.execution.tasks", "TaskService"),
        "TaskExecutorService": ("northcua.controller.execution.runner_registry", "TaskExecutorService"),
        "ProcessTaskExecutorService": (
            "northcua.controller.execution.processes.executor",
            "ProcessTaskExecutorService",
        ),
        "command_idempotency_key": ("northcua.controller.execution.command_bus", "command_idempotency_key"),
        "populate_runner_registry": ("northcua.controller.execution.runner_plugins", "populate_runner_registry"),
    }
    if name not in exports:
        raise AttributeError(name)
    module_name, attr_name = exports[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


__all__ = [
    "CommandBus",
    "ControllerExecutionContext",
    "ControllerRunnerRegistrationContext",
    "ControllerSDK",
    "ControllerTaskExecutor",
    "RemoteCommandDispatcher",
    "RunnerRegistry",
    "TaskService",
    "TaskExecutorService",
    "ProcessTaskExecutorService",
    "command_idempotency_key",
    "populate_runner_registry",
]
