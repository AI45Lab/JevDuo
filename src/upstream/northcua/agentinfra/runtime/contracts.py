"""Protocols and callback contracts shared by agent infra.

This module defines structural interfaces used to connect agent-specific
applications, facades, servers, runners, artifact stores, and CLI adapters
without requiring concrete imports at each call site.
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from northcua.agentinfra.runtime.protocol import AgentRunRequest, AgentRunResult
    from northcua.core.config import ArtifactConfig

BeforeStepCallback = Callable[[int], None]
TStepEvent = TypeVar("TStepEvent")
StepEventCallback = Callable[[TStepEvent], None]


@runtime_checkable
class AgentArtifactStore(Protocol):
    """Protocol implemented by session-scoped artifact stores.

    Attributes:
        root: Session-scoped artifact root directory.
        runtime_log_path: Path to structured runtime events.
        primary_trace_path: Agent-specific primary trace artifact path.
    """

    root: Path
    runtime_log_path: Path
    primary_trace_path: Path

    @classmethod
    def for_session(cls, root_dir: str | Path, session_id: str, config: "ArtifactConfig") -> "AgentArtifactStore":
        """Build a new session-scoped store.

        Args:
            root_dir: Parent artifact root directory.
            session_id: Session identifier used for the child directory.
            config: Artifact naming/layout config.

        Returns:
            Session-scoped artifact store.
        """

    def append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        """Append a JSONL record.

        Args:
            path: JSONL file path.
            payload: JSON-serializable record payload.
        """

    def rewrite_jsonl(self, path: Path, payloads: list[dict[str, object]]) -> None:
        """Rewrite a JSONL file with exact ordered payloads.

        Args:
            path: JSONL file path.
            payloads: Ordered JSON-serializable record payloads.
        """

    def write_json(self, path: Path, payload: object) -> None:
        """Write JSON payload.

        Args:
            path: JSON file path.
            payload: JSON-serializable payload.
        """

    def write_text(self, path: Path, text: str) -> None:
        """Write text payload.

        Args:
            path: Text file path.
            text: UTF-8 text payload.
        """


@runtime_checkable
class AgentCliAdapter(Protocol):
    """Protocol for agent-specific CLI registration and dispatch."""

    def register_commands(
        self, subparsers: argparse._SubParsersAction[argparse.ArgumentParser], *, default_config_help: str
    ) -> None:
        """Register parser entries for one agent.

        Args:
            subparsers: Shared argparse subparser collection.
            default_config_help: Help text for the default config path.
        """

    def owns_command(self, command_name: str) -> bool:
        """Return whether this adapter handles the command.

        Args:
            command_name: Parsed command name.

        Returns:
            True when this adapter owns the command.
        """

    def dispatch(self, args: argparse.Namespace) -> Awaitable[int]:
        """Dispatch the parsed command.

        Args:
            args: Parsed argparse namespace.

        Returns:
            Awaitable process-style exit code.
        """


@runtime_checkable
class SessionScopedFacade(Protocol):
    """Protocol shared by session-scoped runtime facades."""

    @property
    def session_id(self) -> str:
        """Return the current session identifier.

        Returns:
            Active session identifier.
        """

    def spawn_session(self, session_id: str) -> Self:
        """Return an isolated facade for another session.

        Args:
            session_id: Session identifier for the spawned facade.

        Returns:
            Session-scoped facade.
        """

    async def drain_background_tasks(self) -> None:
        """Drain facade-owned background tasks before shutdown or session handoff."""

    async def run_query(self, request: object, **kwargs: object) -> object:
        """Run one agent query through the session facade.

        Args:
            request: Agent-owned typed run request.
            **kwargs: Agent-specific callback or execution options.

        Returns:
            Typed terminal result for the query.
        """


@runtime_checkable
class AgentServer(Protocol):
    """Protocol for application-owned servers."""

    async def start(self) -> None:
        """Start the runtime server and any owned background services."""

    async def stop(self) -> None:
        """Stop the runtime server and release owned background services."""


@runtime_checkable
class AgentRunner(Protocol):
    """Protocol for optional application-level query runners."""

    async def drain_background_tasks(self) -> None:
        """Drain runner-owned background tasks before process shutdown or handoff."""


@runtime_checkable
class AgentApplication(Protocol):
    """Protocol for application bootstrap wrappers."""

    @classmethod
    def from_app_config(
        cls,
        app_config_path: str,
        *,
        session_id: str,
        artifact_root_dir_override: str | None = None,
    ) -> Self:
        """Construct the application from config paths.

        Args:
            app_config_path: Path to the NorthCUA app config.
            session_id: Session identifier for the application.
            artifact_root_dir_override: Optional artifact root override.

        Returns:
            Constructed application instance.
        """

    async def start(self) -> None:
        """Start runtime-owned background services for the application instance."""

    async def stop(self) -> None:
        """Stop runtime-owned background services for the application instance."""


@runtime_checkable
class BrowserStateProvider(Protocol):
    """Reserved browser-state provider abstraction for future web agents."""

    async def collect_state(self, *, session_id: str, step_index: int) -> object:
        """Collect one browser-state snapshot.

        Args:
            session_id: Session identifier.
            step_index: Step index being observed.

        Returns:
            Browser-state snapshot object.
        """
