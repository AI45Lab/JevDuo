"""Persistence adapter between NorthCUA round state and NexAU SessionManager."""

from __future__ import annotations

import logging
from pathlib import Path

from nexau.archs.main_sub.agent_context import GlobalStorage
from nexau.archs.session import SessionManager
from pydantic import BaseModel, Field

from northcua.app.logging import log_runtime_event
from northcua.core.models import (
    CommandExecutionResultRecord,
    DesktopStateSnapshot,
    RoundInput,
    RoundState,
    StepLogRecord,
)

logger = logging.getLogger(__name__)


class PersistedRoundState(BaseModel):
    """Typed SessionModel.context payload for NorthCUA round state.

    Attributes:
        session_id: Source session id.
        session_step: Next global session step number.
        round_num: Current round number.
        round_step: Next step number within the current round.
        latest_request: Most recent user request.
        latest_snapshot: Latest captured desktop state.
        pending_round_inputs: Queued round inputs waiting for execution.
        step_logs: Persisted step logs for the session.
        pending_command_results: Command results awaiting consumption by call id.
        pending_resume_results: Resume results queued for round execution.
    """

    session_id: str
    session_step: int = 1
    round_num: int = 0
    round_step: int = 1
    latest_request: str = ""
    latest_snapshot: DesktopStateSnapshot = Field(default_factory=DesktopStateSnapshot)
    pending_round_inputs: list[RoundInput] = Field(default_factory=list)
    step_logs: list[StepLogRecord] = Field(default_factory=list)
    pending_command_results: dict[str, CommandExecutionResultRecord] = Field(default_factory=dict)
    pending_resume_results: list[CommandExecutionResultRecord] = Field(default_factory=list)


class RoundStateStore:
    """Canonicalize NorthCUA session state through NexAU SessionManager."""

    def __init__(self, session_manager: SessionManager, *, user_id: str) -> None:
        """Initialize SessionManager-backed round state storage.

        Args:
            session_manager: NexAU session manager used for persistence.
            user_id: User id used for session lookups and updates.
        """

        self.session_manager = session_manager
        self.user_id = user_id

    async def load(
        self, session_id: str, *, response_log_path: Path | None = None, evaluation_log_path: Path | None = None
    ) -> RoundState:
        """Load round state from SessionManager, or create an empty default state.

        Args:
            session_id: Session id to load.
            response_log_path: Optional response JSONL path to attach to state.
            evaluation_log_path: Optional evaluation JSONL path to attach to state.

        Returns:
            Hydrated round state.
        """

        session = await self.session_manager.get_session(user_id=self.user_id, session_id=session_id)
        if session is None or not session.context:
            state = RoundState(session_id=session_id)
            log_runtime_event(
                logger,
                "round_state_loaded_default",
                level=logging.DEBUG,
                session_id=session_id,
            )
        else:
            persisted = PersistedRoundState.model_validate(session.context)
            state = RoundState(
                session_id=persisted.session_id,
                session_step=persisted.session_step,
                round_num=persisted.round_num,
                round_step=persisted.round_step,
                latest_request=persisted.latest_request,
                latest_snapshot=persisted.latest_snapshot,
                pending_round_inputs=list(persisted.pending_round_inputs),
                step_logs=list(persisted.step_logs),
                pending_command_results=dict(persisted.pending_command_results),
                pending_resume_results=list(persisted.pending_resume_results),
            )
            log_runtime_event(
                logger,
                "round_state_loaded",
                level=logging.DEBUG,
                session_id=session_id,
                session_step=state.session_step,
                round_num=state.round_num,
                round_step=state.round_step,
                step_log_count=len(state.step_logs),
                pending_round_inputs=len(state.pending_round_inputs),
                pending_command_results=len(state.pending_command_results),
                pending_resume_results=len(state.pending_resume_results),
            )
        state.response_log_path = response_log_path
        state.evaluation_log_path = evaluation_log_path
        return state

    async def save(self, state: RoundState, *, storage: GlobalStorage | None = None) -> None:
        """Persist the full round state back to SessionManager.

        Args:
            state: Round state to persist.
            storage: Optional global storage to preserve on the session.
        """

        state.release_retained_payloads()
        session = await self.session_manager.get_session(user_id=self.user_id, session_id=state.session_id)
        current_storage = storage or (session.storage if session is not None else GlobalStorage())
        payload = PersistedRoundState(
            session_id=state.session_id,
            session_step=state.session_step,
            round_num=state.round_num,
            round_step=state.round_step,
            latest_request=state.latest_request,
            latest_snapshot=state.latest_snapshot,
            pending_round_inputs=list(state.pending_round_inputs),
            step_logs=list(state.step_logs),
            pending_command_results=dict(state.pending_command_results),
            pending_resume_results=list(state.pending_resume_results),
        )
        await self.session_manager.update_session_state(
            user_id=self.user_id,
            session_id=state.session_id,
            context=payload.model_dump(mode="json"),
            storage=current_storage,
        )
        log_runtime_event(
            logger,
            "round_state_saved",
            level=logging.DEBUG,
            session_id=state.session_id,
            session_step=state.session_step,
            round_num=state.round_num,
            round_step=state.round_step,
            step_log_count=len(state.step_logs),
            pending_round_inputs=len(state.pending_round_inputs),
            pending_command_results=len(state.pending_command_results),
            pending_resume_results=len(state.pending_resume_results),
        )

    async def append_round_input(
        self,
        session_id: str,
        round_input: RoundInput,
        *,
        response_log_path: Path | None = None,
        evaluation_log_path: Path | None = None,
    ) -> RoundState:
        """Load, enqueue, and persist a new round input.

        Args:
            session_id: Session id to update.
            round_input: Round input to enqueue.
            response_log_path: Optional response JSONL path to attach to state.
            evaluation_log_path: Optional evaluation JSONL path to attach to state.

        Returns:
            Updated round state.
        """

        state = await self.load(
            session_id,
            response_log_path=response_log_path,
            evaluation_log_path=evaluation_log_path,
        )
        state.enqueue(round_input)
        log_runtime_event(
            logger,
            "round_state_enqueue_round_input",
            session_id=session_id,
            query=round_input.query,
            queue_length=len(state.pending_round_inputs),
        )
        await self.save(state)
        return state

    async def record_command_results(
        self,
        session_id: str,
        results: list[CommandExecutionResultRecord],
        *,
        response_log_path: Path | None = None,
        evaluation_log_path: Path | None = None,
    ) -> RoundState:
        """Load, merge command results, and persist them.

        Args:
            session_id: Session id to update.
            results: Command results to merge by call id.
            response_log_path: Optional response JSONL path to attach to state.
            evaluation_log_path: Optional evaluation JSONL path to attach to state.

        Returns:
            Updated round state.
        """

        state = await self.load(
            session_id,
            response_log_path=response_log_path,
            evaluation_log_path=evaluation_log_path,
        )
        for result in results:
            state.pending_command_results[result.call_id] = result
        log_runtime_event(
            logger,
            "round_state_recorded_command_results",
            session_id=session_id,
            result_count=len(results),
            pending_command_results=len(state.pending_command_results),
            call_ids=[result.call_id for result in results],
        )
        await self.save(state)
        return state

    async def consume_command_results(
        self,
        session_id: str,
        call_ids: list[str],
        *,
        response_log_path: Path | None = None,
        evaluation_log_path: Path | None = None,
    ) -> list[CommandExecutionResultRecord] | None:
        """Atomically consume command results from SessionManager-backed state.

        Args:
            session_id: Session id to update.
            call_ids: Command call ids to consume.
            response_log_path: Optional response JSONL path to attach to state.
            evaluation_log_path: Optional evaluation JSONL path to attach to state.

        Returns:
            Consumed command results in requested order, or ``None`` when any
            requested result is still pending.
        """

        state = await self.load(
            session_id,
            response_log_path=response_log_path,
            evaluation_log_path=evaluation_log_path,
        )
        if not all(call_id in state.pending_command_results for call_id in call_ids):
            log_runtime_event(
                logger,
                "round_state_command_results_pending",
                level=logging.DEBUG,
                session_id=session_id,
                requested_call_ids=call_ids,
                available_call_ids=sorted(state.pending_command_results.keys()),
            )
            return None
        results = [state.pending_command_results.pop(call_id) for call_id in call_ids]
        log_runtime_event(
            logger,
            "round_state_consumed_command_results",
            session_id=session_id,
            consumed_call_ids=call_ids,
            remaining_pending_command_results=len(state.pending_command_results),
        )
        await self.save(state)
        return results
