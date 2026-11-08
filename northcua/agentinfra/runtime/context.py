"""Shared runtime context and orchestration primitives for internal agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from northcua.agentinfra.runtime.artifacts import AgentArtifactStoreBase
from northcua.agentinfra.runtime.protocol import AgentRunRequest, AgentRunResult
from northcua.platforms.protocol import PlatformRuntime

if TYPE_CHECKING:
    from nexau.archs.main_sub.config import AgentConfig
    from nexau.archs.session import SessionManager

    from northcua.core.config import NorthCUAAppConfig
    from northcua.transport.ws.transport import NorthCUAWebSocketTransport


@dataclass(slots=True)
class AgentRuntimeContext:
    """Shared dependencies available to concrete agent orchestration layers.

    Attributes:
        app_config: NorthCUA application config.
        agent_config: NexAU agent config.
        session_manager: NexAU session manager.
        artifact_store: Session-scoped artifact store.
        transport: Shared websocket/in-process transport.
        platform_runtime: Optional typed platform runtime.
        tracer: Optional tracer/composite tracer object.
    """

    app_config: "NorthCUAAppConfig"
    agent_config: "AgentConfig"
    session_manager: "SessionManager"
    artifact_store: AgentArtifactStoreBase
    transport: "NorthCUAWebSocketTransport"
    platform_runtime: PlatformRuntime | None = None
    tracer: object | None = None


@dataclass(frozen=True, slots=True)
class AgentArtifactPolicy:
    """Stable artifact roles for one agent implementation.

    Attributes:
        primary_trace_path: Main per-step trace path.
        runtime_log_path: Runtime event log path.
        result_path: Optional terminal result artifact path.
    """

    primary_trace_path: Path
    runtime_log_path: Path
    result_path: Path | None = None


TRequest = TypeVar("TRequest", bound=AgentRunRequest)
TResult = TypeVar("TResult", bound=AgentRunResult)


class AgentRunOrchestrator(ABC, Generic[TRequest, TResult]):
    """Base class for future generic run orchestration.

    Desktop round execution remains domain-specific in this refactor. This class
    provides the common lifecycle seam so future browser/mobile agents can share
    query orchestration without inheriting desktop semantics.
    """

    def __init__(self, context: AgentRuntimeContext) -> None:
        """Initialize the orchestrator.

        Args:
            context: Shared runtime dependencies.
        """

        self.context = context

    @abstractmethod
    async def run(self, request: TRequest) -> TResult:
        """Run one agent request.

        Args:
            request: Typed agent request.

        Returns:
            Typed agent result.
        """
