"""Desktop controller runner plugin for the root NorthCUA distribution."""

from __future__ import annotations

import logging

from northcua.controller.execution.runner_plugins import ControllerRunnerRegistrationContext
from northcua.controller.execution.runner_registry import RunnerRegistry
from northcua.controller.execution.sdk import ControllerSDK

logger = logging.getLogger(__name__)


def register_controller_runner(
    runner_registry: RunnerRegistry,
    sdk: ControllerSDK,
    context: ControllerRunnerRegistrationContext,
) -> None:
    """Register the desktop remote runner when local-agent dependencies exist.

    Base controller deployments intentionally do not require NexAU. In that
    mode the plugin is discoverable but disabled, allowing `controller health`
    and `controller serve` to start as control-plane-only processes.

    Args:
        runner_registry: Controller runner registry to populate.
        sdk: Controller SDK passed to concrete task runners.
        context: Controller runner configuration resolved by the server.

    Raises:
        ModuleNotFoundError: If a non-NexAU import dependency fails while
            loading the desktop runner implementation.
    """

    try:
        from northcua.agentkit.controller_runner import register_controller_runner as _register
    except ModuleNotFoundError as exc:
        missing_name = exc.name or ""
        if missing_name == "nexau" or missing_name.startswith("nexau."):
            logger.warning(
                "AgentKit controller runners disabled because NexAU is not installed; "
                "install `northcua[local]` or use `uv sync --extra local` for task execution"
            )
            return
        raise
    _register(runner_registry, sdk, context)


__all__ = ["register_controller_runner"]
