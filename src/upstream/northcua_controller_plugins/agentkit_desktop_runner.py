"""AgentKit remote runner factory for the bundled desktop agent."""

from __future__ import annotations

from northcua.agentkit.controller_runner import AgentKitRemoteRunnerConfig, AgentKitRemoteTaskRunnerBase
from northcua.agentkit.spec import AgentSpec
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.sdk import ControllerSDK
from northcua_protocol.payloads import TaskFinished


class DesktopAgentKitRemoteTaskRunner(AgentKitRemoteTaskRunnerBase):
    """AgentKit registration wrapper for the existing desktop remote runner."""

    async def run(self, context: ControllerExecutionContext) -> TaskFinished:
        """Run a desktop task through the existing controller plugin.

        Args:
            context: Controller task context with request, lease, and session ids.

        Returns:
            Terminal task payload produced by the desktop remote runner.
        """

        from northcua_controller_plugins.desktop_runner import DesktopRemoteRunnerConfig, DesktopRemoteTaskRunner

        runner = DesktopRemoteTaskRunner(
            config=DesktopRemoteRunnerConfig(
                app_config_path=self.config.app_config_path,
                artifact_root_dir_override=self.config.artifact_root_dir_override,
                max_steps_per_task=self.config.max_steps_per_task,
                command_timeout_seconds=self.config.command_timeout_seconds,
            ),
            sdk=self.sdk,
        )
        return await runner.run(context)


def build_desktop_remote_task_runner(
    *,
    spec: AgentSpec,
    config: AgentKitRemoteRunnerConfig,
    sdk: ControllerSDK,
) -> DesktopAgentKitRemoteTaskRunner:
    """Build the desktop remote runner for an AgentKit spec.

    Args:
        spec: Agent declaration that requested this runner.
        config: Controller-side AgentKit runner configuration.
        sdk: Controller SDK used by the underlying desktop runner.

    Returns:
        Desktop remote task runner instance.
    """

    del spec
    return DesktopAgentKitRemoteTaskRunner(config=config, sdk=sdk)
