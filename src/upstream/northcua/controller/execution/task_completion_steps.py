"""Focused steps used by controller terminal task completion."""

from __future__ import annotations

import inspect
import logging

from northcua.controller.control_plane.connections import WebSocketPeer
from northcua.controller.execution.task_artifacts import TaskArtifactMaterializer
from northcua.controller.execution.task_lease_release import CancelActiveCommands, TaskLeaseReleaseHandler
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import SessionLeaseReleased, TaskFinished
from northcua_protocol.state import SessionState, TaskState

logger = logging.getLogger(__name__)


TERMINAL_TASK_STATES = {
    TaskState.FINISHED.value,
    TaskState.FAILED.value,
    TaskState.INTERRUPTED.value,
}


def materialize_payload_artifacts(
    *,
    artifacts: TaskArtifactMaterializer,
    payload: TaskFinished,
    allow_controller_blob: bool = False,
) -> TaskFinished:
    """Copy task artifacts into controller storage and return the new payload.

    Args:
        artifacts: Controller artifact materializer.
        payload: Terminal task payload with source artifact descriptors.
        allow_controller_blob: Whether pre-materialized controller blobs from
            controller-owned runners are allowed.

    Returns:
        Payload whose artifact descriptors point at controller-owned storage.
    """

    logger.info(
        "controller complete_task materialize_started",
        extra={"task_id": payload.task_id, "session_id": payload.session_id},
    )
    materialized = payload.model_copy(
        update={"artifacts": artifacts.materialize(payload.artifacts, allow_controller_blob=allow_controller_blob)}
    )
    logger.info(
        "controller complete_task materialize_completed",
        extra={
            "task_id": payload.task_id,
            "session_id": payload.session_id,
            "artifact_count": len(materialized.artifacts),
        },
    )
    return materialized


async def cancel_active_session_commands(
    *,
    cancel_active_commands: CancelActiveCommands | None,
    payload: TaskFinished,
) -> None:
    """Cancel active remote commands owned by the finishing session.

    Args:
        cancel_active_commands: Optional command-cancel callback.
        payload: Terminal task payload naming the session.
    """

    if payload.session_id and cancel_active_commands is not None:
        result = cancel_active_commands(payload.session_id, payload.trace_id, f"task {payload.state.value}")
        if inspect.isawaitable(result):
            await result
        logger.info(
            "controller complete_task active_commands_cancelled",
            extra={"task_id": payload.task_id, "session_id": payload.session_id},
        )


async def release_task_lease(*, lease_releases: TaskLeaseReleaseHandler, payload: TaskFinished) -> None:
    """Release the device lease attached to a terminal task payload.

    Args:
        lease_releases: Controller lease release handler.
        payload: Terminal task payload with lease and device identifiers.
    """

    if not (payload.lease_id and payload.device_id):
        return
    logger.info(
        "controller complete_task lease_release_started",
        extra={
            "task_id": payload.task_id,
            "session_id": payload.session_id,
            "lease_id": payload.lease_id,
            "device_id": payload.device_id,
        },
    )
    await lease_releases.release(
        SessionLeaseReleased(
            lease_id=payload.lease_id,
            session_id=payload.session_id,
            device_id=payload.device_id,
            state=SessionState.RELEASED,
            reason=f"task {payload.state.value}",
        ),
        trace_id=payload.trace_id,
    )
    logger.info(
        "controller complete_task lease_release_completed",
        extra={
            "task_id": payload.task_id,
            "session_id": payload.session_id,
            "lease_id": payload.lease_id,
            "device_id": payload.device_id,
        },
    )


async def send_task_finished_to_submitter(
    *,
    submitter: WebSocketPeer | None,
    payload: TaskFinished,
    correlation_id: str | None,
) -> None:
    """Forward terminal payload to the submitter websocket when it is present.

    Args:
        submitter: Submitter websocket peer, or ``None`` for detached tasks.
        payload: Terminal task payload to send.
        correlation_id: Optional envelope correlation id.
    """

    if submitter is None:
        logger.info(
            "controller complete_task no_submitter_registered",
            extra={"task_id": payload.task_id, "session_id": payload.session_id},
        )
        return
    logger.info(
        "controller complete_task submitter_send_started",
        extra={"task_id": payload.task_id, "session_id": payload.session_id},
    )
    await submitter.send(
        Envelope.wrap(
            MessageType.TASK_FINISHED,
            payload,
            trace_id=payload.trace_id,
            correlation_id=correlation_id,
        ).model_dump_json()
    )
    logger.info(
        "controller complete_task submitter_send_completed",
        extra={"task_id": payload.task_id, "session_id": payload.session_id},
    )
