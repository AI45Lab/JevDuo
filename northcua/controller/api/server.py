"""Websocket controller server for product-grade remote device workers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.exceptions import ConnectionClosed, InvalidMessage
from websockets.http11 import Request, Response

from northcua.controller.api.command_waits import PendingCommandResult, PendingCommandResultRegistry
from northcua.controller.api.dispatch import ControllerEnvelopeDispatcher
from northcua.controller.auth import ControllerAuth
from northcua.controller.config import ControllerConfig
from northcua.controller.control_plane.connections import ConnectionRegistry
from northcua.controller.control_plane.leases import LeaseManager
from northcua.controller.control_plane.registry import DeviceRegistry
from northcua.controller.control_plane.scheduler import Scheduler
from northcua.controller.execution.command_bus import CommandBus
from northcua.controller.execution.executor_protocol import ControllerTaskExecutor
from northcua.controller.execution.sdk import ControllerSDK
from northcua.controller.execution.tasks import TaskService
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import controller_store_drain, controller_store_open
from northcua.controller.storage.blob_store import LocalControllerBlobStore
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua.controller.storage.queue import TaskQueue
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope, parse_envelope
from northcua_protocol.errors import ErrorCode
from northcua_protocol.payloads import CommandRequest, CommandResult, DeviceHello, ErrorPayload

logger = logging.getLogger(__name__)
websocket_logger = logging.getLogger(f"{__name__}.websockets")
websocket_logger.setLevel(logging.INFO)


class _WebsocketServerNoiseFilter(logging.Filter):
    """Drop noisy probe disconnects while preserving real websocket errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a websocket log record should be emitted.

        Args:
            record: Log record produced by the websockets server logger.

        Returns:
            False only for known HTTP probe disconnect noise.
        """

        if record.getMessage() != "opening handshake failed":
            return True
        exc_info = record.exc_info
        if not exc_info or not isinstance(exc_info[1], InvalidMessage):
            return True
        return "did not receive a valid HTTP request" not in str(exc_info[1])


if not any(isinstance(log_filter, _WebsocketServerNoiseFilter) for log_filter in websocket_logger.filters):
    websocket_logger.addFilter(_WebsocketServerNoiseFilter())


class ControllerServer:
    """Single-process controller websocket server.

    The server is intentionally a thin websocket adapter around control-plane
    services. It owns connection cleanup and wire routing, while scheduling,
    leases, command dispatch, persistence, and task completion live in
    dedicated controller services.
    """

    def __init__(self, config: ControllerConfig, *, executor: ControllerTaskExecutor | None = None) -> None:
        self.config = config
        self.store = SQLiteControllerStore(config.db_path)
        self.events = ControllerEventLog(
            self.store,
            command_stage_event_mode=config.command_stage_event_mode,
            command_stage_event_sample_rate=config.command_stage_event_sample_rate,
            event_write_batch_size=config.event_write_batch_size,
            event_write_flush_interval_seconds=config.event_write_flush_interval_seconds,
        )
        self.auth = ControllerAuth(config.security)
        self.blob_store = LocalControllerBlobStore(config.blob_dir)
        self.registry = DeviceRegistry(
            store=self.store,
            events=self.events,
            heartbeat_persist_interval_seconds=self.config.heartbeat_persist_interval_seconds,
        )
        self.scheduler = Scheduler(self.registry)
        self._operation_timings = _ControllerOperationTimingTracker()
        self.leases = LeaseManager(
            store=self.store,
            scheduler=self.scheduler,
            events=self.events,
            timing_observer=self._operation_timings.observe,
        )
        self.queue = TaskQueue(store=self.store, events=self.events)
        self.connections = ConnectionRegistry()
        self.command_results = PendingCommandResultRegistry(
            store=self.store,
            connections=self.connections,
            events=self.events,
        )
        self._pending_command_results: dict[str, PendingCommandResult | asyncio.Future[CommandResult]] = (
            self.command_results.pending_results
        )
        self._device_disconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._device_disconnect_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._device_disconnect_cleanup_failures: dict[str, str] = {}
        self.command_bus = CommandBus(
            store=self.store,
            auth=self.auth,
            events=self.events,
            registry=self.registry,
            blob_store=self.blob_store,
            max_inline_artifact_bytes=config.security.max_inline_artifact_bytes,
            command_timeout_floor_seconds=config.command_timeout_floor_seconds,
        )
        self.task_service = TaskService(
            auth=self.auth,
            connections=self.connections,
            leases=self.leases,
            queue=self.queue,
            events=self.events,
            blob_store=self.blob_store,
            registry=self.registry,
            executor=executor,
            cancel_active_commands=self._cancel_pending_commands_for_session,
        )
        self.sdk = ControllerSDK(task_service=self.task_service, leases=self.leases, command_bus=self.command_bus)
        self.executor = executor
        self._server: Server | None = None
        self._runtime_telemetry_task: asyncio.Task[None] | None = None
        self._handshake_by_connection: dict[object, dict[str, object]] = {}
        self._device_telemetry_ingress_lag = _DeviceTelemetryIngressLagTracker()
        self._dispatcher = ControllerEnvelopeDispatcher(
            auth=self.auth,
            connections=self.connections,
            registry=self.registry,
            command_bus=self.command_bus,
            task_service=self.task_service,
            store=self.store,
            events=self.events,
            command_results=self.command_results,
            cancel_device_disconnect_cleanup=self._cancel_device_disconnect_cleanup,
            device_telemetry_observer=self._device_telemetry_ingress_lag.observe,
            controller_operation_observer=self._operation_timings.observe,
            device_telemetry_persist_mode=config.device_telemetry_persist_mode,
            device_telemetry_sample_rate=config.device_telemetry_sample_rate,
        )

    async def start(self) -> None:
        """Start the controller websocket server."""

        controller_store_open(self.config.db_path)
        self._server = await websockets.serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            max_size=self.config.security.max_envelope_bytes,
            open_timeout=self.config.websocket_open_timeout_seconds,
            ping_interval=self.config.websocket_ping_interval_seconds,
            ping_timeout=self.config.websocket_ping_timeout_seconds,
            backlog=max(1, int(self.config.websocket_backlog)),
            logger=websocket_logger,
            process_request=self._record_handshake_started,
            process_response=self._record_handshake_response,
        )
        if self.config.runtime_telemetry_interval_seconds > 0:
            self._runtime_telemetry_task = asyncio.create_task(
                self._emit_runtime_telemetry(),
                name="northcua-controller-runtime-telemetry",
            )
        logger.info("NorthCUA controller listening on ws://%s:%s", self.config.host, self.config.port)

    async def stop(self) -> None:
        """Stop the controller server."""

        errors: list[BaseException] = []

        async def _attempt(awaitable: Awaitable[object]) -> None:
            try:
                await awaitable
            except BaseException as exc:
                errors.append(exc)

        async def _attempt_uninterruptibly(awaitable: Awaitable[object]) -> None:
            task = asyncio.ensure_future(awaitable)
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    errors.append(exc)
                except BaseException as exc:
                    errors.append(exc)
                    return
            if task.cancelled():
                errors.append(asyncio.CancelledError())
                return
            exception = task.exception()
            if exception is not None:
                errors.append(exception)

        async def _cancel_and_wait_task(task: asyncio.Task[object]) -> None:
            current = asyncio.current_task()
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    if current is not None and current.cancelling() > 0:
                        errors.append(exc)
                        current.uncancel()
                except BaseException as exc:
                    errors.append(exc)
                    return
            if not task.cancelled():
                exception = task.exception()
                if exception is not None:
                    errors.append(exception)

        if self._server is not None:
            self._server.close()
            await _attempt(self._server.wait_closed())
            self._server = None
        if self._runtime_telemetry_task is not None:
            await _cancel_and_wait_task(self._runtime_telemetry_task)
            self._runtime_telemetry_task = None
        await _attempt_uninterruptibly(self.task_service.drain_retry_queued_tasks())
        await _attempt_uninterruptibly(self.registry.aclose())
        if self.executor is not None:
            await _attempt(self.executor.drain())
        for task in self._device_disconnect_tasks.values():
            task.cancel()
        if self._device_disconnect_tasks:
            await _attempt(asyncio.gather(*self._device_disconnect_tasks.values(), return_exceptions=True))
            self._device_disconnect_tasks.clear()
        if self._device_disconnect_cleanup_tasks:
            await _attempt(asyncio.gather(*self._device_disconnect_cleanup_tasks.values(), return_exceptions=True))
            self._device_disconnect_cleanup_tasks.clear()
        try:
            self.events.close()
        except BaseException as exc:
            errors.append(exc)
        await _attempt_uninterruptibly(controller_store_drain(self.config.db_path))
        try:
            self.store.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise errors[0]

    async def serve_forever(self) -> None:
        """Run the controller until the caller cancels the task.

        This helper is mainly for tests and embedding; the CLI uses `start()`
        plus signal handling so it can drain background task executors.
        """

        await self.start()
        await asyncio.Future()

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """Route envelopes for one websocket and perform disconnect cleanup.

        Device and submitter connections share this adapter; role is inferred by
        the first authenticated protocol envelope and then cleaned up here when
        the websocket closes.

        Args:
            websocket: Accepted websocket connection from a device or submitter.
        """

        handler_started_at = time.perf_counter()
        connection = self.connections.ensure(websocket)
        handshake = self._handshake_by_connection.pop(websocket, {})
        handshake_payload = _handshake_metric_payload(handshake, handler_started_at)
        connection.websocket_open_handshake_ms = _payload_int(handshake_payload, "websocket_open_handshake_ms", -1)
        connection.websocket_handler_start_delay_ms = _payload_int(
            handshake_payload,
            "websocket_handler_start_delay_ms",
            -1,
        )
        message_count = 0
        bytes_received = 0
        first_envelope_ms: int | None = None
        trace_id = uuid.uuid4().hex
        self.events.write(
            "websocket_handler_started",
            trace_id=trace_id,
            entity_type="connection",
            entity_id=connection.connection_id,
            **handshake_payload,
            remote_address=_stringify_remote_address(getattr(websocket, "remote_address", "")),
            path=str(handshake.get("path", "")),
            **self.connections.snapshot(),
        )
        try:
            try:
                async for raw in websocket:
                    try:
                        envelope_started_at = time.perf_counter()
                        raw_size = len(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
                        message_count += 1
                        bytes_received += raw_size
                        if raw_size > self.config.security.max_envelope_bytes:
                            await websocket.send(
                                Envelope.wrap(
                                    MessageType.ERROR,
                                    ErrorPayload(
                                        code=ErrorCode.PAYLOAD_TOO_LARGE,
                                        message="Envelope exceeds controller max_envelope_bytes.",
                                    ),
                                    trace_id=uuid.uuid4().hex,
                                ).model_dump_json()
                            )
                            continue
                        envelope = parse_envelope(raw)
                        if first_envelope_ms is None:
                            first_envelope_ms = int((time.perf_counter() - handler_started_at) * 1000)
                            connection.first_envelope_ms = first_envelope_ms
                            connection.first_envelope_type = envelope.type.value
                            self.events.write(
                                "websocket_first_envelope_received",
                                trace_id=envelope.trace_id,
                                entity_type="connection",
                                entity_id=connection.connection_id,
                                message_type=envelope.type.value,
                                first_envelope_ms=first_envelope_ms,
                                raw_size=raw_size,
                                message_count=message_count,
                            )
                        await self._dispatch(websocket, envelope)
                        dispatch_ms = int((time.perf_counter() - envelope_started_at) * 1000)
                        if dispatch_ms >= 1000:
                            self.events.write(
                                "websocket_envelope_slow",
                                trace_id=envelope.trace_id,
                                entity_type="connection",
                                entity_id=connection.connection_id,
                                message_type=envelope.type.value,
                                dispatch_ms=dispatch_ms,
                                raw_size=raw_size,
                            )
                    except Exception as exc:
                        logger.warning("controller failed to dispatch envelope", exc_info=True)
                        await websocket.send(
                            Envelope.wrap(
                                MessageType.ERROR,
                                ErrorPayload(code=ErrorCode.INTERNAL_ERROR, message=str(exc)),
                                trace_id=uuid.uuid4().hex,
                            ).model_dump_json()
                        )
            except ConnectionClosed:
                logger.debug("controller websocket disconnected without close frame", exc_info=True)
        finally:
            connection = self.connections.unregister(websocket)
            trace_id = uuid.uuid4().hex
            close_payload = _websocket_close_payload(websocket)
            if connection is not None:
                self.events.write(
                    "websocket_handler_finished",
                    trace_id=trace_id,
                    entity_type="connection",
                    entity_id=connection.connection_id,
                    handler_elapsed_ms=int((time.perf_counter() - handler_started_at) * 1000),
                    message_count=message_count,
                    bytes_received=bytes_received,
                    first_envelope_ms=first_envelope_ms if first_envelope_ms is not None else -1,
                    final_role=connection.role.value,
                    device_id=connection.device_id,
                    submitter_id=connection.submitter_id,
                    **close_payload,
                    **self.connections.snapshot(),
                )
            if connection and connection.submitter_id:
                await self.task_service.detach_submitter(
                    websocket=websocket,
                    trace_id=trace_id,
                    reason="submitter websocket disconnected",
                )
            if connection and connection.device_id:
                self._schedule_device_disconnect_cleanup(
                    device_id=connection.device_id,
                    trace_id=trace_id,
                    reason="websocket disconnected",
                )

    async def _dispatch(self, websocket: ServerConnection, envelope: Envelope) -> None:
        """Dispatch one parsed protocol envelope to the owning service.

        Args:
            websocket: Websocket that sent the envelope.
            envelope: Parsed protocol envelope.

        Raises:
            PermissionError: If the sender lacks authority for the payload.
        """

        if envelope.type == MessageType.DEVICE_HELLO and isinstance(envelope.payload, DeviceHello):
            await self._wait_for_device_disconnect_cleanup(envelope.payload.descriptor.device_id, envelope.trace_id)
        await self._dispatcher.dispatch(websocket, envelope)

    def _record_handshake_started(self, connection: ServerConnection, request: Request) -> None:
        self._handshake_by_connection[connection] = {
            "started_at": time.perf_counter(),
            "path": str(getattr(request, "path", "") or ""),
        }
        return None

    def _record_handshake_response(
        self,
        connection: ServerConnection,
        request: Request,
        response: Response,
    ) -> Response:
        handshake = self._handshake_by_connection.setdefault(
            connection,
            {"started_at": time.perf_counter(), "path": str(getattr(request, "path", "") or "")},
        )
        started_at = handshake.get("started_at")
        if isinstance(started_at, float):
            handshake["response_ms"] = int((time.perf_counter() - started_at) * 1000)
        status_code = int(getattr(response, "status_code", 0) or 0)
        handshake["response_status_code"] = status_code
        if status_code and status_code != 101:
            self.events.write(
                "websocket_handshake_rejected",
                trace_id=uuid.uuid4().hex,
                entity_type="connection",
                entity_id=uuid.uuid4().hex,
                **_handshake_metric_payload(handshake, time.perf_counter()),
                remote_address=_stringify_remote_address(getattr(connection, "remote_address", "")),
                path=str(handshake.get("path", "")),
            )
            self._handshake_by_connection.pop(connection, None)
        return response

    async def _emit_runtime_telemetry(self) -> None:
        interval = max(0.1, float(self.config.runtime_telemetry_interval_seconds))
        loop = asyncio.get_running_loop()
        expected_at = loop.time() + interval
        while True:
            await asyncio.sleep(max(0.0, expected_at - loop.time()))
            observed_at = loop.time()
            event_loop_lag_ms = max(0, int((observed_at - expected_at) * 1000))
            expected_at = observed_at + interval
            socket_metrics = await asyncio.to_thread(_tcp_socket_queue_metrics, self.config.port)
            self.events.write(
                "controller_runtime_telemetry",
                trace_id=uuid.uuid4().hex,
                entity_type="controller",
                entity_id=f"{self.config.host}:{self.config.port}",
                event_loop_lag_ms=event_loop_lag_ms,
                pending_command_waiters=len(self.command_results.pending_results),
                device_disconnect_grace_tasks=len(self._device_disconnect_tasks),
                device_telemetry_ingress_lag=self._device_telemetry_ingress_lag.drain_snapshot(),
                device_heartbeat_persistence=self.registry.heartbeat_persistence_snapshot(),
                event_writer_persistence=self.events.snapshot(),
                retry_scheduler=self.task_service.retry_scheduler_snapshot(),
                controller_operation_timings=self._operation_timings.drain_snapshot(),
                **self.connections.snapshot(),
                **socket_metrics,
            )
            self.events.flush(wait=False)

    def _schedule_device_disconnect_cleanup(self, *, device_id: str, trace_id: str, reason: str) -> None:
        grace_seconds = max(0.0, float(self.config.device_disconnect_grace_seconds))
        existing = self._device_disconnect_tasks.pop(device_id, None)
        if existing is not None:
            existing.cancel()
        if grace_seconds <= 0:
            self._start_device_disconnect_cleanup(device_id=device_id, trace_id=trace_id, reason=reason)
            return
        self.events.write(
            "device_disconnect_grace_started",
            trace_id=trace_id,
            entity_type="device",
            entity_id=device_id,
            grace_seconds=grace_seconds,
            reason=reason,
        )
        task = asyncio.create_task(
            self._cleanup_device_disconnect_after_grace(
                device_id=device_id,
                trace_id=trace_id,
                reason=reason,
                grace_seconds=grace_seconds,
            ),
            name=f"northcua-device-disconnect-grace:{device_id}",
        )
        self._device_disconnect_tasks[device_id] = task

    def _cancel_device_disconnect_cleanup(self, device_id: str, trace_id: str) -> None:
        task = self._device_disconnect_tasks.pop(device_id, None)
        if task is None:
            return
        task.cancel()
        self.events.write(
            "device_disconnect_grace_recovered",
            trace_id=trace_id,
            entity_type="device",
            entity_id=device_id,
        )

    async def _wait_for_device_disconnect_cleanup(self, device_id: str, trace_id: str) -> None:
        cleanup_task = self._device_disconnect_cleanup_tasks.get(device_id)
        if cleanup_task is not None and not cleanup_task.done():
            self.events.write(
                "device_disconnect_cleanup_waiting_reconnect",
                trace_id=trace_id,
                entity_type="device",
                entity_id=device_id,
            )
            await asyncio.shield(cleanup_task)
        failure = self._device_disconnect_cleanup_failures.get(device_id)
        if failure is None:
            return
        self.events.write(
            "device_disconnect_cleanup_retrying_reconnect",
            trace_id=trace_id,
            entity_type="device",
            entity_id=device_id,
            previous_error=failure,
        )
        retry_task = self._start_device_disconnect_cleanup(
            device_id=device_id,
            trace_id=trace_id,
            reason="retry failed disconnect cleanup before reconnect",
        )
        await asyncio.shield(retry_task)
        failure = self._device_disconnect_cleanup_failures.get(device_id)
        if failure is None:
            return
        self.events.write(
            "device_disconnect_cleanup_reconnect_rejected",
            trace_id=trace_id,
            entity_type="device",
            entity_id=device_id,
            error=failure,
        )
        raise PermissionError("device disconnect cleanup failed; reconnect rejected")

    async def _cleanup_device_disconnect_after_grace(
        self,
        *,
        device_id: str,
        trace_id: str,
        reason: str,
        grace_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(grace_seconds)
            if self.connections.for_device(device_id) is not None:
                self.events.write(
                    "device_disconnect_grace_recovered",
                    trace_id=trace_id,
                    entity_type="device",
                    entity_id=device_id,
                )
                return
            current_task = asyncio.current_task()
            if self._device_disconnect_tasks.get(device_id) is current_task:
                self._device_disconnect_tasks.pop(device_id, None)
            cleanup_task = self._start_device_disconnect_cleanup(
                device_id=device_id,
                trace_id=trace_id,
                reason=reason,
            )
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            raise
        finally:
            if self._device_disconnect_tasks.get(device_id) is asyncio.current_task():
                self._device_disconnect_tasks.pop(device_id, None)

    def _start_device_disconnect_cleanup(self, *, device_id: str, trace_id: str, reason: str) -> asyncio.Task[None]:
        existing = self._device_disconnect_cleanup_tasks.get(device_id)
        if existing is not None and not existing.done():
            return existing
        cleanup_task = asyncio.create_task(
            self._run_device_disconnect_cleanup(device_id=device_id, trace_id=trace_id, reason=reason),
            name=f"northcua-device-disconnect-cleanup:{device_id}",
        )
        self._device_disconnect_cleanup_tasks[device_id] = cleanup_task
        return cleanup_task

    async def _run_device_disconnect_cleanup(self, *, device_id: str, trace_id: str, reason: str) -> None:
        try:
            await self._cleanup_device_disconnect(device_id=device_id, trace_id=trace_id, reason=reason)
        except Exception as exc:
            error = str(exc)
            self._device_disconnect_cleanup_failures[device_id] = error
            logger.warning(
                "device disconnect cleanup failed",
                extra={"device_id": device_id, "reason": reason},
                exc_info=True,
            )
            self.events.write(
                "device_disconnect_cleanup_failed",
                trace_id=trace_id,
                entity_type="device",
                entity_id=device_id,
                reason=reason,
                error=error,
            )
        else:
            self._device_disconnect_cleanup_failures.pop(device_id, None)
        finally:
            if self._device_disconnect_cleanup_tasks.get(device_id) is asyncio.current_task():
                self._device_disconnect_cleanup_tasks.pop(device_id, None)

    async def _cleanup_device_disconnect(self, *, device_id: str, trace_id: str, reason: str) -> None:
        await self.registry.mark_offline_async(device_id, trace_id=trace_id, reason=reason)
        await self._cancel_pending_commands_for_device(device_id)
        await self.task_service.interrupt_device(device_id=device_id, trace_id=trace_id, reason=reason)

    async def _send_command_to_device(self, device_id: str, command: CommandRequest, trace_id: str) -> CommandResult:
        """Send one command request to a connected device and await its result.

        Args:
            device_id: Target device id.
            command: Command request to serialize and send.
            trace_id: Trace id used for the outgoing envelope.

        Returns:
            Command result produced by the device or disconnect cleanup.

        Raises:
            RuntimeError: If the target device is no longer connected.
        """

        return await self.command_results.send_to_device(device_id, command, trace_id)

    async def _cancel_pending_commands_for_device(self, device_id: str) -> None:
        """Resolve in-memory command waits when a device connection disappears."""

        await self.command_results.cancel_for_device_async(device_id)

    async def _cancel_pending_commands_for_session(self, session_id: str, trace_id: str, reason: str) -> None:
        """Cancel persisted and in-memory command waits for one terminal session."""

        await self.command_results.cancel_for_session_async(session_id, trace_id, reason)


def _handshake_metric_payload(handshake: Mapping[str, object], handler_started_at: float) -> dict[str, object]:
    started_at = handshake.get("started_at")
    response_ms = handshake.get("response_ms")
    response_status_code = handshake.get("response_status_code")
    payload: dict[str, object] = {
        "websocket_open_handshake_ms": int(response_ms) if isinstance(response_ms, (int, float)) else -1,
        "websocket_response_status_code": (
            int(response_status_code) if isinstance(response_status_code, (int, float)) else 0
        ),
    }
    if isinstance(started_at, float):
        payload["websocket_handler_start_delay_ms"] = int((handler_started_at - started_at) * 1000)
    else:
        payload["websocket_handler_start_delay_ms"] = -1
    return payload


def _payload_int(payload: Mapping[str, object], key: str, default: int) -> int:
    value = payload.get(key)
    return int(value) if isinstance(value, (int, float)) else default


def _websocket_close_payload(websocket: ServerConnection) -> dict[str, object]:
    return {
        "close_code": int(getattr(websocket, "close_code", 0) or 0),
        "close_reason": str(getattr(websocket, "close_reason", "") or "")[:500],
    }


@dataclass
class _IngressLagBucket:
    values_ms: list[int] = field(default_factory=list)

    def add(self, value_ms: int) -> None:
        """Record one ingress lag sample in milliseconds.

        Args:
            value_ms: Non-negative controller ingress lag sample.
        """

        self.values_ms.append(value_ms)

    def snapshot(self) -> dict[str, int]:
        """Return percentile summary for collected lag samples.

        Returns:
            Count, p50, p90, p95, and max latency values.
        """

        values = sorted(self.values_ms)
        return {
            "count": len(values),
            "p50_ms": _interpolated_percentile(values, 50),
            "p90_ms": _interpolated_percentile(values, 90),
            "p95_ms": _interpolated_percentile(values, 95),
            "max_ms": values[-1] if values else 0,
        }


class _DeviceTelemetryIngressLagTracker:
    """Collect per-interval controller ingress lag for device telemetry."""

    def __init__(self) -> None:
        self._overall = _IngressLagBucket()
        self._by_event_type: dict[str, _IngressLagBucket] = {}
        self._by_tool_stage: dict[str, _IngressLagBucket] = {}

    def observe(self, payload: Mapping[str, object]) -> None:
        """Record one device telemetry ingress lag payload.

        Args:
            payload: Telemetry payload that may include lag, event, and tool
                stage dimensions.
        """

        value = payload.get("controller_ingress_lag_ms")
        if not isinstance(value, int):
            return
        self._overall.add(value)
        event_type = payload.get("event_type")
        if isinstance(event_type, str) and event_type:
            self._by_event_type.setdefault(event_type, _IngressLagBucket()).add(value)
        tool_id = payload.get("tool_id")
        stage = payload.get("stage")
        if isinstance(tool_id, str) and tool_id and isinstance(stage, str) and stage:
            self._by_tool_stage.setdefault(f"{tool_id}:{stage}", _IngressLagBucket()).add(value)

    def drain_snapshot(self) -> dict[str, object]:
        """Return and reset the current ingress lag buckets.

        Returns:
            Snapshot containing overall, by-event-type, and by-tool-stage
            latency summaries.
        """

        snapshot = {
            "overall": self._overall.snapshot(),
            "by_event_type": _top_lag_buckets(self._by_event_type),
            "by_tool_stage": _top_lag_buckets(self._by_tool_stage),
        }
        self._overall = _IngressLagBucket()
        self._by_event_type = {}
        self._by_tool_stage = {}
        return snapshot


@dataclass
class _OperationTimingBucket:
    values_ms: list[float] = field(default_factory=list)
    dropped_samples: int = 0

    def add(self, value_ms: float, *, max_samples: int) -> None:
        """Record one operation timing sample.

        Args:
            value_ms: Operation elapsed time in milliseconds.
            max_samples: Maximum samples retained for one runtime interval.
        """

        if len(self.values_ms) >= max_samples:
            self.dropped_samples += 1
            return
        self.values_ms.append(max(0.0, float(value_ms)))

    def snapshot(self) -> dict[str, float | int]:
        """Return count and percentile timing summary.

        Returns:
            Sample count, drop count, percentiles, and max elapsed time.
        """

        values = sorted(self.values_ms)
        return {
            "count": len(values) + self.dropped_samples,
            "sampled_count": len(values),
            "dropped_samples": self.dropped_samples,
            "p50_ms": _float_percentile(values, 50),
            "p90_ms": _float_percentile(values, 90),
            "p95_ms": _float_percentile(values, 95),
            "max_ms": round(values[-1], 3) if values else 0.0,
        }


class _ControllerOperationTimingTracker:
    """Collect low-overhead per-interval controller operation timings."""

    _MAX_SAMPLES_PER_OPERATION = 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _OperationTimingBucket] = {}

    def observe(self, operation: str, elapsed_ms: float) -> None:
        """Record one operation timing sample.

        Args:
            operation: Stable operation name.
            elapsed_ms: Elapsed time in milliseconds.
        """

        if not operation:
            return
        with self._lock:
            bucket = self._buckets.setdefault(operation, _OperationTimingBucket())
            bucket.add(elapsed_ms, max_samples=self._MAX_SAMPLES_PER_OPERATION)

    def drain_snapshot(self) -> dict[str, dict[str, float | int]]:
        """Return and reset operation timing buckets.

        Returns:
            Timing summaries keyed by stable controller operation name.
        """

        with self._lock:
            buckets = self._buckets
            self._buckets = {}
        ranked = sorted(
            ((name, bucket.snapshot()) for name, bucket in buckets.items()),
            key=lambda item: (float(item[1]["p95_ms"]), int(item[1]["count"]), item[0]),
            reverse=True,
        )
        return dict(ranked)


def _top_lag_buckets(buckets: Mapping[str, _IngressLagBucket], limit: int = 20) -> dict[str, dict[str, int]]:
    ranked = sorted(
        ((name, bucket.snapshot()) for name, bucket in buckets.items()),
        key=lambda item: (item[1]["p95_ms"], item[1]["count"], item[0]),
        reverse=True,
    )
    return dict(ranked[:limit])


def _interpolated_percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * percentile / 100
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return values[lower]
    weight = index - lower
    return int(values[lower] + (values[upper] - values[lower]) * weight)


def _float_percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 3)
    index = (len(values) - 1) * percentile / 100
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return round(values[lower], 3)
    weight = index - lower
    return round(values[lower] + (values[upper] - values[lower]) * weight, 3)


def _stringify_remote_address(remote_address: object) -> str:
    if isinstance(remote_address, tuple):
        return ":".join(str(part) for part in remote_address)
    return str(remote_address or "")


def _tcp_socket_queue_metrics(port: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["ss", "-tanH", f"sport = :{int(port)}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"socket_metrics_available": False, "socket_metrics_error": str(exc)[:300]}
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "ss failed").strip()
        return {"socket_metrics_available": False, "socket_metrics_error": error[:300]}
    recv_q_values: list[int] = []
    send_q_values: list[int] = []
    state_counts: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        state = parts[0]
        try:
            recv_q = int(parts[1])
            send_q = int(parts[2])
        except ValueError:
            continue
        state_counts[state] = state_counts.get(state, 0) + 1
        recv_q_values.append(recv_q)
        send_q_values.append(send_q)
    return {
        "socket_metrics_available": True,
        "socket_count": len(recv_q_values),
        "socket_state_counts": state_counts,
        "recv_q_max": max(recv_q_values, default=0),
        "recv_q_p95": _nearest_percentile(recv_q_values, 0.95),
        "send_q_max": max(send_q_values, default=0),
        "send_q_p95": _nearest_percentile(send_q_values, 0.95),
    }


def _nearest_percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]
