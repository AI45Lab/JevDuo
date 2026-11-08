"""Lifecycle helpers for desktop controller-owned remote runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from time import perf_counter
from typing import Any, cast

from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua_controller_plugins.desktop_artifacts import DesktopRemoteArtifactCollector
from northcua_protocol.payloads import TaskFinished
from northcua_protocol.state import CommandState

logger = logging.getLogger("northcua.controller_plugins.desktop_runner_lifecycle")


def _observe_stop_task(task: asyncio.Task[object]) -> None:
    """Consume late cleanup task failures after terminal payload emission."""

    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.debug("desktop_remote_app_stop_background_failed", exc_info=True)


def log_lifecycle(
    context: ControllerExecutionContext,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit remote-run lifecycle checkpoints into runtime logs.

    Args:
        context: Controller context identifying the task and session.
        event: Stable lifecycle event name.
        level: Logging level for the event.
        fields: Additional structured log fields.
    """

    logger.log(
        level,
        event,
        extra={
            "event": event,
            "task_id": context.request.task_id,
            "trace_id": context.request.trace_id,
            "session_id": context.session_id,
            **fields,
        },
    )


async def stop_app_best_effort(
    *, app: object, context: ControllerExecutionContext, timeout_seconds: float = 10.0
) -> None:
    """Stop the desktop application without blocking terminal task emission.

    Controller-owned task completion must not depend on cleanup hooks. A hung
    websocket/server/facade shutdown would otherwise keep the task and lease in
    `running` forever, hiding the TaskFinished event from PiP.

    Args:
        app: Desktop application facade that may expose an async ``stop`` hook.
        context: Controller context used for structured cleanup logs.
        timeout_seconds: Maximum time to wait before canceling cleanup.
    """

    stop = getattr(app, "stop", None)
    if not callable(stop):
        return
    stop_started_at = perf_counter()
    log_lifecycle(context, "desktop_remote_app_stop_started", timeout_seconds=timeout_seconds)
    task = asyncio.create_task(cast(Coroutine[Any, Any, object], stop()))
    task.add_done_callback(_observe_stop_task)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
    except Exception as exc:
        logger.warning(
            "desktop_remote_app_stop_wait_failed",
            extra={
                "task_id": context.request.task_id,
                "session_id": context.session_id,
                "elapsed_ms": round((perf_counter() - stop_started_at) * 1000, 2),
                "error": str(exc),
            },
            exc_info=True,
        )
        log_lifecycle(
            context,
            "desktop_remote_app_stop_failed",
            level=logging.WARNING,
            elapsed_ms=round((perf_counter() - stop_started_at) * 1000, 2),
            error=str(exc),
        )
        return
    if not done:
        task.cancel()
        log_lifecycle(
            context,
            "desktop_remote_app_stop_timed_out",
            level=logging.WARNING,
            elapsed_ms=round((perf_counter() - stop_started_at) * 1000, 2),
        )
        return
    try:
        task.result()
    except Exception as exc:
        logger.warning(
            "desktop_remote_app_stop_result_failed",
            extra={
                "task_id": context.request.task_id,
                "session_id": context.session_id,
                "elapsed_ms": round((perf_counter() - stop_started_at) * 1000, 2),
                "error": str(exc),
            },
            exc_info=True,
        )
        log_lifecycle(
            context,
            "desktop_remote_app_stop_failed",
            level=logging.WARNING,
            elapsed_ms=round((perf_counter() - stop_started_at) * 1000, 2),
            error=str(exc),
        )
        return
    log_lifecycle(
        context,
        "desktop_remote_app_stop_completed",
        elapsed_ms=round((perf_counter() - stop_started_at) * 1000, 2),
    )


def finalize_payload_with_artifacts(
    *,
    context: ControllerExecutionContext,
    payload: TaskFinished,
    artifact_store: object,
    artifact_collector: DesktopRemoteArtifactCollector,
    attach_latest_evaluation: Callable[[TaskFinished, object], TaskFinished],
) -> TaskFinished:
    """Attach latest evaluation metadata and controller-visible artifacts.

    Args:
        context: Controller context for structured lifecycle logs.
        payload: Terminal payload produced by the desktop runtime.
        artifact_store: Desktop artifact store used by the controller run.
        artifact_collector: Collector that projects artifact files into
            controller protocol descriptors.
        attach_latest_evaluation: Compatibility hook that folds evaluator
            report data into the terminal task result.

    Returns:
        Terminal task payload with evaluation and artifact descriptors attached.
    """

    log_lifecycle(context, "desktop_remote_evaluation_attach_started")
    enriched_payload = attach_latest_evaluation(payload, artifact_store)
    log_lifecycle(
        context,
        "desktop_remote_evaluation_attach_completed",
        result_kind=type(enriched_payload.result).__name__,
        has_evaluation_report=(
            isinstance(enriched_payload.result, dict)
            and any(
                isinstance(enriched_payload.result.get(key), dict)
                for key in ("evaluation_report", "round_evaluation", "final_report")
            )
        ),
    )
    artifact_started_at = perf_counter()
    log_lifecycle(context, "desktop_remote_artifact_collect_started")
    artifacts = artifact_collector.collect(context, artifact_store)
    log_lifecycle(
        context,
        "desktop_remote_artifact_collect_completed",
        elapsed_ms=round((perf_counter() - artifact_started_at) * 1000, 2),
        artifact_count=len(artifacts),
        artifact_ids=[artifact.artifact_id for artifact in artifacts],
    )
    terminal_payload = enriched_payload.model_copy(update={"artifacts": artifacts})
    log_lifecycle(
        context,
        "desktop_remote_runner_returning_terminal_payload",
        state=terminal_payload.state.value,
        artifact_count=len(terminal_payload.artifacts),
    )
    return terminal_payload
