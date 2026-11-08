"""Session-level runtime management for AgentKit agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .cursor import AgentRuntimeCounterState, AgentRuntimeCursor, AgentRuntimePhase
from .ports import RuntimeStateStorePort, SessionResourcePort

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentSessionRuntime:
    """Own session-scope state, resources, and counter progression.

    The session runtime deliberately does not know about prompts, tools,
    controller leases, or sandbox lifecycle. It only owns the agent-session
    counters and a single optional resource adapter such as a browser client,
    page session, or desktop state store.

    Attributes:
        counters: Mutable counter state for this session.
        resource: Optional session resource adapter.
        state_store: Optional state store used to hydrate/save counters.
    """

    counters: AgentRuntimeCounterState
    resource: SessionResourcePort | None = None
    state_store: RuntimeStateStorePort | None = None
    _started: bool = False
    _closed: bool = False

    @property
    def session_id(self) -> str:
        """Return the active session id.

        Returns:
            Stable session id from the runtime counter state.
        """

        return self.counters.session_id

    async def hydrate(self) -> None:
        """Hydrate state from the optional backing store."""

        if self.state_store is not None:
            await self.state_store.hydrate()

    async def save(self) -> None:
        """Persist state to the optional backing store."""

        if self.state_store is not None:
            await self.state_store.save()

    async def start(self) -> None:
        """Start the optional resource once."""

        if self._started:
            return
        if self.resource is not None:
            await self.resource.start()
        self._started = True
        self._closed = False

    async def close(self) -> None:
        """Close the optional resource idempotently."""

        if self._closed:
            return
        try:
            if self.resource is not None:
                await self.resource.close()
        finally:
            self._started = False
            self._closed = True

    def begin_round(self) -> int:
        """Advance to a new user-query round.

        Returns:
            One-based round number.
        """

        return self.counters.begin_round()

    def reserve_step(self, phase: AgentRuntimePhase = AgentRuntimePhase.MAIN) -> AgentRuntimeCursor:
        """Reserve the next step cursor without advancing counters.

        Args:
            phase: Runtime phase being executed.

        Returns:
            Immutable cursor for the next step in ``phase``.
        """

        return self.counters.reserve_step(phase)

    def complete_step(self, cursor: AgentRuntimeCursor) -> None:
        """Advance counters after a step transaction completes.

        Args:
            cursor: Cursor returned by ``reserve_step`` for the completed
                transaction.
        """

        self.counters.complete_step(cursor)


__all__ = ["AgentSessionRuntime"]
