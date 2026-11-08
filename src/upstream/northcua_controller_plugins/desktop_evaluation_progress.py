"""Evaluator progress events for controller-owned desktop runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from northcua.agents.desktop.facade import NorthCUAAgentFacade
from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.telemetry.progress import (
    RuntimeProgressEventType,
    EvaluationTelemetryState,
)
from northcua_controller_plugins.desktop_runner_evaluation_helpers import DesktopRemoteEvaluationPayloadMixin
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import TelemetryUpdate

logger = logging.getLogger(__name__)


async def drain_evaluators_with_progress(
    *,
    facade: NorthCUAAgentFacade,
    context: ControllerExecutionContext,
    drain: Callable[[NorthCUAAgentFacade], Awaitable[None]],
) -> None:
    """Drain evaluator tasks while periodically notifying PiP watchers.

    Args:
        facade: Desktop facade whose background evaluator tasks must finish.
        context: Controller task context used for task/session identifiers.
        drain: Coroutine that waits for all evaluator work owned by the facade.

    Raises:
        Exception: Re-raises any evaluator drain failure after reporting the
            failed evaluator status to controller subscribers.
    """

    if context.progress_sink is None:
        await drain(facade)
        return
    drain_task = asyncio.ensure_future(drain(facade))
    await emit_evaluation_status(context=context, state=EvaluationTelemetryState.RUNNING)
    try:
        while not drain_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(drain_task), timeout=10.0)
            except TimeoutError:
                await emit_evaluation_status(context=context, state=EvaluationTelemetryState.RUNNING)
        await drain_task
    except Exception:
        logger.exception(
            "desktop_remote_evaluator_drain_failed",
            extra={"task_id": context.request.task_id, "session_id": context.session_id},
        )
        await emit_evaluation_status(context=context, state=EvaluationTelemetryState.FAILED)
        raise
    if not await emit_evaluation_report(context=context, artifact_store=facade.artifact_store):
        await emit_evaluation_status(context=context, state=EvaluationTelemetryState.COMPLETED)


async def emit_evaluation_report(*, context: ControllerExecutionContext, artifact_store: object) -> bool:
    """Send the concrete evaluator report when it is already persisted.

    Args:
        context: Controller task context containing the progress sink.
        artifact_store: Desktop artifact store searched for the latest
            evaluator report.

    Returns:
        True when a report event was emitted. False means callers may fall back
        to a lightweight lifecycle status.
    """

    if context.progress_sink is None:
        return False
    evaluation = DesktopRemoteEvaluationPayloadMixin._latest_evaluation_record(artifact_store)
    if evaluation is None:
        return False
    report, report_path = evaluation
    payload = TelemetryUpdate(
        events=[
            {
                "event_type": RuntimeProgressEventType.EVALUATION_REPORT.value,
                "task_id": context.request.task_id,
                "trace_id": context.request.trace_id,
                "session_id": context.session_id,
                "state": EvaluationTelemetryState.COMPLETED.value,
                "title": "Verification",
                "report": report,
                "report_path": str(report_path) if report_path is not None else "",
            }
        ]
    )
    try:
        await context.progress_sink(MessageType.TELEMETRY_UPDATE, payload)
    except Exception:
        logger.debug(
            "desktop_remote_evaluation_report_send_failed",
            exc_info=True,
            extra={"task_id": context.request.task_id, "session_id": context.session_id},
        )
        return False
    return True


async def emit_evaluation_status(*, context: ControllerExecutionContext, state: EvaluationTelemetryState) -> None:
    """Send a compact evaluator lifecycle event to controller subscribers.

    Args:
        context: Controller task context containing the progress sink.
        state: Evaluator lifecycle state such as ``running`` or ``completed``.
    """

    if context.progress_sink is None:
        return
    if state is EvaluationTelemetryState.COMPLETED:
        text = "Verification report is ready."
    elif state is EvaluationTelemetryState.FAILED:
        text = "Verification did not complete cleanly."
    else:
        text = "NorthCUA is verifying the final result."
    payload = TelemetryUpdate(
        events=[
            {
                "event_type": RuntimeProgressEventType.EVALUATION_STATUS.value,
                "task_id": context.request.task_id,
                "trace_id": context.request.trace_id,
                "session_id": context.session_id,
                "state": state.value,
                "title": "Verification",
                "text": text,
            }
        ]
    )
    try:
        await context.progress_sink(MessageType.TELEMETRY_UPDATE, payload)
    except Exception:
        logger.debug(
            "desktop_remote_evaluation_status_send_failed",
            exc_info=True,
            extra={"task_id": context.request.task_id, "session_id": context.session_id, "state": state.value},
        )
