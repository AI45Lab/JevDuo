"""Lease release and interruption handling for controller tasks."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable

from northcua.controller.control_plane.connections import ConnectionRegistry, WebSocketPeer
from northcua.controller.control_plane.leases import LeaseManager
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import (
    ControllerStorePriority,
    controller_store_call,
    controller_store_read,
)
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.queue import TaskQueue
from northcua.controller.storage.result_compaction import compact_task_finished_for_sqlite
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import SessionLeaseGranted, SessionLeaseReleased, TaskFinished
from northcua_protocol.state import SessionState

from .task_interruptions import build_interrupted_task

CancelActiveCommands = Callable[[str, str, str], None | Awaitable[None]]
RetryQueuedTasks = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)
_INTERRUPTED_SUBMITTER_NOTIFY_TIMEOUT_SECONDS = 5.0


class TaskLeaseReleaseHandler:
    """Release leases and interrupt sessions while preserving controller flow."""

    def __init__(
        self,
        *,
        connections: ConnectionRegistry,
        leases: LeaseManager,
        queue: TaskQueue,
        events: ControllerEventLog,
        submitter_by_task_id: dict[str, WebSocketPeer],
        lease_by_session_id: dict[str, SessionLeaseGranted],
        cancel_active_commands: CancelActiveCommands | None,
        retry_queued_tasks: RetryQueuedTasks,
    ) -> None:
        self.connections = connections
        self.leases = leases
        self.queue = queue
        self.events = events
        self.submitter_by_task_id = submitter_by_task_id
        self.lease_by_session_id = lease_by_session_id
        self.cancel_active_commands = cancel_active_commands
        self.retry_queued_tasks = retry_queued_tasks
        self._interrupted_submitter_notification_tasks: set[asyncio.Task[None]] = set()

    async def release(self, release: SessionLeaseReleased, *, trace_id: str) -> None:
        """Release a lease and notify the owning device when connected.

        Args:
            release: Lease release payload.
            trace_id: Trace id used for controller event correlation.

        Raises:
            PermissionError: If the release payload does not match the active lease.
        """

        lease = self.lease_by_session_id.get(release.session_id)
        if lease is not None and (lease.lease_id != release.lease_id or lease.device_id != release.device_id):
            raise PermissionError("lease release does not match active lease")
        await self.leases.release_async(release, trace_id=trace_id)
        if self.cancel_active_commands is not None:
            result = self.cancel_active_commands(release.session_id, trace_id, f"lease released: {release.reason}")
            if inspect.isawaitable(result):
                await result
        self.lease_by_session_id.pop(release.session_id, None)
        connection = self.connections.for_device(release.device_id)
        if connection is not None:
            await connection.websocket.send(
                Envelope.wrap(MessageType.SESSION_LEASE_RELEASED, release, trace_id=trace_id).model_dump_json()
            )
        await self.retry_queued_tasks(trace_id=trace_id)

    async def interrupt_device(self, *, device_id: str, trace_id: str, reason: str) -> list[str]:
        """Mark known sessions for a disconnected device as interrupted.

        Args:
            device_id: Device whose websocket disconnected or became unhealthy.
            trace_id: Trace id used for controller event correlation.
            reason: Reason recorded on interrupted leases.

        Returns:
            Session ids interrupted for the disconnected device.
            reason: Standardized interruption reason for events and task errors.
        """

        interrupted_tasks = await controller_store_call(
            self.queue.store.path,
            lambda store: _interrupt_active_tasks_for_device(
                store,
                device_id=device_id,
                trace_id=trace_id,
                reason=reason,
            ),
            operation_name="task_interrupt_device",
            priority=ControllerStorePriority.CRITICAL,
        )
        interrupted_task_ids: list[str] = []
        await self._release_active_device_leases(device_id=device_id, trace_id=trace_id, reason=reason)
        for interrupted in interrupted_tasks:
            interrupted_task_ids.append(interrupted.task_id)
            submitter = self.submitter_by_task_id.get(interrupted.task_id)
            self.events.write(
                "task_finished",
                trace_id=interrupted.trace_id,
                entity_type="task",
                entity_id=interrupted.task_id,
                session_id=interrupted.session_id,
                device_id=device_id,
                status=interrupted.state.value,
            )
            self.events.write(
                "task_interrupted",
                trace_id=trace_id,
                entity_type="task",
                entity_id=interrupted.task_id,
                session_id=interrupted.session_id,
                device_id=device_id,
                reason=reason,
            )
            if submitter is None:
                continue
            self._schedule_interrupted_submitter_notification(
                submitter=submitter,
                interrupted=interrupted,
                trace_id=trace_id,
                device_id=device_id,
                reason=reason,
            )
        return interrupted_task_ids

    def _schedule_interrupted_submitter_notification(
        self,
        *,
        submitter: WebSocketPeer,
        interrupted: TaskFinished,
        trace_id: str,
        device_id: str,
        reason: str,
    ) -> None:
        task = asyncio.create_task(
            self._notify_interrupted_submitter(
                submitter=submitter,
                interrupted=interrupted,
                trace_id=trace_id,
                device_id=device_id,
                reason=reason,
            ),
            name=f"northcua-interrupted-submitter-notify:{interrupted.task_id}",
        )
        self._interrupted_submitter_notification_tasks.add(task)
        task.add_done_callback(self._discard_interrupted_submitter_notification_task)

    def _discard_interrupted_submitter_notification_task(self, task: asyncio.Task[None]) -> None:
        self._interrupted_submitter_notification_tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "interrupted submitter notification task failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    async def _notify_interrupted_submitter(
        self,
        *,
        submitter: WebSocketPeer,
        interrupted: TaskFinished,
        trace_id: str,
        device_id: str,
        reason: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                submitter.send(
                    Envelope.wrap(MessageType.TASK_FINISHED, interrupted, trace_id=trace_id).model_dump_json()
                ),
                timeout=_INTERRUPTED_SUBMITTER_NOTIFY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "controller failed to notify submitter about interrupted task",
                extra={
                    "task_id": interrupted.task_id,
                    "session_id": interrupted.session_id,
                    "device_id": device_id,
                },
                exc_info=True,
            )
            try:
                self.events.write(
                    "task_interrupted_submitter_send_failed",
                    trace_id=trace_id,
                    entity_type="task",
                    entity_id=interrupted.task_id,
                    session_id=interrupted.session_id,
                    device_id=device_id,
                    reason=reason,
                    timeout=isinstance(exc, TimeoutError),
                )
            except Exception:
                logger.warning(
                    "controller failed to record interrupted submitter notification failure",
                    extra={
                        "task_id": interrupted.task_id,
                        "session_id": interrupted.session_id,
                        "device_id": device_id,
                    },
                    exc_info=True,
                )

    async def detach_submitter(self, *, websocket: WebSocketPeer, trace_id: str, reason: str) -> None:
        """Remove task-to-submitter mappings for a disconnected submitter.

        Args:
            websocket: Submitter websocket being disconnected.
            trace_id: Trace id used for controller event correlation.
            reason: Disconnect reason stored in event payloads.
        """

        detached_task_ids = [
            task_id for task_id, submitter in self.submitter_by_task_id.items() if submitter is websocket
        ]
        for task_id in detached_task_ids:
            self.submitter_by_task_id.pop(task_id, None)
            task_row, session_row = await controller_store_read(
                self.queue.store.path,
                lambda store: (store.task_row(task_id), store.session_row_for_task(task_id)),
            )
            orphaned = False
            if task_row is not None and session_row is not None:
                orphaned = await self.queue.terminalize_pending_orphan_async(
                    build_interrupted_task(
                        task_id=task_id,
                        trace_id=str(task_row["trace_id"]),
                        session_id=str(session_row["session_id"]),
                        error=reason,
                    ),
                    trace_id=trace_id,
                    reason=reason,
                )
            self.events.write(
                "task_submitter_detached",
                trace_id=trace_id,
                entity_type="task",
                entity_id=task_id,
                reason=reason,
                orphaned=orphaned,
            )

    async def _release_active_device_leases(self, *, device_id: str, trace_id: str, reason: str) -> None:
        for session_id, lease in list(self.lease_by_session_id.items()):
            if lease.device_id != device_id:
                continue
            release_task = asyncio.create_task(
                self._release_one_active_device_lease(
                    session_id=session_id,
                    lease=lease,
                    trace_id=trace_id,
                    reason=reason,
                )
            )
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                await release_task
                raise

    async def _release_one_active_device_lease(
        self,
        *,
        session_id: str,
        lease: SessionLeaseGranted,
        trace_id: str,
        reason: str,
    ) -> None:
        should_release_registry = await self._persist_disconnected_lease_release(lease)
        if should_release_registry:
            await self._release_registry_after_disconnect(lease.device_id)
        self.lease_by_session_id.pop(session_id, None)
        self.events.write(
            "session_interrupted",
            trace_id=trace_id,
            entity_type="session",
            entity_id=session_id,
            device_id=lease.device_id,
            lease_id=lease.lease_id,
            reason=reason,
        )
        self.events.write(
            "lease_released_after_interrupt",
            trace_id=trace_id,
            entity_type="session",
            entity_id=session_id,
            device_id=lease.device_id,
            lease_id=lease.lease_id,
            reason=reason,
        )

    async def _persist_disconnected_lease_release(self, lease: SessionLeaseGranted) -> bool:
        db_task = asyncio.create_task(
            controller_store_call(
                self.queue.store.path,
                lambda store: _release_active_lease_after_disconnect(store, lease),
                operation_name="lease_release_after_disconnect",
                priority=ControllerStorePriority.CRITICAL,
            )
        )
        try:
            return await asyncio.shield(db_task)
        except asyncio.CancelledError:
            should_release_registry = await db_task
            if should_release_registry:
                await self._release_registry_after_disconnect(lease.device_id)
            raise

    async def _release_registry_after_disconnect(self, device_id: str) -> None:
        release_task = asyncio.create_task(self.leases.scheduler.registry.release_session_async(device_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise


def _interrupt_active_tasks_for_device(
    store: SQLiteControllerStore,
    *,
    device_id: str,
    trace_id: str,
    reason: str,
) -> list[TaskFinished]:
    rows = list(
        store.connection.execute(
            """
            SELECT sessions.session_id, sessions.task_id
            FROM sessions
            JOIN tasks ON tasks.task_id=sessions.task_id
            WHERE sessions.device_id=?
              AND sessions.status IN (?, ?)
              AND tasks.status NOT IN (?, ?, ?)
            """,
            (
                device_id,
                SessionState.LEASED.value,
                SessionState.RUNNING.value,
                "finished",
                "failed",
                "interrupted",
            ),
        )
    )
    interrupted = []
    with store.connection:
        for row in rows:
            payload = build_interrupted_task(
                task_id=str(row["task_id"]),
                trace_id=trace_id,
                session_id=str(row["session_id"]),
                error=reason,
            )
            result_json = compact_task_finished_for_sqlite(payload).model_dump_json()
            changed = store.connection.execute(
                """
                UPDATE tasks
                SET status=?, result_json=?, finished_at=datetime('now')
                WHERE task_id=?
                  AND status NOT IN (?, ?, ?)
                """,
                (
                    payload.state.value,
                    result_json,
                    payload.task_id,
                    "finished",
                    "failed",
                    "interrupted",
                ),
            ).rowcount
            if changed <= 0:
                continue
            store.connection.execute(
                """
                UPDATE sessions
                SET status=?, updated_at=datetime('now')
                WHERE session_id=?
                  AND status IN (?, ?)
                """,
                (
                    SessionState.INTERRUPTED.value,
                    payload.session_id,
                    SessionState.LEASED.value,
                    SessionState.RUNNING.value,
                ),
            )
            interrupted.append(payload)
    return interrupted


def _release_active_lease_after_disconnect(store: SQLiteControllerStore, lease: SessionLeaseGranted) -> bool:
    """Release a disconnected device lease without reopening terminal tasks."""

    lease_row = store.lease_row(lease.lease_id)
    was_active = lease_row is not None and str(lease_row["status"]) == SessionState.LEASED.value
    store.update_lease_status(lease.lease_id, SessionState.RELEASED)
    session = store.session_row(lease.session_id)
    if session is None:
        return was_active
    if str(session["lease_id"] or "") != lease.lease_id:
        return was_active
    if str(session["status"]) in {SessionState.LEASED.value, SessionState.RUNNING.value}:
        store.update_session_status(lease.session_id, SessionState.RELEASED)
    return was_active


__all__ = ["CancelActiveCommands", "RetryQueuedTasks", "TaskLeaseReleaseHandler"]
