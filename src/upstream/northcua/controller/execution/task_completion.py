"""Terminal task completion and cancellation handling."""

from __future__ import annotations

import asyncio
import logging

from northcua.controller.control_plane.connections import ConnectionRegistry, WebSocketPeer
from northcua.controller.execution.task_artifacts import TaskArtifactMaterializer
from northcua.controller.execution.task_completion_steps import (
    TERMINAL_TASK_STATES,
    cancel_active_session_commands,
    materialize_payload_artifacts,
    release_task_lease,
    send_task_finished_to_submitter,
)
from northcua.controller.execution.task_lease_release import CancelActiveCommands, TaskLeaseReleaseHandler
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import (
    ControllerStorePriority,
    controller_store_call,
    controller_store_read,
)
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.queue import TaskQueue
from northcua.controller.storage.result_compaction import compact_task_finished_for_sqlite
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.payloads import SessionLeaseGranted, TaskCancel, TaskFinished
from northcua_protocol.state import TaskState

logger = logging.getLogger(__name__)


class TaskTerminalCompleter:
    """Validate, persist, and forward terminal task payloads."""

    def __init__(
        self,
        *,
        connections: ConnectionRegistry,
        queue: TaskQueue,
        events: ControllerEventLog,
        artifacts: TaskArtifactMaterializer,
        submitter_by_task_id: dict[str, WebSocketPeer],
        lease_by_session_id: dict[str, SessionLeaseGranted],
        cancel_active_commands: CancelActiveCommands | None,
        lease_releases: TaskLeaseReleaseHandler,
    ) -> None:
        self.connections = connections
        self.queue = queue
        self.events = events
        self.artifacts = artifacts
        self.submitter_by_task_id = submitter_by_task_id
        self.lease_by_session_id = lease_by_session_id
        self.cancel_active_commands = cancel_active_commands
        self.lease_releases = lease_releases

    async def finish_task(
        self, *, websocket: WebSocketPeer, payload: TaskFinished, correlation_id: str | None = None
    ) -> None:
        """Persist a device-originated terminal task after lease validation.

        Args:
            websocket: Device websocket attempting to finish the task.
            payload: Terminal task payload.
            correlation_id: Optional envelope correlation id.

        Raises:
            PermissionError: If the websocket does not own the active lease.
        """

        await self._validate_task_finish_sender(websocket, payload)
        await self.complete_task(
            payload=payload,
            correlation_id=correlation_id,
            allow_controller_blob_artifacts=False,
        )

    async def cancel_task(
        self,
        *,
        websocket: WebSocketPeer,
        payload: TaskCancel,
        correlation_id: str | None = None,
    ) -> None:
        """Interrupt one task on behalf of its owning submitter.

        Args:
            websocket: Submitter websocket that owns the task.
            payload: Task cancel request.
            correlation_id: Optional envelope correlation id.

        Raises:
            PermissionError: If the task is unknown or owned by another submitter.
        """

        row, session_row = await controller_store_read(
            self.queue.store.path,
            lambda store: (store.task_row(payload.task_id), store.session_row_for_task(payload.task_id)),
        )
        if row is None:
            raise PermissionError("task cancel references unknown task")
        submitter = self.submitter_by_task_id.get(payload.task_id)
        if submitter is not websocket:
            raise PermissionError("task cancel sender does not own the task")
        if session_row is None:
            raise PermissionError("task cancel references a task without a session")
        session_id = str(session_row["session_id"])
        if payload.session_id and payload.session_id != session_id:
            raise PermissionError("task cancel session_id does not match the persisted task session")
        self.events.write(
            "task_cancel_requested",
            trace_id=payload.trace_id,
            entity_type="task",
            entity_id=payload.task_id,
            session_id=session_id,
            reason=payload.reason,
        )
        await self.complete_task(
            payload=TaskFinished(
                task_id=payload.task_id,
                trace_id=payload.trace_id,
                session_id=session_id,
                state=TaskState.INTERRUPTED,
                device_id=str(session_row["device_id"] or ""),
                lease_id=str(session_row["lease_id"] or ""),
                error=payload.reason or "task cancelled by submitter",
                terminal_decision=AgentTerminalDecision.error(
                    reason=AgentTerminalReason.INTERRUPTED,
                    final_answer=payload.reason or "task cancelled by submitter",
                    summary="The controller interrupted the task after a cancel request.",
                    retryable=False,
                ).to_json(),
            ),
            correlation_id=correlation_id,
            allow_controller_blob_artifacts=False,
        )

    async def complete_task(
        self,
        *,
        payload: TaskFinished,
        correlation_id: str | None = None,
        allow_controller_blob_artifacts: bool = False,
    ) -> None:
        """Persist and forward a controller-approved terminal payload.

        Args:
            payload: Terminal task payload approved by controller or executor.
            correlation_id: Optional envelope correlation id.
            allow_controller_blob_artifacts: Whether artifacts that already
                point at controller-owned blob storage are trusted and verified.

        Raises:
            asyncio.CancelledError: Re-raised after terminal post-persist
                cleanup has been reconciled when caller cancellation races with
                task completion.
        """

        logger.info(
            "controller complete_task started",
            extra={
                "task_id": payload.task_id,
                "session_id": payload.session_id,
                "state": payload.state.value,
                "artifact_count": len(payload.artifacts),
            },
        )
        existing = await controller_store_read(self.queue.store.path, lambda store: store.task_row(payload.task_id))
        if existing is not None and str(existing["status"]) in TERMINAL_TASK_STATES:
            logger.info(
                "controller complete_task deduped",
                extra={
                    "task_id": payload.task_id,
                    "session_id": payload.session_id,
                    "prior_status": str(existing["status"]),
                },
            )
            self.events.write(
                "task_finish_deduped",
                trace_id=payload.trace_id,
                entity_type="task",
                entity_id=payload.task_id,
                prior_status=str(existing["status"]) if existing is not None else "",
            )
            return
        payload = await asyncio.to_thread(
            materialize_payload_artifacts,
            artifacts=self.artifacts,
            payload=payload,
            allow_controller_blob=allow_controller_blob_artifacts,
        )
        result_json = await asyncio.to_thread(lambda: compact_task_finished_for_sqlite(payload).model_dump_json())
        finish_task = asyncio.create_task(self.queue.finish_async(payload, result_json=result_json))
        try:
            changed = await asyncio.shield(finish_task)
        except asyncio.CancelledError:
            changed = await finish_task
            if changed:
                await self._complete_task_after_persist(
                    payload=payload,
                    correlation_id=correlation_id,
                )
            raise
        if not changed:
            current = await controller_store_read(self.queue.store.path, lambda store: store.task_row(payload.task_id))
            self.events.write(
                "task_finish_deduped",
                trace_id=payload.trace_id,
                entity_type="task",
                entity_id=payload.task_id,
                prior_status=str(current["status"]) if current is not None else "",
            )
            return
        post_persist_task = asyncio.create_task(
            self._complete_task_after_persist(
                payload=payload,
                correlation_id=correlation_id,
            )
        )
        try:
            await asyncio.shield(post_persist_task)
        except asyncio.CancelledError:
            await post_persist_task
            raise

    async def _complete_task_after_persist(
        self,
        *,
        payload: TaskFinished,
        correlation_id: str | None,
    ) -> None:
        await cancel_active_session_commands(cancel_active_commands=self.cancel_active_commands, payload=payload)
        logger.info(
            "controller complete_task queue_finished",
            extra={"task_id": payload.task_id, "session_id": payload.session_id, "state": payload.state.value},
        )
        await controller_store_call(
            self.queue.store.path,
            lambda store: _record_payload_artifacts_with_store(store, payload),
            operation_name="task_finish_artifacts",
            priority=ControllerStorePriority.CRITICAL,
        )
        logger.info(
            "controller complete_task artifacts_recorded",
            extra={
                "task_id": payload.task_id,
                "session_id": payload.session_id,
                "artifact_count": len(payload.artifacts),
            },
        )
        await release_task_lease(lease_releases=self.lease_releases, payload=payload)
        await send_task_finished_to_submitter(
            submitter=self.submitter_by_task_id.get(payload.task_id),
            payload=payload,
            correlation_id=correlation_id,
        )

    async def _validate_task_finish_sender(self, websocket: WebSocketPeer, payload: TaskFinished) -> None:
        active_lease = self.lease_by_session_id.get(payload.session_id)
        if active_lease is None:
            raise PermissionError("task finish references a session without an active lease")
        session_row, task_row = await controller_store_read(
            self.queue.store.path,
            lambda store: (store.session_row(payload.session_id), store.task_row(payload.task_id)),
        )
        if session_row is None or str(session_row["task_id"]) != payload.task_id:
            raise PermissionError("task finish task_id does not match the active session")
        if task_row is None:
            raise PermissionError("task finish references an unknown task")
        if str(task_row["status"]) in TERMINAL_TASK_STATES:
            raise PermissionError("task finish references an already terminal task")
        connection = self.connections.get(websocket)
        if (
            connection.device_id
            and connection.device_id == active_lease.device_id
            and active_lease.task_id == payload.task_id
            and payload.lease_id == active_lease.lease_id
            and payload.device_id == active_lease.device_id
        ):
            return
        raise PermissionError("task finish sender does not own the active lease")


def _record_payload_artifacts_with_store(store: SQLiteControllerStore, payload: TaskFinished) -> None:
    for artifact in payload.artifacts:
        store.record_artifact_ref(
            artifact,
            trace_id=payload.trace_id,
            task_id=payload.task_id,
            session_id=payload.session_id,
            source="task_output",
        )


__all__ = ["TaskTerminalCompleter"]
