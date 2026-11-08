"""Task orchestration service for controller websocket requests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from northcua.controller.auth import ControllerAuth
from northcua.controller.control_plane.connections import ConnectionRegistry, WebSocketPeer
from northcua.controller.control_plane.leases import LeaseManager
from northcua.controller.control_plane.registry import DeviceRegistry
from northcua.controller.execution.executor_protocol import ControllerTaskExecutor
from northcua.controller.execution.task_artifacts import TaskArtifactMaterializer
from northcua.controller.execution.task_scheduler import TaskLeaseScheduler
from northcua.controller.execution.task_terminal import (
    TaskLeaseReleaseHandler,
    TaskTerminalCompleter,
)
from northcua.controller.execution.task_terminal import build_interrupted_task as build_interrupted_payload
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua.controller.storage.queue import TaskQueue
from northcua_protocol.capabilities import Capability
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import (
    SessionLeaseGranted,
    SessionLeaseReleased,
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskRequest,
)

logger = logging.getLogger(__name__)


class TaskService:
    """Own task acceptance, scheduling, lease delivery, and terminal updates."""

    def __init__(
        self,
        *,
        auth: ControllerAuth,
        connections: ConnectionRegistry,
        leases: LeaseManager,
        queue: TaskQueue,
        events: ControllerEventLog,
        blob_store: LocalControllerBlobStore,
        registry: DeviceRegistry,
        executor: ControllerTaskExecutor | None = None,
        cancel_active_commands: Callable[[str, str, str], None | Awaitable[None]] | None = None,
    ) -> None:
        self.auth = auth
        self.connections = connections
        self.leases = leases
        self.queue = queue
        self.events = events
        self.blob_store = blob_store
        self.registry = registry
        self.executor = executor
        self.cancel_active_commands = cancel_active_commands
        self._submitter_by_task_id: dict[str, WebSocketPeer] = {}
        self._lease_by_session_id: dict[str, SessionLeaseGranted] = {}
        self._artifacts = TaskArtifactMaterializer(auth=auth, blob_store=blob_store)
        self._scheduler = TaskLeaseScheduler(
            connections=connections,
            leases=leases,
            queue=queue,
            events=events,
            executor=executor,
            submitter_by_task_id=self._submitter_by_task_id,
            lease_by_session_id=self._lease_by_session_id,
            blob_store=blob_store,
        )
        self._lease_releases = TaskLeaseReleaseHandler(
            connections=connections,
            leases=leases,
            queue=queue,
            events=events,
            submitter_by_task_id=self._submitter_by_task_id,
            lease_by_session_id=self._lease_by_session_id,
            cancel_active_commands=cancel_active_commands,
            retry_queued_tasks=self.retry_queued_tasks,
        )
        self._terminal = TaskTerminalCompleter(
            connections=connections,
            queue=queue,
            events=events,
            artifacts=self._artifacts,
            submitter_by_task_id=self._submitter_by_task_id,
            lease_by_session_id=self._lease_by_session_id,
            cancel_active_commands=cancel_active_commands,
            lease_releases=self._lease_releases,
        )

    def set_executor(self, executor: ControllerTaskExecutor | None) -> None:
        """Install or clear the controller-owned task executor.

        The scheduler owns the moment where a task lease becomes runnable, so
        executor changes must update both this service facade and the scheduler
        collaborator. Controller startup calls this after runner plugins are
        loaded; tests and control-plane-only deployments may leave it unset.

        Args:
            executor: Executor service that starts agent runners after a lease
                is delivered, or ``None`` to run as a control-plane-only server.
        """

        self.executor = executor
        self._scheduler.executor = executor

    async def accept_task(
        self,
        *,
        websocket: WebSocketPeer,
        request: TaskRequest,
        correlation_id: str,
    ) -> TaskAccepted:
        """Accept and try to schedule a task request from any submitter.

        Args:
            websocket: Submitter or self-submitting device websocket.
            request: Task request to authenticate, persist, and schedule.
            correlation_id: Envelope id used to correlate accept/lease messages.

        Returns:
            Accepted task payload sent back to the submitter.

        Raises:
            PermissionError: If auth, ownership, or capability checks fail.
            ValueError: If metadata or artifacts violate controller limits.
        """

        connection = self.connections.get(websocket)
        skip_submitter_token = False
        effective_request = self._with_agentkit_capability_floor(request) if self.executor is not None else request
        if connection.device_id:
            device = self.registry.get(connection.device_id)
            advertised_capabilities = set(device.descriptor.capabilities) if device is not None else set()
            self.auth.validate_device_self_submission(
                effective_request,
                device_id=connection.device_id,
                device_capabilities=advertised_capabilities,
            )
            skip_submitter_token = True
            if not effective_request.target_device_id:
                effective_request = effective_request.model_copy(update={"target_device_id": connection.device_id})
        self.auth.validate_task_request(effective_request, skip_submitter_token=skip_submitter_token)
        submitter_id = (
            connection.submitter_id or effective_request.metadata.get("submitter_id") or f"submitter-{uuid.uuid4().hex}"
        )
        materialized_artifacts = (
            await asyncio.to_thread(self._artifacts.materialize, effective_request.input_artifacts)
            if effective_request.input_artifacts
            else []
        )
        materialized_request = effective_request.model_copy(
            update={
                "token": "",
                "input_artifacts": materialized_artifacts,
            }
        )
        session_id = uuid.uuid4().hex
        previous_submitter = self._submitter_by_task_id.get(materialized_request.task_id)
        preregistered_submitter = previous_submitter is None
        if preregistered_submitter:
            self._submitter_by_task_id[materialized_request.task_id] = websocket

        def _rollback_submitter_mapping() -> None:
            if (
                not preregistered_submitter
                or self._submitter_by_task_id.get(materialized_request.task_id) is not websocket
            ):
                return
            self._submitter_by_task_id.pop(materialized_request.task_id, None)

        accept_task = asyncio.create_task(
            self.queue.accept_async(
                materialized_request,
                session_id=session_id,
                submitter_id=str(submitter_id),
                input_artifacts=materialized_request.input_artifacts,
            )
        )
        accepted_persisted = False
        schedule_started = False
        try:
            await asyncio.shield(accept_task)
            accepted_persisted = True
            accepted = TaskAccepted(
                task_id=materialized_request.task_id,
                trace_id=materialized_request.trace_id,
                session_id=session_id,
            )
            await websocket.send(
                Envelope.wrap(
                    MessageType.TASK_ACCEPTED,
                    accepted,
                    trace_id=materialized_request.trace_id,
                    correlation_id=correlation_id,
                ).model_dump_json()
            )
            self.connections.mark_submitter(websocket, str(submitter_id))
            schedule_started = True
            await self._scheduler.try_schedule(
                websocket=websocket,
                request=materialized_request,
                session_id=session_id,
                correlation_id=correlation_id,
            )
            return accepted
        except asyncio.CancelledError:
            try:
                if not accept_task.done():
                    await accept_task
                else:
                    accept_task.result()
                accepted_persisted = True
            except BaseException:
                if not schedule_started:
                    _rollback_submitter_mapping()
                raise
            if accepted_persisted and not schedule_started:
                _rollback_submitter_mapping()
                await self.queue.terminalize_pending_orphan_async(
                    build_interrupted_payload(
                        task_id=materialized_request.task_id,
                        trace_id=materialized_request.trace_id,
                        session_id=session_id,
                        error="task acceptance cancelled before scheduling",
                    ),
                    trace_id=materialized_request.trace_id,
                    reason="task acceptance cancelled before scheduling",
                )
            raise
        except Exception:
            if not schedule_started:
                _rollback_submitter_mapping()
                if accepted_persisted:
                    await self.queue.terminalize_pending_orphan_async(
                        build_interrupted_payload(
                            task_id=materialized_request.task_id,
                            trace_id=materialized_request.trace_id,
                            session_id=session_id,
                            error="task acceptance failed before scheduling",
                        ),
                        trace_id=materialized_request.trace_id,
                        reason="task acceptance failed before scheduling",
                    )
            raise

    @staticmethod
    def _with_agentkit_capability_floor(request: TaskRequest) -> TaskRequest:
        """Merge registered AgentKit remote capability requirements into the task."""

        try:
            from northcua.agentkit.registry import load_all_agent_specs
        except Exception:  # pragma: no cover - controller can run without AgentKit extensions
            logger.debug("AgentKit registry unavailable while deriving task capabilities", exc_info=True)
            return request
        spec = load_all_agent_specs().maybe_get(request.agent_kind)
        if spec is None or not spec.remote.enabled:
            return request
        required: set[Capability] = set(request.required_capabilities)
        required.update(spec.remote.required_capabilities)
        if required == request.required_capabilities:
            return request
        return request.model_copy(update={"required_capabilities": required})

    async def finish_task(
        self, *, websocket: WebSocketPeer, payload: TaskFinished, correlation_id: str | None = None
    ) -> None:
        """Persist and forward a terminal task payload.

        Args:
            websocket: Device websocket attempting to finish the task.
            payload: Terminal task payload.
            correlation_id: Optional envelope correlation id.

        Raises:
            PermissionError: If the websocket does not own the active lease.
        """

        await self._terminal.finish_task(websocket=websocket, payload=payload, correlation_id=correlation_id)

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

        await self._terminal.cancel_task(websocket=websocket, payload=payload, correlation_id=correlation_id)
        if self.executor is not None:
            await self.executor.cancel_task(payload.task_id, payload.reason or "task cancelled by submitter")

    async def complete_task(self, *, payload: TaskFinished, correlation_id: str | None = None) -> None:
        """Persist and forward a controller-approved terminal task payload.

        Args:
            payload: Terminal task payload approved by controller or executor.
            correlation_id: Optional envelope correlation id.
        """

        await self._terminal.complete_task(
            payload=payload,
            correlation_id=correlation_id,
            allow_controller_blob_artifacts=True,
        )

    async def release_lease(self, release: SessionLeaseReleased, *, trace_id: str) -> None:
        """Release a lease and notify the owning device when connected.

        Args:
            release: Lease release payload.
            trace_id: Trace id used for controller event correlation.

        Raises:
            PermissionError: If the release payload does not match the active lease.
        """

        await self._lease_releases.release(release, trace_id=trace_id)

    async def interrupt_device(self, *, device_id: str, trace_id: str, reason: str) -> None:
        """Mark known sessions for a disconnected device as interrupted.

        Args:
            device_id: Device whose websocket disconnected or became unhealthy.
            trace_id: Trace id used for controller event correlation.
            reason: Standardized interruption reason for events and task errors.
        """

        interrupted_task_ids = await self._lease_releases.interrupt_device(
            device_id=device_id,
            trace_id=trace_id,
            reason=reason,
        )
        if self.executor is None:
            return
        for task_id in interrupted_task_ids:
            await self.executor.cancel_task(task_id, reason)

    async def detach_submitter(self, *, websocket: WebSocketPeer, trace_id: str, reason: str) -> None:
        """Remove task-to-submitter mappings for a disconnected submitter websocket.

        Args:
            websocket: Submitter websocket being disconnected.
            trace_id: Trace id used for controller event correlation.
            reason: Disconnect reason stored in event payloads.
        """

        await self._lease_releases.detach_submitter(websocket=websocket, trace_id=trace_id, reason=reason)

    async def retry_queued_tasks(self, *, trace_id: str) -> None:
        """Try scheduling accepted tasks that were previously held by backpressure.

        The controller intentionally keeps queued tasks in SQLite as accepted
        tasks with pending sessions. A retry is triggered by device register,
        heartbeat, and lease release events, which is enough for the single
        process v1 controller without a separate scheduler loop.

        Args:
            trace_id: Trace id used for retry skip and scheduling events.
        """

        await self._scheduler.retry_queued(trace_id=trace_id)

    async def drain_retry_queued_tasks(self) -> None:
        """Wait for the shared queued-task retry sweep during shutdown."""

        await self._scheduler.drain_retry_queued()

    def retry_scheduler_snapshot(self) -> dict[str, int | bool]:
        """Return queued-task retry single-flight telemetry."""

        return self._scheduler.retry_snapshot()

    def build_interrupted_task(self, *, task_id: str, trace_id: str, session_id: str, error: str) -> TaskFinished:
        """Build a terminal interrupted task payload.

        Args:
            task_id: Task being interrupted.
            trace_id: Trace id used for controller event correlation.
            session_id: Session associated with the task.
            error: Human-readable interruption reason.

        Returns:
            Terminal interrupted task payload.
        """

        return build_interrupted_payload(task_id=task_id, trace_id=trace_id, session_id=session_id, error=error)
