"""Web process-worker runner strategy."""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING, cast

from .contracts import ProcessWorkerRunner, ProcessWorkerRunnerBuildContext

if TYPE_CHECKING:
    from northcua.agentkit.controller_runner import AgentKitRemoteRunnerConfig
    from northcua_controller_plugins.agentkit_web_runner import WebAgentKitRemoteTaskRunner


class WebProcessWorkerRunnerFactory:
    """Construct the AgentKit web runner with an IPC command transport."""

    @property
    def agent_kind(self) -> str:
        """Return the supported protocol agent kind."""

        return "web"

    async def build(self, context: ProcessWorkerRunnerBuildContext) -> ProcessWorkerRunner:
        """Import and construct the Web runner without blocking heartbeats."""

        config_type, runner_type = await asyncio.to_thread(_import_web_runner)
        request = context.request
        return cast(
            ProcessWorkerRunner,
            runner_type(
                config=config_type(
                    app_config_path=request.app_config_path,
                    artifact_root_dir_override=request.artifact_root_dir_override,
                    command_timeout_seconds=request.command_timeout_seconds,
                ),
                command_transport_factory=context.command_transport_factory(),
            ),
        )


def _import_web_runner() -> tuple[type[AgentKitRemoteRunnerConfig], type[WebAgentKitRemoteTaskRunner]]:
    config_module = importlib.import_module("northcua.agentkit.controller_runner")
    runner_module = importlib.import_module("northcua_controller_plugins.agentkit_web_runner")
    return cast(
        "tuple[type[AgentKitRemoteRunnerConfig], type[WebAgentKitRemoteTaskRunner]]",
        (config_module.AgentKitRemoteRunnerConfig, runner_module.WebAgentKitRemoteTaskRunner),
    )
