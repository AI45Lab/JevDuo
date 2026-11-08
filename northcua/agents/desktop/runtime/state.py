"""Desktop runtime state-store adapter for the shared AgentKit runtime."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.agentkit.runtime import AgentRuntimeCounterState, RuntimeStateStorePort
from northcua.agents.desktop.services.session_state import RoundStateStore
from northcua.agents.desktop.session import NorthCUARoundCoordinator
from northcua.runtime.artifacts.store import NorthCUAArtifactStore


@dataclass(slots=True)
class DesktopRuntimeStateStore(RuntimeStateStorePort):
    """Map desktop ``RoundState`` counters to shared runtime counters.

    The adapter is the only place where ``RoundState.session_step`` and
    ``RoundState.round_step`` are synchronized with ``AgentRuntimeCounterState``.
    Desktop step execution appends records; shared runtime counter progression
    then calls this store to persist the advanced cursor.

    Attributes:
        counters: Shared mutable counter state owned by AgentKit runtime.
        round_coordinator: Desktop queue/state facade for the active session.
        state_store: Durable RoundState store backed by the session manager.
        artifact_store: Artifact store that supplies response/evaluation paths.
    """

    counters: AgentRuntimeCounterState
    round_coordinator: NorthCUARoundCoordinator
    state_store: RoundStateStore
    artifact_store: NorthCUAArtifactStore

    async def hydrate(self) -> None:
        """Hydrate round state and copy counters into AgentKit runtime state."""

        state = await self.state_store.load(
            self.round_coordinator.state.session_id,
            response_log_path=self.artifact_store.response_log_path,
            evaluation_log_path=self.artifact_store.evaluation_log_path,
        )
        self.round_coordinator.replace_state(state)
        self._copy_round_state_to_counters()

    async def save(self) -> None:
        """Persist counters and domain state through the canonical state store."""

        self._copy_counters_to_round_state()
        await self.state_store.save(self.round_coordinator.state)

    def _copy_round_state_to_counters(self) -> None:
        state = self.round_coordinator.state
        self.counters.session_id = state.session_id
        self.counters.next_session_step = state.session_step
        self.counters.current_round_num = state.round_num
        self.counters.next_round_step = state.round_step

    def _copy_counters_to_round_state(self) -> None:
        state = self.round_coordinator.state
        state.session_step = self.counters.next_session_step
        state.round_num = self.counters.current_round_num
        state.round_step = self.counters.next_round_step


__all__ = ["DesktopRuntimeStateStore"]
