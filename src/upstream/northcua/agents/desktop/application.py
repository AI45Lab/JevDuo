"""Application bootstrap and dependency assembly for desktop NorthCUA."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Self, cast

from northcua.agentinfra.runtime.application import AgentApplicationBase
from northcua.agentinfra.runtime.artifacts import ArtifactStoreFactory
from northcua.agentinfra.runtime.protocol import (
    AgentKind,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeDescriptor,
    AgentTransportCapabilities,
    DesktopRunRequest,
)
from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.agents.desktop.observation import ObservationCoordinator
from northcua.agents.desktop.server import NorthCUAWebSocketServer
from northcua.core.config_loader import NorthCUAConfigLoader
from northcua.core.enums import RuntimeMode
from northcua.core.models import StepLogRecord
from northcua.platforms import PlatformCapability, PlatformKind
from northcua.platforms.desktop import DesktopPlatformRuntime
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.desktop import DesktopObservationService
from northcua.runtime.mcp.device_executor import NorthCUADeviceCommandExecutor


class DesktopApplication(
    AgentApplicationBase[
        NorthCUAArtifactStore,
        NorthCUAAgentFacade,
        NorthCUAWebSocketServer,
    ]
):
    """High-level desktop application wrapper used by tests and CLIs.

    Attributes:
        runtime_descriptor: Static descriptor for the desktop runtime.
    """

    runtime_descriptor: ClassVar[AgentRuntimeDescriptor] = AgentRuntimeDescriptor(
        kind=AgentKind.DESKTOP,
        display_name="NorthCUA Desktop",
        default_agent_config_selector=AgentKind.DESKTOP,
        transport_capabilities=AgentTransportCapabilities(
            supports_stepwise_local_boundary=True,
            supports_remote_external_dispatch=True,
        ),
        platform_kind=PlatformKind.DESKTOP,
        platform_capabilities=(
            PlatformCapability("screenshot"),
            PlatformCapability("action"),
            PlatformCapability("accessibility"),
        ),
    )

    def _build_artifact_store(self, session_id: str) -> NorthCUAArtifactStore:
        return ArtifactStoreFactory.create_session_store(
            NorthCUAArtifactStore,
            root_dir=self.app_config.artifacts.root_dir,
            session_id=session_id,
            config=self.app_config.artifacts,
        )

    @classmethod
    def from_app_config(
        cls,
        app_config_path: str,
        *,
        session_id: str,
        artifact_root_dir_override: str | None = None,
        runtime_mode_override: RuntimeMode | None = None,
        bootstrap_phase_callback: Callable[[str], None] | None = None,
    ) -> Self:
        """Load desktop config and optionally force runtime mode before bootstrap.

        Controller-owned remote desktop runs must enter application bootstrap
        with ``websocket.runtime_mode`` already set to remote. Setting the mode
        after construction is too late because runtime services may otherwise
        instantiate local desktop-native MCP executors on the controller host.

        Args:
            app_config_path: Path to the NorthCUA app config file.
            session_id: Session identifier used for artifacts and state.
            artifact_root_dir_override: Optional artifact root for this run.
            runtime_mode_override: Optional runtime mode applied before
                runtime services are assembled.
            bootstrap_phase_callback: Optional synchronous phase hook used by
                process workers to keep heartbeat diagnostics current during
                blocking startup.

        Returns:
            Bootstrapped desktop application using the loaded configs.
        """

        if bootstrap_phase_callback is not None:
            bootstrap_phase_callback("loading_app_config")
        bootstrap = NorthCUAConfigLoader.from_app_config(
            app_config_path,
            agent_kind=cls.runtime_descriptor.default_agent_config_selector,
        )
        app_config = bootstrap.app.model_copy(deep=True)
        if artifact_root_dir_override:
            app_config.artifacts.root_dir = artifact_root_dir_override
        if runtime_mode_override is not None:
            app_config.websocket = app_config.websocket.model_copy(update={"runtime_mode": runtime_mode_override})
        return cls(
            app_config, bootstrap.agent, session_id=session_id, bootstrap_phase_callback=bootstrap_phase_callback
        )

    def _build_facade(self) -> NorthCUAAgentFacade:
        facade = NorthCUAAgentFacade(
            app_config=self.app_config,
            agent_config=self.agent_config,
            artifact_store=self.artifact_store,
            session_manager=self.session_manager,
            execution_transport=self.execution_transport,
            user_id=self.app_config.execution.user_id,
            transport_capabilities=self.transport_capabilities,
            platform_runtime=(
                self.platform_runtime if isinstance(self.platform_runtime, DesktopPlatformRuntime) else None
            ),
        )
        facade.rounds.state.response_log_path = self.artifact_store.response_log_path
        facade.rounds.state.evaluation_log_path = self.artifact_store.evaluation_log_path
        return facade

    def _build_platform_runtime(self) -> DesktopPlatformRuntime:
        """Create the desktop platform runtime used by the round flow."""

        if self.app_config.websocket.runtime_mode == RuntimeMode.REMOTE:
            return DesktopPlatformRuntime(observation_service=None, action_executor=None)

        observation_coordinator = ObservationCoordinator.for_local(
            app_config=self.app_config,
            agent_config=self.agent_config,
            artifact_store=self.artifact_store,
        )
        observation_service = DesktopObservationService(coordinator=observation_coordinator)
        return DesktopPlatformRuntime(
            observation_service=observation_service,
            action_executor=NorthCUADeviceCommandExecutor(),
        )

    async def run_query(self, request: AgentRunRequest, **kwargs: object) -> AgentRunResult:
        """Run one typed desktop query through the application facade.

        Args:
            request: Desktop run request.
            **kwargs: Callback or runtime options forwarded to the facade.

        Returns:
            Desktop run result.

        Raises:
            TypeError: If request is not a DesktopRunRequest.
        """

        if not isinstance(request, DesktopRunRequest):
            raise TypeError("DesktopApplication.run_query expects a DesktopRunRequest.")
        on_before_step = cast(Callable[[int], None] | None, kwargs.get("on_before_step"))
        on_step = cast(Callable[[StepLogRecord], None] | None, kwargs.get("on_step"))
        return await self.facade.run_query(
            request,
            on_before_step=on_before_step if callable(on_before_step) else None,
            on_step=on_step if callable(on_step) else None,
        )

    def _build_server(self) -> NorthCUAWebSocketServer:
        return NorthCUAWebSocketServer(
            facade=self.facade,
            execution_transport=self.execution_transport,
            runtime_mode=self.app_config.websocket.runtime_mode,
            command_result_timeout_seconds=self.app_config.websocket.command_result_timeout_seconds,
            command_poll_interval_seconds=self.app_config.websocket.command_poll_interval_seconds,
            host=self.app_config.websocket.host,
            port=self.app_config.websocket.port,
        )


NorthCUAApplication = DesktopApplication
