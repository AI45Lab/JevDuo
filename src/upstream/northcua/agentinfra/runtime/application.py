"""Shared application bootstrap scaffold for internal agents.

The base application wires config loading, coordinate environment setup,
session storage, transport, tracing, artifact stores, facades, runners, and
servers while leaving agent-specific construction to subclasses.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar, Generic, Self, TypeVar

from northcua.agentinfra.runtime.protocol import AgentKind, AgentRuntimeDescriptor
from northcua.core.nexau_support import rethrow_missing_nexau, suppress_nexau_optional_dependency_noise

suppress_nexau_optional_dependency_noise()
try:
    from nexau.archs.main_sub.config import AgentConfig
    from nexau.archs.session import SessionManager
    from nexau.archs.session.orm import DatabaseEngine, InMemoryDatabaseEngine
    from nexau.archs.tracer.adapters.in_memory import InMemoryTracer
    from nexau.archs.tracer.composite import CompositeTracer
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by runtime startup
    rethrow_missing_nexau(exc)

from northcua.agentinfra.runtime.artifacts import AgentArtifactStoreBase, ArtifactStoreFactory
from northcua.agentinfra.runtime.context import AgentRuntimeContext
from northcua.agentinfra.runtime.protocol import AgentRunRequest, AgentRunResult
from northcua.app.logging import configure_runtime_logging
from northcua.core.config import NorthCUAAppConfig
from northcua.core.config_loader import NorthCUAConfigLoader
from northcua.core.coordinates import (
    CoordinateEnvironmentSnapshot,
    apply_app_coordinate_environment,
    restore_coordinate_environment,
)
from northcua.core.desktop_runtime_environment import (
    DesktopRuntimeEnvironmentSnapshot,
    apply_app_desktop_runtime_environment,
    restore_desktop_runtime_environment,
)
from northcua.platforms.protocol import PlatformRuntime
from northcua.platforms.registry import PlatformRuntimeRegistry, default_platform_runtime_registry
from northcua.transport.ws.config import NorthCUAWebSocketTransportConfig
from northcua.transport.ws.transport import NorthCUAWebSocketTransport

TArtifactStore = TypeVar("TArtifactStore", bound=AgentArtifactStoreBase)
TFacade = TypeVar("TFacade")
TServer = TypeVar("TServer")

logger = logging.getLogger(__name__)


class AgentApplicationBase(ABC, Generic[TArtifactStore, TFacade, TServer]):
    """Template-method application bootstrap for session-scoped agent runtimes.

    Attributes:
        runtime_descriptor: Static runtime descriptor supplied by subclasses.
        app_config: NorthCUA application config.
        session_id: Active session identifier.
        agent_config: Isolated NexAU agent config copy.
        engine: Session database engine.
        session_manager: NexAU session manager.
        execution_transport: Runtime execution transport.
        transport_capabilities: Runtime transport capability flags.
        platform_runtime_registry: Registry used to resolve platform runtimes.
        platform_runtime: Optional platform runtime instance for the agent.
        runtime_context: Shared context envelope for future generic orchestrators.
        artifact_store: Session-scoped artifact store.
        facade: Session-scoped runtime facade.
        task_runner: Optional application-level task runner.
        server: Runtime server wrapper.
    """

    runtime_descriptor: ClassVar[AgentRuntimeDescriptor]

    def __init__(
        self,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        session_id: str,
        *,
        bootstrap_phase_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize shared runtime infrastructure.

        Args:
            app_config: NorthCUA application config.
            agent_config: NexAU agent config.
            session_id: Active session identifier.
            bootstrap_phase_callback: Optional synchronous phase hook for
                process workers to keep liveness diagnostics fresh while this
                bootstrap path performs filesystem-heavy initialization.
        """

        self._bootstrap_phase_callback = bootstrap_phase_callback
        self.app_config = app_config
        self.session_id = session_id
        self.agent_config = agent_config.model_copy(deep=True)
        self._emit_bootstrap_phase("applying_environment")
        self._coordinate_environment: CoordinateEnvironmentSnapshot = apply_app_coordinate_environment(app_config)
        self._desktop_runtime_environment: DesktopRuntimeEnvironmentSnapshot = apply_app_desktop_runtime_environment(
            app_config
        )
        self._emit_bootstrap_phase("building_session_engine")
        self.engine = self._build_engine()
        self.session_manager = self._build_session_manager()
        self._emit_bootstrap_phase("configuring_tracer")
        self._configure_agent_tracer()
        self._emit_bootstrap_phase("building_execution_transport")
        self.execution_transport = self._build_execution_transport()
        self.transport_capabilities = self.runtime_descriptor.transport_capabilities
        self._emit_bootstrap_phase("building_artifact_store")
        self.artifact_store = self._build_artifact_store(session_id)
        configure_runtime_logging(self.artifact_store)
        self.platform_runtime_registry = self._build_platform_runtime_registry()
        self._emit_bootstrap_phase("building_platform_runtime")
        self.platform_runtime = self._build_platform_runtime()
        self.runtime_context = self._build_runtime_context()
        self._emit_bootstrap_phase("building_facade")
        self.facade = self._build_facade()
        self.task_runner = self._build_task_runner()
        self.server = self._build_server()
        self._emit_bootstrap_phase("application_bootstrapped")

    @property
    def websocket_server(self) -> TServer:
        """Backward-friendly alias used by existing callers and tests.

        Returns:
            Runtime server wrapper.
        """

        return self.server

    @classmethod
    def from_app_config(
        cls,
        app_config_path: str,
        *,
        session_id: str,
        artifact_root_dir_override: str | None = None,
    ) -> Self:
        """Load app/agent config and construct an application instance.

        Args:
            app_config_path: Path to the NorthCUA app config.
            session_id: Session identifier for the application.
            artifact_root_dir_override: Optional artifact root override.

        Returns:
            Constructed application instance.
        """

        bootstrap = NorthCUAConfigLoader.from_app_config(
            app_config_path,
            agent_kind=cls.runtime_descriptor.default_agent_config_selector,
        )
        app_config = bootstrap.app.model_copy(deep=True)
        if artifact_root_dir_override:
            app_config.artifacts.root_dir = artifact_root_dir_override
        return cls(app_config, bootstrap.agent, session_id=session_id)

    def _emit_bootstrap_phase(self, phase: str) -> None:
        if self._bootstrap_phase_callback is None:
            return
        try:
            self._bootstrap_phase_callback(phase)
        except Exception:
            logger.debug("application_bootstrap_phase_callback_failed", extra={"phase": phase}, exc_info=True)

    async def start(self) -> None:
        """Start runtime-owned background services when the concrete server exposes a start hook."""

        start = getattr(self.server, "start", None)
        if callable(start):
            await start()

    async def stop(self) -> None:
        """Stop runtime-owned background services and restore coordinate env."""

        try:
            stop = getattr(self.server, "stop", None)
            if callable(stop):
                await stop()
            else:
                await self.facade.drain_background_tasks()
        finally:
            restore_desktop_runtime_environment(self._desktop_runtime_environment)
            restore_coordinate_environment(self._coordinate_environment)

    def _build_engine(self) -> DatabaseEngine:
        """Create the configured NexAU session database engine.

        Returns:
            Session database engine.

        Raises:
            NotImplementedError: If a non-memory backend is configured.
        """

        if self.app_config.execution.session_backend != "memory":
            raise NotImplementedError(f"Unsupported session backend: {self.app_config.execution.session_backend}")
        return InMemoryDatabaseEngine.get_shared_instance()

    def _build_session_manager(self) -> SessionManager:
        """Create the shared NexAU session manager.

        Returns:
            Configured session manager.
        """

        return SessionManager(
            engine=self.engine,
            heartbeat_interval=self.app_config.websocket.heartbeat_interval_seconds,
        )

    def _build_execution_transport(self) -> NorthCUAWebSocketTransport:
        """Create the websocket-backed execution transport.

        Returns:
            Configured NorthCUA websocket transport.
        """

        return NorthCUAWebSocketTransport(
            engine=self.engine,
            session_manager=self.session_manager,
            config=NorthCUAWebSocketTransportConfig(runtime_mode=self.app_config.websocket.runtime_mode),
            default_agent_config=self.agent_config,
            raw_agent_mcp_servers=list(self.app_config.agent_mcp_servers),
            heartbeat_interval=self.app_config.websocket.heartbeat_interval_seconds,
        )

    def _build_platform_runtime_registry(self) -> PlatformRuntimeRegistry:
        """Create the application-local platform runtime registry.

        Returns:
            Platform runtime registry.
        """

        return default_platform_runtime_registry()

    def _build_platform_runtime(self) -> PlatformRuntime | None:
        """Create an optional typed platform runtime.

        Concrete agents can override this hook when they are ready to route
        observation/action flow through the platform contract. Returning ``None``
        preserves the current domain-specific execution path.

        Returns:
            Platform runtime instance, or None.
        """

        return None

    def _build_runtime_context(self) -> AgentRuntimeContext:
        """Build the shared runtime context for generic orchestration layers.

        Returns:
            Runtime context envelope.
        """

        return AgentRuntimeContext(
            app_config=self.app_config,
            agent_config=self.agent_config,
            session_manager=self.session_manager,
            artifact_store=self.artifact_store,
            transport=self.execution_transport,
            platform_runtime=self.platform_runtime,
            tracer=self.agent_config.resolved_tracer,
        )

    def _configure_agent_tracer(self) -> None:
        """Attach configured in-memory or Langfuse tracers to the agent config.

        Raises:
            RuntimeError: If Langfuse tracing is enabled but unavailable.
        """

        tracers = list(self.agent_config.tracers or [])
        if self.agent_config.resolved_tracer is not None and not tracers:
            tracers = [self.agent_config.resolved_tracer]

        if self.app_config.execution.enable_in_memory_tracer and not any(
            isinstance(tracer, InMemoryTracer) for tracer in tracers
        ):
            tracers.append(InMemoryTracer())

        if self.app_config.execution.enable_langfuse_tracer:
            try:
                from nexau.archs.tracer.adapters.langfuse import LangfuseTracer
            except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
                raise RuntimeError("Langfuse tracing is enabled but nexau LangfuseTracer is unavailable.") from exc
            if not any(isinstance(tracer, LangfuseTracer) for tracer in tracers):
                experiment_id = self.app_config.execution.experiment_id.strip()
                langfuse_tags = [
                    *list(self.app_config.execution.langfuse_tags),
                    *list(self.app_config.execution.experiment_tags),
                    self.runtime_descriptor.kind.value,
                ]
                if experiment_id:
                    langfuse_tags.append(f"experiment:{experiment_id}")
                langfuse_metadata = {
                    "agent_kind": self.runtime_descriptor.kind.value,
                    **dict(self.app_config.execution.langfuse_metadata),
                }
                if experiment_id:
                    langfuse_metadata["experiment_id"] = experiment_id
                langfuse_public_key = (
                    self.app_config.execution.langfuse_public_key.get_secret_value()
                    if self.app_config.execution.langfuse_public_key is not None
                    else None
                )
                langfuse_secret_key = (
                    self.app_config.execution.langfuse_secret_key.get_secret_value()
                    if self.app_config.execution.langfuse_secret_key is not None
                    else None
                )
                tracers.append(
                    LangfuseTracer(
                        public_key=langfuse_public_key,
                        secret_key=langfuse_secret_key,
                        host=self.app_config.execution.langfuse_host,
                        session_id=self.session_id,
                        tags=langfuse_tags,
                        metadata=langfuse_metadata,
                    )
                )

        self.agent_config.tracers = tracers
        if len(tracers) == 1:
            self.agent_config.resolved_tracer = tracers[0]
        elif len(tracers) > 1:
            self.agent_config.resolved_tracer = CompositeTracer(tracers)
        else:
            self.agent_config.resolved_tracer = None

    @abstractmethod
    def _build_artifact_store(self, session_id: str) -> TArtifactStore:
        """Create the session-scoped artifact store for this application.

        Args:
            session_id: Session identifier.

        Returns:
            Session-scoped artifact store.
        """

    @abstractmethod
    def _build_facade(self) -> TFacade:
        """Create the session-scoped facade for this application.

        Returns:
            Session-scoped facade.
        """

    def _build_task_runner(self):
        """Create an optional application-level task runner.

        Returns:
            Optional task runner; None when the runtime does not define one.
        """

        return None

    @abstractmethod
    async def run_query(self, request: AgentRunRequest, **kwargs: object) -> AgentRunResult:
        """Run one typed query through the concrete agent runtime.

        Args:
            request: Typed agent run request.
            **kwargs: Agent-specific callback or execution options.

        Returns:
            Typed terminal run result.
        """

    @abstractmethod
    def _build_server(self) -> TServer:
        """Create the remote server wrapper for this application.

        Returns:
            Runtime server wrapper.
        """
