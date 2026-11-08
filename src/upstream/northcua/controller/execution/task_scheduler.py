"""Task lease scheduling and queued-task retry helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from northcua.controller.control_plane.connections import ConnectionRegistry, WebSocketPeer
from northcua.controller.control_plane.leases import LeaseManager
from northcua.controller.execution.executor_protocol import ControllerExecutionContext, ControllerTaskExecutor
from northcua.controller.execution.task_interruptions import build_interrupted_task
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import controller_store_read
from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua.controller.storage.queue import TaskQueue
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import SessionLeaseGranted, SessionLeaseReleased, TaskRequest
from northcua_protocol.state import SessionState

logger = logging.getLogger(__name__)
MISSING_SUBMITTER_MAPPING_GRACE_SECONDS = 30.0


class TaskLeaseScheduler:
    """Acquire, deliver, and retry task leases without owning task API state."""

    def __init__(
        self,
        *,
        connections: ConnectionRegistry,
        leases: LeaseManager,
        queue: TaskQueue,
        events: ControllerEventLog,
        executor: ControllerTaskExecutor | None,
        submitter_by_task_id: dict[str, WebSocketPeer],
        lease_by_session_id: dict[str, SessionLeaseGranted],
        blob_store: LocalControllerBlobStore | None = None,
    ) -> None:
        self.connections = connections
        self.leases = leases
        self.queue = queue
        self.events = events
        self.executor = executor
        self.submitter_by_task_id = submitter_by_task_id
        self.lease_by_session_id = lease_by_session_id
        self.blob_store = blob_store
        self._target_connection_missing_event_at: dict[str, float] = {}
        self._missing_submitter_mapping_event_at: dict[str, float] = {}
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_requested = False
        self._retry_trace_id = ""
        self._retry_sweeps_started = 0
        self._retry_triggers_coalesced = 0
        self._retry_sweep_failures = 0
        self._retry_item_failures = 0

    async def try_schedule(
        self,
        *,
        websocket: WebSocketPeer,
        request: TaskRequest,
        session_id: str,
        correlation_id: str,
    ) -> None:
        """Attempt one lease delivery and queue the task on backpressure.

        Args:
            websocket: Submitter websocket that owns the task.
            request: Materialized task request to schedule.
            session_id: Controller session id for the task.
            correlation_id: Envelope id used when delivering the lease.
        """

        connection = self.connections.find(websocket)
        if connection is None:
            await self.queue.mark_queued_async(request, session_id=session_id, reason="submitter connection missing")
            return
        if request.target_device_id and self.connections.for_device(request.target_device_id) is None:
            if correlation_id != "queued-task-retry":
                await self.queue.mark_queued_async(
                    request,
                    session_id=session_id,
                    reason="target device connection missing",
                )
            self._write_target_connection_waiting_event(request=request, session_id=session_id)
            return
        try:
            lease = await self.leases.acquire_for_task_async(
                request,
                session_id=session_id,
                preferred_device_id=connection.device_id,
                create_task=False,
            )
        except RuntimeError as exc:
            await self.queue.mark_queued_async(request, session_id=session_id, reason=str(exc))
            return
        await self._deliver_lease(
            lease=lease,
            request=request,
            session_id=session_id,
            correlation_id=correlation_id,
        )

    async def retry_queued(self, *, trace_id: str) -> None:
        """Try scheduling accepted tasks that were previously backpressured.

        Args:
            trace_id: Trace id used for retry skip and scheduling events.
        """

        self._retry_requested = True
        self._retry_trace_id = trace_id
        task = self._retry_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._run_retry_sweeps(),
                name="northcua-controller-retry-queued",
            )
            self._retry_task = task
        else:
            self._retry_triggers_coalesced += 1
        await asyncio.shield(task)

    async def _run_retry_sweeps(self) -> None:
        while self._retry_requested:
            self._retry_requested = False
            trace_id = self._retry_trace_id
            self._retry_sweeps_started += 1
            try:
                await self._retry_queued_once(trace_id=trace_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._retry_sweep_failures += 1
                logger.warning(
                    "controller queued-task retry sweep failed; preserving coalesced retry",
                    extra={"trace_id": trace_id, "retry_requested": self._retry_requested},
                    exc_info=True,
                )

    async def drain_retry_queued(self) -> None:
        """Wait for an admitted shared retry sweep during controller shutdown."""

        task = self._retry_task
        if task is not None:
            await asyncio.shield(task)

    def retry_snapshot(self) -> dict[str, int | bool]:
        """Return bounded single-flight counters for runtime telemetry."""

        task = self._retry_task
        return {
            "active": task is not None and not task.done(),
            "requested": self._retry_requested,
            "sweeps_started": self._retry_sweeps_started,
            "triggers_coalesced": self._retry_triggers_coalesced,
            "sweep_failures": self._retry_sweep_failures,
            "item_failures": self._retry_item_failures,
        }

    async def _retry_queued_once(self, *, trace_id: str) -> None:
        rows = await controller_store_read(
            self.queue.store.path,
            lambda store: store.schedulable_task_rows(),
        )
        for row in rows:
            request = TaskRequest.model_validate_json(row["request_json"])
            submitter = self.submitter_by_task_id.get(request.task_id)
            if submitter is None:
                submitter = self._recover_submitter_mapping(request=request, row=row, trace_id=trace_id)
            if submitter is None:
                row_age_seconds = _row_age_seconds(row)
                if row_age_seconds is not None and row_age_seconds < MISSING_SUBMITTER_MAPPING_GRACE_SECONDS:
                    self._write_missing_submitter_mapping_grace_event(
                        request=request,
                        session_id=row["session_id"],
                        trace_id=trace_id,
                        age_seconds=row_age_seconds,
                    )
                    continue
                await self._terminalize_missing_submitter(
                    request=request,
                    session_id=row["session_id"],
                    trace_id=trace_id,
                    reason="queued task lost its submitter mapping",
                )
                continue
            if self.connections.find(submitter) is None:
                self.submitter_by_task_id.pop(request.task_id, None)
                await self._terminalize_missing_submitter(
                    request=request,
                    session_id=row["session_id"],
                    trace_id=trace_id,
                    reason="queued task submitter websocket is disconnected",
                )
                continue
            if request.target_device_id and self.connections.for_device(request.target_device_id) is None:
                self._write_target_connection_waiting_event(request=request, session_id=row["session_id"])
                continue
            try:
                await self.try_schedule(
                    websocket=submitter,
                    request=request,
                    session_id=row["session_id"],
                    correlation_id="queued-task-retry",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._retry_item_failures += 1
                logger.warning(
                    "controller queued-task retry failed for one task; continuing sweep",
                    extra={
                        "task_id": request.task_id,
                        "session_id": row["session_id"],
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )

    def _recover_submitter_mapping(
        self,
        *,
        request: TaskRequest,
        row: Any,
        trace_id: str,
    ) -> WebSocketPeer | None:
        submitter_id = str(row["submitter_id"] or "")
        if not submitter_id:
            return None
        connection = self.connections.for_submitter(submitter_id)
        if connection is None:
            return None
        self.submitter_by_task_id[request.task_id] = connection.websocket
        self.events.write(
            "task_submitter_mapping_recovered",
            trace_id=trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=row["session_id"],
            submitter_id=submitter_id,
        )
        return connection.websocket

    async def _terminalize_missing_submitter(
        self,
        *,
        request: TaskRequest,
        session_id: str,
        trace_id: str,
        reason: str,
    ) -> None:
        await self.queue.terminalize_pending_orphan_async(
            build_interrupted_task(
                task_id=request.task_id,
                trace_id=request.trace_id,
                session_id=session_id,
                error=reason,
            ),
            trace_id=trace_id,
            reason=reason,
        )

    def _write_missing_submitter_mapping_grace_event(
        self,
        *,
        request: TaskRequest,
        session_id: str,
        trace_id: str,
        age_seconds: float,
    ) -> None:
        now = time.monotonic()
        last = self._missing_submitter_mapping_event_at.get(request.task_id, 0.0)
        if now - last < 30.0:
            return
        self._missing_submitter_mapping_event_at[request.task_id] = now
        self.events.write(
            "task_retry_missing_submitter_mapping_grace",
            trace_id=trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            age_seconds=round(age_seconds, 3),
            grace_seconds=MISSING_SUBMITTER_MAPPING_GRACE_SECONDS,
        )

    def _write_target_connection_waiting_event(self, *, request: TaskRequest, session_id: str) -> None:
        now = time.monotonic()
        last = self._target_connection_missing_event_at.get(request.task_id, 0.0)
        if now - last < 30.0:
            return
        self._target_connection_missing_event_at[request.task_id] = now
        self.events.write(
            "task_retry_waiting_target_connection",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            target_device_id=request.target_device_id,
        )

    async def _deliver_lease(
        self,
        *,
        lease: SessionLeaseGranted,
        request: TaskRequest,
        session_id: str,
        correlation_id: str,
    ) -> None:
        self.lease_by_session_id[session_id] = lease
        delivery_task = asyncio.create_task(
            self._deliver_lease_after_registration(
                lease=lease,
                request=request,
                session_id=session_id,
                correlation_id=correlation_id,
            )
        )
        try:
            await asyncio.shield(delivery_task)
        except asyncio.CancelledError:
            await delivery_task
            raise

    async def _deliver_lease_after_registration(
        self,
        *,
        lease: SessionLeaseGranted,
        request: TaskRequest,
        session_id: str,
        correlation_id: str,
    ) -> None:
        device_connection = self.connections.for_device(lease.device_id)
        if device_connection is None:
            await self._rollback_undelivered(lease, request, session_id, reason="device connection missing")
            return
        try:
            await device_connection.websocket.send(
                Envelope.wrap(
                    MessageType.SESSION_LEASE_GRANTED,
                    lease,
                    trace_id=request.trace_id,
                    correlation_id=correlation_id,
                ).model_dump_json()
            )
        except Exception:
            logger.warning(
                "controller failed to deliver session lease to device",
                extra={
                    "task_id": request.task_id,
                    "session_id": session_id,
                    "device_id": lease.device_id,
                    "lease_id": lease.lease_id,
                },
                exc_info=True,
            )
            await self._rollback_undelivered(lease, request, session_id, reason="device websocket send failed")
            raise
        running = await self.queue.mark_running_async(
            task_id=request.task_id,
            trace_id=request.trace_id,
            session_id=session_id,
            device_id=lease.device_id,
        )
        if not running:
            await self._release_delivered_nonrunning_lease(
                lease=lease,
                request=request,
                reason="task reached terminal state before runner scheduling",
            )
            return
        if self.executor is None:
            logger.info(
                "controller delivered lease without task executor",
                extra={
                    "task_id": request.task_id,
                    "session_id": session_id,
                    "device_id": lease.device_id,
                    "agent_kind": request.agent_kind,
                },
            )
            self.events.write(
                "task_runner_unavailable",
                trace_id=request.trace_id,
                entity_type="task",
                entity_id=request.task_id,
                session_id=session_id,
                device_id=lease.device_id,
                agent_kind=request.agent_kind,
            )
            return
        logger.info(
            "controller scheduling task runner",
            extra={
                "task_id": request.task_id,
                "session_id": session_id,
                "device_id": lease.device_id,
                "agent_kind": request.agent_kind,
            },
        )
        self.events.write(
            "task_runner_scheduled",
            trace_id=request.trace_id,
            entity_type="task",
            entity_id=request.task_id,
            session_id=session_id,
            device_id=lease.device_id,
            agent_kind=request.agent_kind,
        )

        async def _send_progress(message_type, payload) -> None:
            submitter = self.submitter_by_task_id.get(request.task_id)
            if submitter is None or self.connections.find(submitter) is None:
                return
            await submitter.send(
                Envelope.wrap(
                    message_type,
                    payload,
                    trace_id=request.trace_id,
                    correlation_id=f"task-progress-{request.task_id}",
                ).model_dump_json()
            )

        await self.executor.schedule(
            ControllerExecutionContext(
                request=request,
                session_id=session_id,
                lease=lease,
                blob_store_root=self.blob_store.root if self.blob_store is not None else None,
                progress_sink=_send_progress,
            )
        )

    async def _release_delivered_nonrunning_lease(
        self,
        *,
        lease: SessionLeaseGranted,
        request: TaskRequest,
        reason: str,
    ) -> None:
        self.lease_by_session_id.pop(lease.session_id, None)
        release = SessionLeaseReleased(
            lease_id=lease.lease_id,
            session_id=lease.session_id,
            device_id=lease.device_id,
            state=SessionState.RELEASED,
            reason=reason,
        )
        await self.leases.release_async(release, trace_id=request.trace_id)
        device_connection = self.connections.for_device(lease.device_id)
        if device_connection is None:
            return
        try:
            await device_connection.websocket.send(
                Envelope.wrap(MessageType.SESSION_LEASE_RELEASED, release, trace_id=request.trace_id).model_dump_json()
            )
        except Exception:
            logger.warning(
                "controller failed to release non-running delivered lease",
                extra={
                    "task_id": request.task_id,
                    "session_id": lease.session_id,
                    "device_id": lease.device_id,
                    "lease_id": lease.lease_id,
                },
                exc_info=True,
            )

    async def _rollback_undelivered(
        self,
        lease: SessionLeaseGranted,
        request: TaskRequest,
        session_id: str,
        *,
        reason: str,
    ) -> None:
        self.lease_by_session_id.pop(session_id, None)
        task_requeued = await self.leases.rollback_undelivered_async(lease, trace_id=request.trace_id, reason=reason)
        queue_reason = "device lease delivery failed" if reason == "device websocket send failed" else reason
        if task_requeued:
            await self.queue.mark_queued_async(request, session_id=session_id, reason=queue_reason)


def _row_age_seconds(row: Any) -> float | None:
    created_at = str(row["created_at"] or "")
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
