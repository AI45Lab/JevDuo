"""Abstract adapter contracts for evaluator evidence providers."""

from __future__ import annotations

from typing import Protocol

from ..protocol import RoundEvaluationBundle


class RoundEvidenceAdapter(Protocol):
    """Adapter that converts an agent-specific round into a generic bundle."""

    def build_round_bundle(
        self,
        *,
        round_num: int,
        round_request: str,
        step_logs: list[object],
    ) -> RoundEvaluationBundle:
        """Build a generic AgentKit evaluator bundle.

        Args:
            round_num: One-based round number to evaluate.
            round_request: User request for the round.
            step_logs: Domain-specific persisted step records.

        Returns:
            Generic round evaluation bundle.
        """
        ...


__all__ = ["RoundEvidenceAdapter"]
