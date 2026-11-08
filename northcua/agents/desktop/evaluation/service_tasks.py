"""Async task and event helpers for the desktop round evaluator service."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.routing import EvaluatorClientToolDispatcher, ExternalEvaluatorToolDispatcher
from northcua.app.logging import log_runtime_event
from northcua.core.config import NorthCUAAppConfig
from northcua.core.models import StepLogRecord

from .events import DesktopEvaluationEvent, DesktopEvaluationEventCallback
from .protocol import RoundEvaluationRecord

logger = logging.getLogger(__name__)


class _RoundEvaluatorTaskHost(Protocol):
    """Host surface required by ``RoundEvaluatorTaskManagementMixin``."""

    app_config: NorthCUAAppConfig
    event_callback: DesktopEvaluationEventCallback | None
    _pending_tasks: set[asyncio.Task[RoundEvaluationRecord | None]]
    _completed_failures: list[BaseException]

    async def evaluate_round_and_maybe_rollup(
        self,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        terminal: bool,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> RoundEvaluationRecord | None:
        """Evaluate one round and optionally emit session rollup records.

        Args:
            round_request: User query that produced the round under review.
            round_num: One-based round number associated with ``step_logs``.
            step_logs: Persisted main-agent step records used as evaluation
                evidence.
            terminal: Whether this evaluation should also trigger end-of-session
                rollup behavior.
            client_tool_dispatcher: Optional dispatcher for live client-side
                evaluator tools when evaluation runs over a remote device.

        Returns:
            Persisted evaluation record when evaluation is enabled and produces
            a round report; otherwise ``None``.
        """

    def _track_completed_task(self, task: asyncio.Task[RoundEvaluationRecord | None]) -> None:
        """Track completion for a scheduled round evaluator task."""

    def _raise_or_clear_completed_failures(self) -> None:
        """Raise or clear completed evaluator task failures."""


class RoundEvaluatorTaskManagementMixin:
    """Task scheduling and progress-event behavior shared by evaluator services."""

    def _emit_event(
        self: _RoundEvaluatorTaskHost, *, name: str, session_id: str, round_num: int, payload: JsonObject
    ) -> None:
        """Emit a best-effort evaluator progress event for terminal UIs."""
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(DesktopEvaluationEvent(name=name, session_id=session_id, round_num=round_num, payload=payload))
        except Exception as exc:
            log_runtime_event(
                logger,
                "round_evaluation_event_callback_failed",
                callback_event=name,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def schedule_round_evaluation(
        self: _RoundEvaluatorTaskHost,
        *,
        round_request: str,
        round_num: int,
        step_logs: list[StepLogRecord],
        terminal: bool,
        client_tool_dispatcher: EvaluatorClientToolDispatcher | ExternalEvaluatorToolDispatcher | None = None,
    ) -> asyncio.Task[RoundEvaluationRecord | None]:
        """Create and track one asynchronous round evaluation task.

        Args:
            round_request: User request for the evaluated round.
            round_num: Round number to evaluate.
            step_logs: Persisted step logs available to the evaluator.
            terminal: Whether this round should trigger a session rollup.
            client_tool_dispatcher: Optional live client tool dispatcher.

        Returns:
            Created asyncio task for the evaluation.
        """
        task = asyncio.create_task(
            self.evaluate_round_and_maybe_rollup(
                round_request=round_request,
                round_num=round_num,
                step_logs=step_logs,
                terminal=terminal,
                client_tool_dispatcher=client_tool_dispatcher,
            ),
            name=f"northcua-round-evaluator:{round_num}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._track_completed_task)
        return task

    def _track_completed_task(self: _RoundEvaluatorTaskHost, task: asyncio.Task[RoundEvaluationRecord | None]) -> None:
        self._pending_tasks.discard(task)
        if task.cancelled():
            self._completed_failures.append(asyncio.CancelledError("round evaluation task was cancelled"))
            return
        failure = task.exception()
        if failure is not None:
            self._completed_failures.append(failure)

    async def drain(self: _RoundEvaluatorTaskHost) -> None:
        """Wait for all pending evaluator tasks to complete.

        Raises:
            BaseException: Re-raises evaluator failures when fail-open is
                disabled, including timeout and completed async failures.
        """
        if not self._pending_tasks:
            self._raise_or_clear_completed_failures()
            return
        tasks = list(self._pending_tasks)
        timeout_seconds = self.app_config.evaluation.drain_timeout_seconds
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_seconds)
        except TimeoutError as exc:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            log_runtime_event(
                logger, "round_evaluation_drain_timed_out", timeout_seconds=timeout_seconds, pending_count=len(pending)
            )
            if not self.app_config.evaluation.fail_open:
                raise TimeoutError(f"Timed out waiting for evaluator drain after {timeout_seconds:.2f}s") from exc
            self._raise_or_clear_completed_failures()
            return
        failures = [result for result in results if isinstance(result, Exception)]
        if not failures:
            self._raise_or_clear_completed_failures()
            return
        for failure in failures:
            self._completed_failures.append(failure)
        self._raise_or_clear_completed_failures()

    def _raise_or_clear_completed_failures(self: _RoundEvaluatorTaskHost) -> None:
        failures = list(self._completed_failures)
        self._completed_failures.clear()
        if not failures:
            return
        for failure in failures:
            log_runtime_event(
                logger, "round_evaluation_drain_failed", error=str(failure), error_type=type(failure).__name__
            )
        if not self.app_config.evaluation.fail_open:
            raise failures[0]
