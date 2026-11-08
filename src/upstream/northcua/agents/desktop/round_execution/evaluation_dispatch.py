"""Evaluator scheduling helpers for round execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from northcua.agents.desktop.evaluation.service import RoundEvaluatorService
from northcua.app.logging import log_runtime_event, runtime_logging_context
from northcua.core.config import NorthCUAAppConfig
from northcua.core.enums import EvaluationSchedule, TaskStatus
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.transport.ws.remote_round import ExternalToolDispatcher

logger = logging.getLogger(__name__)
_TERMINAL_TASK_STATUSES = {TaskStatus.FINISH, TaskStatus.FAIL, TaskStatus.ERROR}


class RoundEvaluationScheduler:
    """Run or schedule round evaluation while preserving runtime logging semantics."""

    def __init__(
        self,
        *,
        round_evaluator_service: RoundEvaluatorService,
        app_config: NorthCUAAppConfig,
        artifact_store: NorthCUAArtifactStore,
    ) -> None:
        self._round_evaluator_service = round_evaluator_service
        self._app_config = app_config
        self._artifact_store = artifact_store

    async def drain(self) -> None:
        """Drain background evaluator tasks before shutdown or CLI exit."""
        await self._round_evaluator_service.drain()

    async def maybe_run_round_evaluation(
        self,
        step_log: StepLogRecord,
        step_logs: list[StepLogRecord],
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> bool:
        """Run, schedule, or skip evaluator work for a completed round.

        Args:
            step_log: Persisted step record for the round that just completed.
            step_logs: Snapshot of all step records available to the evaluator.

        Raises:
            Exception: Re-raises evaluator failures only when evaluation fail-open
                is disabled in app config.
        """

        schedule = self._effective_schedule()
        if not self._round_evaluator_service.enabled:
            return False
        terminal = step_log.status in _TERMINAL_TASK_STATUSES
        if schedule == EvaluationSchedule.SESSION_ROLLUP_ONLY:
            await _rollup_session(
                service=self._round_evaluator_service,
                app_config=self._app_config,
                step_log=step_log,
                enabled=terminal,
            )
            return bool(terminal)
        if schedule == EvaluationSchedule.AFTER_ROUND_SYNC:
            if not terminal:
                _log_evaluation_event("round_evaluation_skipped", step_log, reason="non_terminal_round")
                return False
            await _run_sync_round_evaluation(
                service=self._round_evaluator_service,
                app_config=self._app_config,
                step_log=step_log,
                step_logs=step_logs,
                terminal=terminal,
                remote_tool_dispatcher=remote_tool_dispatcher,
            )
            return True
        if schedule != EvaluationSchedule.AFTER_ROUND_ASYNC:
            return False
        if not terminal:
            _log_evaluation_event("round_evaluation_skipped", step_log, reason="non_terminal_round")
            return False
        _schedule_async_round_evaluation(
            service=self._round_evaluator_service,
            artifact_store=self._artifact_store,
            step_log=step_log,
            step_logs=step_logs,
            terminal=terminal,
            remote_tool_dispatcher=remote_tool_dispatcher,
        )
        return False

    async def run_step_limit_evaluation(
        self,
        step_log: StepLogRecord,
        step_logs: list[StepLogRecord],
        *,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Run or schedule final evaluator work for a max-step task exit.

        Args:
            step_log: Last persisted step log before the step limit was hit.
            step_logs: Full session step log snapshot available to the evaluator.
            remote_tool_dispatcher: Optional dispatcher for remote evaluator
                client tools.

        Raises:
            Exception: Re-raises evaluator failures when fail-open is disabled.
        """
        schedule = self._effective_schedule()
        if not self._round_evaluator_service.enabled:
            return
        if schedule == EvaluationSchedule.SESSION_ROLLUP_ONLY:
            await _rollup_session(
                service=self._round_evaluator_service,
                app_config=self._app_config,
                step_log=step_log,
                enabled=True,
                reason="step_limit",
            )
            return
        if schedule == EvaluationSchedule.AFTER_ROUND_SYNC:
            await _run_sync_round_evaluation(
                service=self._round_evaluator_service,
                app_config=self._app_config,
                step_log=step_log,
                step_logs=step_logs,
                terminal=True,
                remote_tool_dispatcher=remote_tool_dispatcher,
                reason="step_limit",
            )
            return
        if schedule != EvaluationSchedule.AFTER_ROUND_ASYNC:
            return
        _schedule_async_round_evaluation(
            service=self._round_evaluator_service,
            artifact_store=self._artifact_store,
            step_log=step_log,
            step_logs=step_logs,
            terminal=True,
            remote_tool_dispatcher=remote_tool_dispatcher,
            reason="step_limit",
        )

    def _effective_schedule(self) -> EvaluationSchedule:
        effective_schedule = getattr(self._round_evaluator_service, "effective_schedule", None)
        if callable(effective_schedule):
            return cast(EvaluationSchedule, effective_schedule())
        return self._app_config.evaluation.schedule


def _log_evaluation_event(event_name: str, step_log: StepLogRecord, **metadata: object) -> None:
    """Log one scheduler event with stable desktop round fields."""

    fields: dict[str, object] = {
        "session_id": step_log.session_id,
        "session_step": step_log.session_step,
        "round_num": step_log.round_num,
        **metadata,
    }
    log_runtime_event(
        logger,
        event_name,
        level=logging.INFO,
        **cast(Any, fields),
    )


async def _rollup_session(
    *,
    service: RoundEvaluatorService,
    app_config: NorthCUAAppConfig,
    step_log: StepLogRecord,
    enabled: bool,
    reason: str | None = None,
) -> None:
    """Run session rollup when requested by the effective schedule."""

    if not enabled:
        return
    try:
        await service.rollup_session()
    except Exception as exc:
        logger.exception("Session evaluator rollup failed")
        fields: dict[str, object] = {"error": str(exc)}
        if reason is not None:
            fields["reason"] = reason
        _log_evaluation_event("session_evaluation_rollup_failed", step_log, **fields)
        if not app_config.evaluation.fail_open:
            raise


async def _run_sync_round_evaluation(
    *,
    service: RoundEvaluatorService,
    app_config: NorthCUAAppConfig,
    step_log: StepLogRecord,
    step_logs: list[StepLogRecord],
    terminal: bool,
    remote_tool_dispatcher: ExternalToolDispatcher | None,
    reason: str | None = None,
) -> None:
    """Run the evaluator synchronously and honor fail-open policy."""

    try:
        await service.evaluate_round_and_maybe_rollup(
            round_request=step_log.request,
            round_num=step_log.round_num,
            step_logs=step_logs,
            terminal=terminal,
            client_tool_dispatcher=remote_tool_dispatcher,
        )
        _log_round_evaluation_completion("round_evaluation_completed", step_log, reason=reason)
    except Exception as exc:
        logger.exception("Round evaluator failed")
        _log_round_evaluation_completion("round_evaluation_failed", step_log, error=str(exc), reason=reason)
        if not app_config.evaluation.fail_open:
            raise


def _schedule_async_round_evaluation(
    *,
    service: RoundEvaluatorService,
    artifact_store: NorthCUAArtifactStore,
    step_log: StepLogRecord,
    step_logs: list[StepLogRecord],
    terminal: bool,
    remote_tool_dispatcher: ExternalToolDispatcher | None,
    reason: str | None = None,
) -> None:
    """Schedule the evaluator and attach stable completion logging."""

    task = service.schedule_round_evaluation(
        round_request=step_log.request,
        round_num=step_log.round_num,
        step_logs=step_logs,
        terminal=terminal,
        client_tool_dispatcher=remote_tool_dispatcher,
    )
    fields: dict[str, object] = {"terminal": terminal}
    if reason is not None:
        fields["reason"] = reason
    _log_evaluation_event("round_evaluation_scheduled", step_log, **fields)
    task.add_done_callback(
        lambda completed_task: _log_async_round_completion(
            artifact_store=artifact_store,
            completed_task=completed_task,
            step_log=step_log,
            reason=reason,
        )
    )


def _log_async_round_completion(
    *,
    artifact_store: NorthCUAArtifactStore,
    completed_task: asyncio.Task[object],
    step_log: StepLogRecord,
    reason: str | None,
) -> None:
    """Log async evaluator completion in the runtime JSONL context."""

    with runtime_logging_context(log_path=artifact_store.runtime_log_path, session_id=step_log.session_id):
        try:
            completed_task.result()
        except Exception as exc:
            logger.exception("Async round evaluator failed")
            _log_round_evaluation_completion("round_evaluation_failed", step_log, error=str(exc), reason=reason)
        else:
            _log_round_evaluation_completion("round_evaluation_completed", step_log, reason=reason)


def _log_round_evaluation_completion(
    event_name: str,
    step_log: StepLogRecord,
    *,
    error: str | None = None,
    reason: str | None = None,
) -> None:
    """Log evaluator completion/failure with optional error and reason fields."""

    fields: dict[str, object] = {}
    if error is not None:
        fields["error"] = error
    if reason is not None:
        fields["reason"] = reason
    _log_evaluation_event(event_name, step_log, **fields)
