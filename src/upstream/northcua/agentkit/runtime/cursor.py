"""Shared runtime cursor for agent session, round, and step lifecycles."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from northcua.core.json_types import JsonObject


class AgentRuntimePhase(str, Enum):
    """Runtime phase whose step counters may advance independently."""

    MAIN = "main"
    EVALUATOR = "evaluator"
    SETUP = "setup"
    CLEANUP = "cleanup"

    @property
    def counts_toward_session(self) -> bool:
        """Return whether this phase consumes the main agent session budget.

        Returns:
            ``True`` only for user-task main-agent steps. Evaluator, setup, and
            cleanup steps are intentionally tracked separately so post-processing
            cannot consume or pollute the main agent's session step budget.
        """

        return self is AgentRuntimePhase.MAIN


@dataclass(frozen=True, slots=True)
class AgentRuntimeCursor:
    """Immutable pointer to one runtime position.

    Attributes:
        session_id: Stable agent session id.
        round_num: One-based round/query number.
        round_step: One-based step within the active round.
        session_step: One-based main-agent step within the session.
        phase: Runtime phase for this cursor.
        phase_step: One-based step within ``phase``.
    """

    session_id: str
    round_num: int = 1
    round_step: int = 1
    session_step: int = 1
    phase: AgentRuntimePhase = AgentRuntimePhase.MAIN
    phase_step: int = 1

    def for_phase(self, phase: AgentRuntimePhase, *, phase_step: int = 1) -> "AgentRuntimeCursor":
        """Return a cursor projected into another runtime phase.

        Args:
            phase: Target runtime phase.
            phase_step: One-based phase-local step.

        Returns:
            Cursor carrying the same session/round coordinates with a new phase
            and phase-local step.
        """

        return replace(self, phase=phase, phase_step=max(1, phase_step))

    def to_json(self) -> JsonObject:
        """Serialize the cursor for traces, telemetry, and protocol payloads.

        Returns:
            JSON-compatible cursor payload.
        """

        return {
            "session_id": self.session_id,
            "round_num": self.round_num,
            "round_step": self.round_step,
            "session_step": self.session_step,
            "phase": self.phase.value,
            "phase_step": self.phase_step,
        }


@dataclass(slots=True)
class AgentRuntimeCounterState:
    """Mutable counter state for one agent session.

    Attributes:
        session_id: Stable agent session id.
        next_session_step: Next one-based main-agent step.
        current_round_num: Current one-based round/query number. ``0`` means no
            round has started yet.
        next_round_step: Next one-based step in the current round.
        phase_steps: Next one-based step per non-main runtime phase.
    """

    session_id: str
    next_session_step: int = 1
    current_round_num: int = 0
    next_round_step: int = 1
    phase_steps: dict[AgentRuntimePhase, int] = field(default_factory=dict)

    def begin_round(self) -> int:
        """Advance to a new round and reset round-local counters.

        Returns:
            One-based round number that just became active.
        """

        self.current_round_num += 1
        self.next_round_step = 1
        self.phase_steps.clear()
        return self.current_round_num

    def reserve_step(self, phase: AgentRuntimePhase = AgentRuntimePhase.MAIN) -> AgentRuntimeCursor:
        """Return the cursor for the next step without advancing counters.

        Args:
            phase: Runtime phase being executed.

        Returns:
            Immutable cursor for the next step in ``phase``.
        """

        round_num = max(1, self.current_round_num)
        phase_step = self.next_round_step if phase is AgentRuntimePhase.MAIN else self.phase_steps.get(phase, 1)
        return AgentRuntimeCursor(
            session_id=self.session_id,
            round_num=round_num,
            round_step=self.next_round_step,
            session_step=self.next_session_step,
            phase=phase,
            phase_step=phase_step,
        )

    def complete_step(self, cursor: AgentRuntimeCursor) -> None:
        """Advance counters after a step has been persisted or intentionally skipped.

        Args:
            cursor: Cursor returned by ``reserve_step`` for the completed step.
        """

        if cursor.phase.counts_toward_session:
            self.next_session_step = max(self.next_session_step, cursor.session_step + 1)
            self.next_round_step = max(self.next_round_step, cursor.round_step + 1)
            return
        self.phase_steps[cursor.phase] = max(
            self.phase_steps.get(cursor.phase, 1),
            cursor.phase_step + 1,
        )

    def to_json(self) -> JsonObject:
        """Serialize mutable counter state for diagnostics.

        Returns:
            JSON-compatible counter payload.
        """

        return {
            "session_id": self.session_id,
            "next_session_step": self.next_session_step,
            "current_round_num": self.current_round_num,
            "next_round_step": self.next_round_step,
            "phase_steps": {phase.value: step for phase, step in self.phase_steps.items()},
        }


__all__ = ["AgentRuntimeCounterState", "AgentRuntimeCursor", "AgentRuntimePhase"]
