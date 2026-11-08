"""Shared step-budget contracts for AgentKit runtimes.

The budget layer is intentionally domain-neutral: it knows about model-turn,
round, and session counters, but it does not know how concrete agents collect
observations or execute tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from northcua.core.json_types import JsonObject
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason


class AgentBudgetScope(str, Enum):
    """Scope of a step budget enforced by an agent runtime."""

    MODEL_ITERATION = "model_iteration"
    ROUND = "round"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class AgentStepBudget:
    """Step limits shared by local, remote, and low-code agent runtimes.

    Attributes:
        max_model_iterations_per_turn: Safety cap for the internal model/tool
            loop inside one LLM turn. ``0`` means the caller should use its
            existing configured default.
        max_round_steps: Maximum persisted steps for the current user query or
            round. ``0`` means the caller's configured round default remains in
            force.
        max_session_steps: Maximum persisted steps across the whole session.
            ``0`` disables the session-level budget.
    """

    max_model_iterations_per_turn: int = 0
    max_round_steps: int = 0
    max_session_steps: int = 0

    def round_limit(self, fallback: int) -> int:
        """Return the effective round limit with a positive fallback.

        Args:
            fallback: Configured default when ``max_round_steps`` is disabled.

        Returns:
            Positive round limit.
        """

        if self.max_round_steps > 0:
            return self.max_round_steps
        return max(1, fallback)


class AgentBudgetPolicy:
    """Helpers for consistent max-step terminal decisions."""

    @staticmethod
    def max_step_decision(
        *,
        scope: AgentBudgetScope,
        limit: int,
        current: int,
        final_answer: str = "",
        summary: str | None = None,
    ) -> AgentTerminalDecision:
        """Build a standardized max-step terminal decision.

        Args:
            scope: Budget scope that was exhausted.
            limit: Configured positive limit.
            current: Counter value that reached or exceeded the limit.
            final_answer: Optional user-facing explanation.
            summary: Optional panel/telemetry summary.

        Returns:
            Shared failed terminal decision with budget metadata.
        """

        payload: JsonObject = {"scope": scope.value, "limit": limit, "current": current}
        return AgentTerminalDecision.fail(
            reason=AgentTerminalReason.MAX_STEPS,
            final_answer=final_answer or f"Task reached the {scope.value} step limit before completion.",
            summary=summary or f"The agent reached the {scope.value} step limit.",
            metadata=payload,
        )

    def terminal_decision_after_step(
        self,
        *,
        budget: AgentStepBudget,
        round_step: int,
        session_step: int,
        final_answer: str = "",
    ) -> AgentTerminalDecision | None:
        """Return a terminal decision if the completed step exhausted a budget.

        Session budget has precedence because it is the broader safety bound.

        Args:
            budget: Effective limits for the current run.
            round_step: One-based step number within the current round.
            session_step: One-based monotonic step number within the session.
            final_answer: Optional explanation to attach to the decision.

        Returns:
            Max-step terminal decision when a budget is exhausted, otherwise
            ``None``.
        """

        if budget.max_session_steps > 0 and session_step >= budget.max_session_steps:
            return self.max_step_decision(
                scope=AgentBudgetScope.SESSION,
                limit=budget.max_session_steps,
                current=session_step,
                final_answer=final_answer,
                summary="The agent reached the session step limit.",
            )
        if budget.max_round_steps > 0 and round_step >= budget.max_round_steps:
            return self.max_step_decision(
                scope=AgentBudgetScope.ROUND,
                limit=budget.max_round_steps,
                current=round_step,
                final_answer=final_answer,
                summary="The agent reached the round step limit.",
            )
        return None

    def terminal_decision_before_step(
        self,
        *,
        budget: AgentStepBudget,
        next_round_step: int,
        next_session_step: int,
        final_answer: str = "",
    ) -> AgentTerminalDecision | None:
        """Return a terminal decision when the next step would exceed a budget.

        This preflight check prevents local and remote runners from collecting a
        fresh observation or invoking the model after a cumulative session cap
        has already been reached.

        Args:
            budget: Effective limits for the current run.
            next_round_step: One-based round step that would execute next.
            next_session_step: One-based session step that would execute next.
            final_answer: Optional explanation to attach to the decision.

        Returns:
            Max-step terminal decision when running another step is disallowed,
            otherwise ``None``.
        """

        if budget.max_session_steps > 0 and next_session_step > budget.max_session_steps:
            return self.max_step_decision(
                scope=AgentBudgetScope.SESSION,
                limit=budget.max_session_steps,
                current=max(0, next_session_step - 1),
                final_answer=final_answer,
                summary="The agent already reached the session step limit.",
            )
        if budget.max_round_steps > 0 and next_round_step > budget.max_round_steps:
            return self.max_step_decision(
                scope=AgentBudgetScope.ROUND,
                limit=budget.max_round_steps,
                current=max(0, next_round_step - 1),
                final_answer=final_answer,
                summary="The agent already reached the round step limit.",
            )
        return None

    def remaining_round_iterations(
        self,
        *,
        budget: AgentStepBudget,
        current_round_step: int = 1,
        current_session_step: int = 1,
        fallback_round_steps: int = 1,
    ) -> int:
        """Return how many more step attempts can be made safely.

        Args:
            budget: Effective limits for the current run.
            current_round_step: Next one-based round step to execute.
            current_session_step: Next one-based session step to execute.
            fallback_round_steps: Positive default when no round limit is set.

        Returns:
            Non-negative number of allowed step attempts.
        """

        remaining = budget.round_limit(fallback_round_steps) - max(0, current_round_step - 1)
        if budget.max_session_steps > 0:
            remaining = min(remaining, budget.max_session_steps - max(0, current_session_step - 1))
        return max(0, remaining)


__all__ = ["AgentBudgetPolicy", "AgentBudgetScope", "AgentStepBudget"]
