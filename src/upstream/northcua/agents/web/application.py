"""Application bootstrap for generic web-agent runtime sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, cast

from nexau.archs.main_sub.config import AgentConfig

from northcua.agentinfra.runtime.application import AgentApplicationBase
from northcua.agentinfra.runtime.artifacts import ArtifactStoreFactory
from northcua.agentinfra.runtime.protocol import (
    AgentKind,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeDescriptor,
    AgentTransportCapabilities,
    WebRunRequest,
)
from northcua.core.config import NorthCUAAppConfig
from northcua.platforms import PlatformCapability, PlatformKind
from northcua.platforms.browser import BrowserPlatformRuntime

from .artifacts import WebArtifactStore
from .domain.panel_models import WebStepPanelRecord
from .facade import WebAgentFacade
from .mcp.registration import ensure_web_agent_mcp_servers_registered, register_web_agent_mcp_extension_hooks
from .server import WebAgentWebSocketServer


class WebAgentApplication(AgentApplicationBase[WebArtifactStore, WebAgentFacade, WebAgentWebSocketServer]):
    """High-level generic web-agent application wrapper for CLI and websocket serving."""

    runtime_descriptor: ClassVar[AgentRuntimeDescriptor] = AgentRuntimeDescriptor(
        kind=AgentKind.WEB,
        display_name="NorthCUA Web Agent",
        default_agent_config_selector=AgentKind.WEB,
        transport_capabilities=AgentTransportCapabilities(
            uses_in_memory_mcp=True,
            supports_stepwise_local_boundary=True,
            requires_invocation_context=True,
        ),
        platform_kind=PlatformKind.BROWSER,
        platform_capabilities=(
            PlatformCapability("snapshot"),
            PlatformCapability("action"),
            PlatformCapability("screenshot"),
        ),
    )

    def __init__(self, app_config: NorthCUAAppConfig, agent_config: AgentConfig, session_id: str) -> None:
        """Initialize web-agent runtime and register its agent-owned MCP servers."""

        register_web_agent_mcp_extension_hooks()
        ensure_web_agent_mcp_servers_registered()
        super().__init__(app_config, agent_config, session_id)

    def _build_artifact_store(self, session_id: str) -> WebArtifactStore:
        return ArtifactStoreFactory.create_session_store(
            WebArtifactStore,
            root_dir=self.app_config.artifacts.root_dir,
            session_id=session_id,
            config=self.app_config.artifacts,
        )

    def _build_facade(self) -> WebAgentFacade:
        return WebAgentFacade(
            app_config=self.app_config,
            agent_config=self.agent_config,
            session_manager=self.session_manager,
            execution_transport=self.execution_transport,
            artifact_store=self.artifact_store,
            user_id=self.app_config.execution.user_id,
            transport_capabilities=self.transport_capabilities,
        )

    def _build_platform_runtime(self) -> BrowserPlatformRuntime:
        """Create the browser platform descriptor for web-agent sessions."""

        return BrowserPlatformRuntime()

    async def run_query(self, request: AgentRunRequest, **kwargs: object) -> AgentRunResult:
        """Run one typed web-agent query through the application facade.

        Args:
            request: Typed web run request supplied by local, server, or tests.
            **kwargs: Optional render callbacks forwarded to the facade.

        Returns:
            Lightweight run result used by shared application/server callers.

        Raises:
            TypeError: If ``request`` is not a ``WebRunRequest``.
        """

        if not isinstance(request, WebRunRequest):
            raise TypeError("WebAgentApplication.run_query expects a WebRunRequest.")
        on_before_step = cast(Callable[[int], None] | None, kwargs.get("on_before_step"))
        on_step = cast(Callable[[WebStepPanelRecord], None] | None, kwargs.get("on_step"))
        return await self.facade.run_query(
            request,
            evaluate=cast(bool | None, kwargs.get("evaluate")),
            on_before_step=on_before_step if callable(on_before_step) else None,
            on_step=on_step if callable(on_step) else None,
        )

    def _build_server(self) -> WebAgentWebSocketServer:
        return WebAgentWebSocketServer(
            facade=self.facade,
            host=self.app_config.websocket.host,
            port=self.app_config.websocket.port,
        )
