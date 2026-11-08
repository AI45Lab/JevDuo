"""Round/session queue coordination for NorthCUA."""

from __future__ import annotations

from northcua.core.models import RoundInput, RoundState


class NorthCUARoundCoordinator:
    """Own queued round inputs and the live desktop round state."""

    def __init__(self, session_id: str) -> None:
        self.state = RoundState(session_id=session_id)

    def replace_state(self, state: RoundState) -> None:
        """Replace the live in-memory state with a persisted snapshot.

        Args:
            state: Previously persisted round state to resume from.
        """

        self.state = state

    def enqueue_initial_request(self, query: str, metadata: dict[str, object] | None = None) -> None:
        """Seed the first round input.

        Args:
            query: User request that starts the session.
            metadata: Optional round metadata persisted with the queued input.
        """

        self.state.enqueue(RoundInput(query=query, metadata=metadata))

    def enqueue_followup(self, query: str, metadata: dict[str, object] | None = None) -> None:
        """Queue a follow-up round.

        Args:
            query: User request for the next round.
            metadata: Optional round metadata persisted with the queued input.
        """

        self.state.enqueue(RoundInput(query=query, metadata=metadata))

    def next_round(self) -> RoundInput | None:
        """Advance to the next round input.

        Returns:
            The next queued round input, a synthetic resume input, or ``None``
            when the session has no pending work.
        """

        if self.state.pending_resume_results:
            return RoundInput(
                query=self.state.latest_request,
                metadata={"resume": True},
            )

        next_input = self.state.pop_next()
        if next_input is not None:
            self.state.latest_request = next_input.query
            self.state.round_step = 1
            self.state.round_num += 1
        return next_input
