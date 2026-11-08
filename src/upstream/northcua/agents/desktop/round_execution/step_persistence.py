"""Step and trace persistence helpers for round execution."""

from __future__ import annotations

import logging
import time

from northcua.agentinfra.tracing.log_writer import LLMTraceLogWriter
from northcua.agentkit.runtime import AgentRuntimeCursor
from northcua.app.logging import log_runtime_event
from northcua.core.enums import TaskStatus
from northcua.core.models import DesktopStateSnapshot, EvaluationLogRecord, LLMTraceRecord, StepLogRecord

from ..services.step_log_rewrite import rewrite_step_logs_preserving_existing

logger = logging.getLogger(__name__)
_TERMINAL_TASK_STATUSES = {TaskStatus.FINISH, TaskStatus.FAIL, TaskStatus.ERROR}


class RoundStepPersister:
    """Persist llm traces, step logs, and terminal evaluation summaries."""

    def __init__(
        self,
        *,
        artifact_store,
        state_store,
        round_coordinator,
        session_manager,
        user_id: str,
        llm_trace_writer: LLMTraceLogWriter,
    ) -> None:
        self._artifact_store = artifact_store
        self._state_store = state_store
        self._round_coordinator = round_coordinator
        self._session_manager = session_manager
        self._user_id = user_id
        self._llm_trace_writer = llm_trace_writer

    def persist_llm_trace(
        self,
        *,
        session_id: str,
        session_step: int,
        round_num: int,
        round_step: int,
        request: str,
        llm_trace: LLMTraceRecord,
    ) -> None:
        """Append one desktop llm trace record.

        Args:
            session_id: Session identifier for the trace row.
            session_step: Monotonic step number across the session.
            round_num: User-facing round number.
            round_step: Step number within the current round.
            request: User request text for the round.
            llm_trace: Full provider request/response trace to persist.
        """

        self._llm_trace_writer.log_desktop_trace(
            session_id=session_id,
            session_step=session_step,
            round_num=round_num,
            round_step=round_step,
            request=request,
            llm_trace=llm_trace,
        )

    async def persist_step(
        self,
        *,
        step_log: StepLogRecord,
        snapshot: DesktopStateSnapshot,
        duration_ms: float,
        terminal_summary: str,
        advance_counters: bool = True,
        save_state: bool = True,
    ) -> None:
        """Persist the completed step and update persisted session state.

        Args:
            step_log: Step record to append to in-memory state and steps.jsonl.
            snapshot: Latest desktop snapshot associated with the step.
            duration_ms: End-to-end round duration in milliseconds.
            terminal_summary: Summary written to the legacy evaluation log when
                the step reaches a terminal status.
            advance_counters: Whether to advance desktop counters here. Shared
                runtime callers pass ``False`` because AgentSessionRuntime owns
                counter progression.
            save_state: Whether to save SessionManager state immediately.
                Shared runtime callers save after counters have advanced.
        """

        self._round_coordinator.state.step_logs.append(step_log)
        self._round_coordinator.state.latest_snapshot = snapshot
        step_log_payload = step_log.model_dump(mode="json")
        if advance_counters:
            self._round_coordinator.state.session_step += 1
            self._round_coordinator.state.round_step += 1
        if save_state:
            session = await self._session_manager.get_session(user_id=self._user_id, session_id=step_log.session_id)
            storage = session.storage if session is not None else None
            await self._state_store.save(self._round_coordinator.state, storage=storage)
        self._artifact_store.append_jsonl(self._artifact_store.step_log_path, step_log_payload)
        log_runtime_event(
            logger,
            "round_step_persisted",
            session_id=step_log.session_id,
            session_step=step_log.session_step,
            round_num=step_log.round_num,
            round_step=step_log.round_step,
            status=step_log.status,
            assistant_tool_calls=len(step_log.assistant_tool_calls),
            tool_results=len(step_log.tool_results),
            step_log_path=self._artifact_store.step_log_path,
            evaluation_log_path=self._artifact_store.evaluation_log_path,
            duration_ms=round(duration_ms, 2),
        )
        if step_log.status in _TERMINAL_TASK_STATUSES:
            # The compact evaluation log predates round_evaluations.jsonl; keep
            # it stable for existing dashboards and session summaries.
            evaluation_record = EvaluationLogRecord(
                session_id=step_log.session_id,
                round_num=step_log.round_num,
                session_step=step_log.session_step,
                status=step_log.status,
                request=step_log.request,
                result=step_log.result,
                summary=terminal_summary,
                terminal_decision=dict(step_log.terminal_decision),
            )
            self._artifact_store.append_jsonl(
                self._artifact_store.evaluation_log_path,
                evaluation_record.model_dump(mode="json"),
            )
            log_runtime_event(
                logger,
                "round_terminal_status_reached",
                session_id=step_log.session_id,
                round_num=step_log.round_num,
                session_step=step_log.session_step,
                status=step_log.status,
                evaluation_log_path=self._artifact_store.evaluation_log_path,
            )

    async def save_shared_runtime_failure_state(self, cursor: AgentRuntimeCursor) -> None:
        """Save RoundState after a shared-runtime step persisted but post-work failed.

        Args:
            cursor: Shared runtime cursor for the persisted step.
        """

        state = self._round_coordinator.state
        state.round_num = max(state.round_num, cursor.round_num)
        if cursor.phase.counts_toward_session:
            state.session_step = max(state.session_step, cursor.session_step + 1)
            state.round_step = max(state.round_step, cursor.round_step + 1)
        session = await self._session_manager.get_session(user_id=self._user_id, session_id=cursor.session_id)
        storage = session.storage if session is not None else None
        await self._state_store.save(state, storage=storage)
        log_runtime_event(
            logger,
            "round_state_saved_after_post_step_failure",
            session_id=cursor.session_id,
            round_num=state.round_num,
            session_step=state.session_step,
            round_step=state.round_step,
        )

    async def rewrite_step_logs_after_terminal_metadata(self, *, session_id: str, session_step: int) -> None:
        """Persist terminal evaluator metadata added after the step row append."""

        session = await self._session_manager.get_session(user_id=self._user_id, session_id=session_id)
        storage = session.storage if session is not None else None
        await self._state_store.save(self._round_coordinator.state, storage=storage)
        rewrite_step_logs_preserving_existing(
            self._round_coordinator.state.step_logs,
            self._artifact_store.step_log_path,
        )
        log_runtime_event(
            logger,
            "round_step_logs_rewritten_after_terminal_evaluation",
            session_id=session_id,
            session_step=session_step,
            step_log_path=self._artifact_store.step_log_path,
        )
