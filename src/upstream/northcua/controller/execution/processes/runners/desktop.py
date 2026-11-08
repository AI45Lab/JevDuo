"""Desktop process-worker runner strategy."""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING, cast

from .contracts import ProcessWorkerRunner, ProcessWorkerRunnerBuildContext

if TYPE_CHECKING:
    from northcua_controller_plugins.desktop_runner import DesktopRemoteRunnerConfig, DesktopRemoteTaskRunner


class DesktopProcessWorkerRunnerFactory:
    """Construct the existing desktop runner with an IPC command transport."""

    @property
    def agent_kind(self) -> str:
        """Return the supported protocol agent kind."""

        return "desktop"

    async def build(self, context: ProcessWorkerRunnerBuildContext) -> ProcessWorkerRunner:
        """Import and construct the desktop runner without blocking heartbeats."""

        config_type, runner_type = await asyncio.to_thread(_import_desktop_runner)
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
                startup_phase_sink=context.phase_reporter.set_phase_from_thread,
                runtime_progress_sink_enabled=True,
            ),
        )


def _import_desktop_runner() -> tuple[type[DesktopRemoteRunnerConfig], type[DesktopRemoteTaskRunner]]:
    module = importlib.import_module("northcua_controller_plugins.desktop_runner")
    return cast(
        "tuple[type[DesktopRemoteRunnerConfig], type[DesktopRemoteTaskRunner]]",
        (module.DesktopRemoteRunnerConfig, module.DesktopRemoteTaskRunner),
    )
