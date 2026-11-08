"""Session-model and state hydration helpers for round execution."""

from __future__ import annotations

import logging

from northcua.app.logging import log_runtime_event

logger = logging.getLogger(__name__)


class RoundStateHydrator:
    """Load persisted round/session state and ensure NexAU session tables exist."""

    def __init__(
        self,
        *,
        session_manager,
        state_store,
        round_coordinator,
        artifact_store,
        user_id: str,
    ) -> None:
        self._session_manager = session_manager
        self._state_store = state_store
        self._round_coordinator = round_coordinator
        self._artifact_store = artifact_store
        self._user_id = user_id
        self._session_models_ready = False

    async def ensure_session_models(self) -> None:
        """Initialize session ORM models exactly once for this service instance."""

        if self._session_models_ready:
            return
        await self._session_manager.setup_models()
        self._session_models_ready = True
        log_runtime_event(
            logger,
            "session_models_ready",
            level=logging.DEBUG,
            user_id=self._user_id,
        )

    async def hydrate_round_state(self) -> None:
        """Load persisted round state and replace the in-memory coordinator state."""

        state = await self._state_store.load(
            self._round_coordinator.state.session_id,
            response_log_path=self._artifact_store.response_log_path,
            evaluation_log_path=self._artifact_store.evaluation_log_path,
        )
        self._round_coordinator.replace_state(state)
        log_runtime_event(
            logger,
            "round_state_hydrated",
            level=logging.DEBUG,
            session_id=self._round_coordinator.state.session_id,
            session_step=self._round_coordinator.state.session_step,
            round_num=self._round_coordinator.state.round_num,
            round_step=self._round_coordinator.state.round_step,
            step_log_count=len(self._round_coordinator.state.step_logs),
            pending_round_inputs=len(self._round_coordinator.state.pending_round_inputs),
            pending_command_results=len(self._round_coordinator.state.pending_command_results),
            pending_resume_results=len(self._round_coordinator.state.pending_resume_results),
        )
