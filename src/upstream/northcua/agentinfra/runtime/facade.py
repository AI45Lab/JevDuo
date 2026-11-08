"""Shared session-scoped facade base classes.

Facade base classes own per-session runtime state and clone/spawn logic shared
by agent implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Self, TypeVar

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager

from northcua.agentinfra.runtime.artifacts import AgentArtifactStoreBase, ArtifactStoreFactory
from northcua.agentinfra.runtime.protocol import AgentRunRequest, AgentRunResult, AgentTransportCapabilities
from northcua.core.config import NorthCUAAppConfig

TArtifactStore = TypeVar("TArtifactStore", bound=AgentArtifactStoreBase)


class SessionScopedAgentFacade(ABC, Generic[TArtifactStore]):
    """Common session-scoped facade state and spawn logic.

    Attributes:
        app_config: NorthCUA application config.
        agent_config: NexAU agent config.
        session_manager: NexAU session manager.
        execution_transport: Runtime execution transport.
        artifact_store: Session-scoped artifact store.
        user_id: Runtime user identifier.
        transport_capabilities: Runtime transport capability flags.
    """

    def __init__(
        self,
        *,
        app_config: NorthCUAAppConfig,
        agent_config: AgentConfig,
        session_manager: SessionManager,
        execution_transport,
        artifact_store: TArtifactStore,
        user_id: str,
        transport_capabilities: AgentTransportCapabilities | None = None,
    ) -> None:
        """Initialize common facade state.

        Args:
            app_config: NorthCUA application config.
            agent_config: NexAU agent config.
            session_manager: NexAU session manager.
            execution_transport: Runtime execution transport.
            artifact_store: Session-scoped artifact store.
            user_id: Runtime user identifier.
            transport_capabilities: Optional runtime transport capability flags.
        """

        self.app_config = app_config
        self.agent_config = agent_config
        self.session_manager = session_manager
        self.execution_transport = execution_transport
        self.artifact_store = artifact_store
        self.user_id = user_id
        self.transport_capabilities = transport_capabilities or AgentTransportCapabilities()

    @property
    def session_id(self) -> str:
        """Return the session id encoded by the active artifact store.

        Returns:
            Active session identifier.
        """

        return self.artifact_store.session_id

    async def drain_background_tasks(self) -> None:
        """Drain optional facade-owned background tasks before shutdown or handoff."""

    @abstractmethod
    async def run_query(self, request: AgentRunRequest, **kwargs: object) -> AgentRunResult:
        """Run one typed query through the concrete session facade.

        Args:
            request: Agent-specific typed run request.
            **kwargs: Agent-specific callback or dispatch options.

        Returns:
            Typed terminal result for the run.
        """

    def spawn_session(self, session_id: str) -> Self:
        """Create an isolated facade for another session.

        Args:
            session_id: Session identifier for the spawned facade.

        Returns:
            Current facade when the session id matches, otherwise a sibling facade.
        """

        if session_id == self.session_id:
            return self
        artifact_store = ArtifactStoreFactory.create_session_store(
            type(self.artifact_store),
            root_dir=self.app_config.artifacts.root_dir,
            session_id=session_id,
            config=self.app_config.artifacts,
        )
        return self._spawn_with_artifact_store(artifact_store)

    @staticmethod
    def clone_agent_config(agent_config: AgentConfig) -> AgentConfig:
        """Return an isolated deep copy for another facade/session.

        Args:
            agent_config: NexAU agent config to clone.

        Returns:
            Deep-copied agent config.
        """

        return agent_config.model_copy(deep=True)

    @abstractmethod
    def _spawn_with_artifact_store(self, artifact_store: TArtifactStore) -> Self:
        """Create a sibling facade with a different session-scoped artifact store.

        Args:
            artifact_store: Artifact store for the spawned session.

        Returns:
            Spawned facade instance.
        """
