"""Websocket envelope dispatch handlers for the controller API."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from northcua.controller.api.command_waits import PendingCommandResult, PendingCommandResultRegistry
from northcua.controller.auth import ControllerAuth
from northcua.controller.control_plane.connections import ConnectionRegistry, WebSocketPeer
from northcua.controller.control_plane.registry import DeviceRegistry
from northcua.controller.execution.command_bus import CommandBus
from northcua.controller.execution.task_status import TaskStatusReader, statuses_for_ids_from_db, task_list_from_db
from northcua.controller.execution.tasks import TaskService
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.observability.telemetry_reader import ControllerTelemetryReader
from northcua.controller.storage.async_store import controller_store_read_profiled
from northcua.controller.storage.persistence import SQLiteControllerStore
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import (
    CommandCancel,
    CommandRequest,
    CommandResult,
    DeviceDrain,
    DeviceHeartbeat,
    DeviceHello,
    DeviceTelemetryEvent,
    SessionInterrupted,
    SessionLeaseReleased,
    TaskCancel,
    TaskFinished,
    TaskListRequest,
    TaskListResponse,
    TaskRequest,
    TaskStatusFilter,
    TaskStatusRequest,
    TaskStatusResponse,
    TaskStatusSnapshot,
    TelemetrySubscribeRequest,
    TelemetryUpdate,
)
from northcua_protocol.runtime_sidecars import split_runtime_sidecar
from northcua_runtime_shared.redaction import redact_for_log


class ControllerEnvelopeDispatcher:
    """Route parsed websocket envelopes to controller services."""

    def __init__(
        self,
        *,
        auth: ControllerAuth,
        connections: ConnectionRegistry,
        registry: DeviceRegistry,
        command_bus: CommandBus,
        task_service: TaskService,
        store: SQLiteControllerStore,
        events: ControllerEventLog,
        command_results: PendingCommandResultRegistry,
        cancel_device_disconnect_cleanup: Callable[[str, str], None] | None = None,
        device_telemetry_observer: Callable[[Mapping[str, object]], None] | None = None,
        controller_operation_observer: Callable[[str, float], None] | None = None,
        device_telemetry_persist_mode: str = "aggregate",
        device_telemetry_sample_rate: float = 1.0,
    ) -> None:
        self.auth = auth
        self.connections = connections
        self.registry = registry
        self.command_bus = command_bus
        self.task_service = task_service
        self.store = store
        self.events = events
        self.command_results = command_results
        self.cancel_device_disconnect_cleanup = cancel_device_disconnect_cleanup
        self._controller_operation_observer = controller_operation_observer
        self._status_db_path = Path(store.path) if getattr(store, "path", None) is not None else None
        self.status_reader = TaskStatusReader(store, timing_observer=controller_operation_observer)
        self.telemetry_reader = ControllerTelemetryReader(store)
        self.device_telemetry_observer = device_telemetry_observer
        self.device_telemetry_persist_mode = device_telemetry_persist_mode
        self.device_telemetry_sample_rate = min(1.0, max(0.0, float(device_telemetry_sample_rate)))

    async def dispatch(self, websocket: WebSocketPeer, envelope: Envelope) -> None:
        """Dispatch one parsed protocol envelope to the owning service.

        Args:
            websocket: Websocket that sent the envelope.
            envelope: Parsed protocol envelope.

        Raises:
            PermissionError: If the sender lacks authority for the payload.
        """

        payload = envelope.payload
        if envelope.type == MessageType.DEVICE_HELLO and isinstance(payload, DeviceHello):
            await self._handle_device_hello(websocket, envelope, payload)
            return
        if envelope.type == MessageType.DEVICE_HEARTBEAT and isinstance(payload, DeviceHeartbeat):
            await self._handle_device_heartbeat(websocket, envelope, payload)
            return
        if envelope.type == MessageType.DEVICE_TELEMETRY and isinstance(payload, DeviceTelemetryEvent):
            self._handle_device_telemetry(websocket, envelope, payload)
            return
        if envelope.type == MessageType.DEVICE_DRAIN and isinstance(payload, DeviceDrain):
            await self._handle_device_drain(websocket, envelope, payload)
            return
        if envelope.type == MessageType.COMMAND_RESULT and isinstance(payload, CommandResult):
            await self._handle_command_result(websocket, envelope, payload)
            return
        if envelope.type == MessageType.SESSION_LEASE_RELEASED and isinstance(payload, SessionLeaseReleased):
            raise PermissionError("session lease release is controller-originated only")
        if envelope.type == MessageType.SESSION_INTERRUPTED and isinstance(payload, SessionInterrupted):
            raise PermissionError("session_interrupted is controller-originated only")
        if envelope.type == MessageType.COMMAND_CANCEL and isinstance(payload, CommandCancel):
            raise PermissionError("command_cancel is controller-originated only")
        if envelope.type == MessageType.TASK_FINISHED and isinstance(payload, TaskFinished):
            await self.task_service.finish_task(
                websocket=websocket,
                payload=payload,
                correlation_id=envelope.message_id,
            )
            return
        if envelope.type == MessageType.TASK_CANCEL and isinstance(payload, TaskCancel):
            await self.task_service.cancel_task(
                websocket=websocket,
                payload=payload,
                correlation_id=envelope.message_id,
            )
            return
        if envelope.type == MessageType.TASK_REQUEST and isinstance(payload, TaskRequest):
            await self.task_service.accept_task(
                websocket=websocket,
                request=payload,
                correlation_id=envelope.message_id,
            )
            return
        if envelope.type == MessageType.TASK_STATUS_REQUEST and isinstance(payload, TaskStatusRequest):
            self.auth.validate_submitter_read_token(payload.token)
            statuses = await self._statuses_for_ids(payload.task_ids)
            await websocket.send(
                Envelope.wrap(
                    MessageType.TASK_STATUS_RESPONSE,
                    TaskStatusResponse(statuses=statuses),
                    trace_id=envelope.trace_id,
                    correlation_id=envelope.message_id,
                ).model_dump_json()
            )
            return
        if envelope.type == MessageType.TASK_LIST_REQUEST and isinstance(payload, TaskListRequest):
            self.auth.validate_submitter_read_token(payload.token)
            tasks = await self._list_tasks(payload.filter)
            await websocket.send(
                Envelope.wrap(
                    MessageType.TASK_LIST_RESPONSE,
                    TaskListResponse(tasks=tasks),
                    trace_id=envelope.trace_id,
                    correlation_id=envelope.message_id,
                ).model_dump_json()
            )
            return
        if envelope.type == MessageType.TELEMETRY_SUBSCRIBE and isinstance(payload, TelemetrySubscribeRequest):
            self.auth.validate_submitter_read_token(payload.token)
            await websocket.send(
                Envelope.wrap(
                    MessageType.TELEMETRY_UPDATE,
                    await self.telemetry_reader.update_async(payload),
                    trace_id=envelope.trace_id,
                    correlation_id=envelope.message_id,
                ).model_dump_json()
            )
            return

    async def _statuses_for_ids(self, task_ids: list[str]) -> list[TaskStatusSnapshot]:
        """Return task statuses without running SQLite reads on the websocket loop."""

        if self._status_db_path is None:
            return self.status_reader.statuses_for_ids(task_ids)
        return await asyncio.to_thread(
            statuses_for_ids_from_db,
            self._status_db_path,
            task_ids,
            timing_observer=self._controller_operation_observer,
        )

    async def _list_tasks(self, task_filter: TaskStatusFilter) -> list[TaskStatusSnapshot]:
        """Return a filtered task list without running SQLite reads on the websocket loop."""

        if self._status_db_path is None:
            return self.status_reader.list_tasks(task_filter)
        return await asyncio.to_thread(
            task_list_from_db,
            self._status_db_path,
            task_filter,
            timing_observer=self._controller_operation_observer,
        )

    async def _handle_device_hello(self, websocket: WebSocketPeer, envelope: Envelope, payload: DeviceHello) -> None:
        self.auth.validate_device_hello(payload.descriptor, payload.token)
        existing_connection = self.connections.for_device(payload.descriptor.device_id)
        if existing_connection is not None and existing_connection.websocket is not websocket:
            raise PermissionError("device id is already connected")
        if self.cancel_device_disconnect_cleanup is not None:
            self.cancel_device_disconnect_cleanup(payload.descriptor.device_id, envelope.trace_id)
        await self.registry.register_async(payload.descriptor, trace_id=envelope.trace_id)
        connection = self.connections.mark_device(websocket, payload.descriptor.device_id)
        self.registry.record_readiness_stage(
            trace_id=envelope.trace_id,
            device_id=payload.descriptor.device_id,
            stage="websocket_open",
            connection_id=connection.connection_id,
            websocket_open_handshake_ms=connection.websocket_open_handshake_ms,
            websocket_handler_start_delay_ms=connection.websocket_handler_start_delay_ms,
            first_envelope_ms=connection.first_envelope_ms,
            first_envelope_type=connection.first_envelope_type,
        )
        self.command_bus.register_sender(
            payload.descriptor.device_id,
            lambda command, trace_id, device_id=payload.descriptor.device_id: self.command_results.send_to_device(
                device_id,
                command,
                trace_id,
            ),
        )
        await self.task_service.retry_queued_tasks(trace_id=envelope.trace_id)

    async def _handle_device_heartbeat(
        self,
        websocket: WebSocketPeer,
        envelope: Envelope,
        payload: DeviceHeartbeat,
    ) -> None:
        connection = self.connections.get(websocket)
        if connection.device_id != payload.device_id:
            raise PermissionError("heartbeat sender does not own the device")
        should_retry_queued = await self.registry.heartbeat_async(payload, trace_id=envelope.trace_id)
        if should_retry_queued:
            await self.task_service.retry_queued_tasks(trace_id=envelope.trace_id)

    async def _handle_device_drain(self, websocket: WebSocketPeer, envelope: Envelope, payload: DeviceDrain) -> None:
        connection = self.connections.get(websocket)
        if connection.device_id != payload.device_id:
            raise PermissionError("drain sender does not own the device")
        await self.registry.mark_draining_async(payload.device_id, trace_id=envelope.trace_id, reason=payload.reason)

    def _handle_device_telemetry(
        self,
        websocket: WebSocketPeer,
        envelope: Envelope,
        payload: DeviceTelemetryEvent,
    ) -> None:
        connection = self.connections.get(websocket)
        if connection.device_id != payload.device_id:
            raise PermissionError("device telemetry sender does not own the device")
        event_payload = {
            "source": "device",
            "event_type": payload.event_type,
            "device_id": payload.device_id,
            "command_id": payload.command_id,
            "session_id": payload.session_id,
            "lease_id": payload.lease_id,
            "tool_id": payload.tool_id,
            "stage": payload.stage,
            "device_timestamp": payload.timestamp.isoformat(),
            **dict(payload.payload),
        }
        received_at = datetime.now(timezone.utc)
        event_payload["controller_received_timestamp"] = received_at.isoformat()
        ingress_lag_ms = _device_telemetry_ingress_lag_ms(event_payload, received_at)
        if ingress_lag_ms is not None:
            event_payload["controller_ingress_lag_ms"] = ingress_lag_ms
        redacted = redact_for_log(event_payload)
        if isinstance(redacted, dict) and self.device_telemetry_observer is not None:
            self.device_telemetry_observer(redacted)
        if not isinstance(redacted, dict) or not _should_persist_device_telemetry(
            redacted,
            trace_id=envelope.trace_id,
            message_id=envelope.message_id,
            mode=self.device_telemetry_persist_mode,
            sample_rate=self.device_telemetry_sample_rate,
        ):
            return
        self.events.write_redacted(
            payload.event_type,
            trace_id=envelope.trace_id,
            entity_type=payload.entity_type or ("command" if payload.command_id else "device"),
            entity_id=payload.entity_id or payload.command_id or payload.session_id or payload.device_id,
            payload=redacted,
        )

    async def _handle_command_result(
        self, websocket: WebSocketPeer, envelope: Envelope, payload: CommandResult
    ) -> None:
        raw_payload = payload
        payload = _strip_command_result_runtime_sidecar(raw_payload)
        connection = self.connections.get(websocket)
        if not connection.device_id:
            raise PermissionError("command result sender is not an authenticated device")
        artifact_count = len(payload.artifacts)
        artifact_bytes = _inline_artifact_bytes(payload)
        self.events.write(
            "command_result_envelope_received",
            trace_id=envelope.trace_id,
            entity_type="command",
            entity_id=payload.command_id,
            device_id=payload.device_id,
            session_id=payload.session_id,
            lease_id=payload.lease_id,
            status=payload.status.value,
            artifact_count=artifact_count,
            artifact_bytes=artifact_bytes,
        )
        pending = self.command_results.pending_for_result(payload)
        authority_source = "memory"
        preloaded_command_row: dict[str, object] | None = None
        if pending is not None:
            authority = pending
        else:
            command_row, lookup_metrics = await controller_store_read_profiled(
                self.store.path,
                lambda store: store.command_row_for_result(payload),
                operation_name="command_result_authority_fallback",
            )
            self.events.write(
                "command_result_authority_fallback",
                trace_id=envelope.trace_id,
                entity_type="command",
                entity_id=payload.command_id,
                store_operation=lookup_metrics.operation,
                store_mode=lookup_metrics.mode,
                store_queue_ms=lookup_metrics.queue_ms,
                store_admission_ms=lookup_metrics.admission_ms,
                store_executor_queue_ms=lookup_metrics.executor_queue_ms,
                store_operation_ms=lookup_metrics.operation_ms,
                store_exec_ms=lookup_metrics.exec_ms,
                store_sql_execute_ms=lookup_metrics.exec_ms,
                store_exec_scope="execute_and_transaction_excluding_fetch",
                store_total_ms=lookup_metrics.total_ms,
                store_queue_depth=lookup_metrics.queue_depth,
                store_admitted_queue_depth=lookup_metrics.admitted_queue_depth,
                store_sql_count=lookup_metrics.sql_count,
            )
            if command_row is None:
                self.events.write(
                    "command_result_unknown_command",
                    trace_id=envelope.trace_id,
                    entity_type="command",
                    entity_id=payload.command_id,
                    device_id=payload.device_id,
                    session_id=payload.session_id,
                    lease_id=payload.lease_id,
                    status=payload.status.value,
                    artifact_count=artifact_count,
                    artifact_bytes=artifact_bytes,
                )
                raise PermissionError("command result references unknown command")
            authority = CommandRequest.model_validate_json(command_row["request_json"])
            preloaded_command_row = dict(command_row)
            authority_source = "sqlite_read_pool"
        _validate_command_result_authority(
            connection_device_id=connection.device_id,
            payload=payload,
            authority=authority,
        )
        future = self.command_results.pop_future_for_result(payload)
        if future is not None and not future.done():
            future.set_result(raw_payload)
            self.events.write(
                "command_result_future_resolved",
                trace_id=envelope.trace_id,
                entity_type="command",
                entity_id=payload.command_id,
                device_id=payload.device_id,
                session_id=payload.session_id,
                lease_id=payload.lease_id,
                status=payload.status.value,
                artifact_count=artifact_count,
                artifact_bytes=artifact_bytes,
                authority_source=authority_source,
            )
            return
        self.events.write(
            "command_result_late_or_unmatched",
            trace_id=envelope.trace_id,
            entity_type="command",
            entity_id=payload.command_id,
            device_id=payload.device_id,
            session_id=payload.session_id,
            lease_id=payload.lease_id,
            status=payload.status.value,
            artifact_count=artifact_count,
            artifact_bytes=artifact_bytes,
            authority_source=authority_source,
        )
        await self.command_bus.record_result_async(
            payload,
            trace_id=envelope.trace_id,
            preloaded_command_row=preloaded_command_row,
        )


def _validate_command_result_authority(
    *,
    connection_device_id: str,
    payload: CommandResult,
    authority: CommandRequest | PendingCommandResult,
) -> None:
    if payload.command_id != authority.command_id:
        raise PermissionError("command result id does not match the active command")
    if connection_device_id != authority.device_id or payload.device_id != authority.device_id:
        raise PermissionError("command result sender does not own the command device")
    if payload.lease_id != authority.lease_id:
        raise PermissionError("command result lease does not match the active command lease")
    if payload.session_id != authority.session_id:
        raise PermissionError("command result session does not match the active command session")
    if payload.lease_generation != authority.lease_generation:
        raise PermissionError("command result lease_generation does not match the active command lease")


def _device_telemetry_ingress_lag_ms(payload: Mapping[str, object], received_at: datetime) -> int | None:
    timestamp = payload.get("device_event_timestamp") or payload.get("device_timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        device_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if device_at.tzinfo is None:
        device_at = device_at.replace(tzinfo=timezone.utc)
    return max(0, int((received_at - device_at).total_seconds() * 1000))


def _strip_command_result_runtime_sidecar(payload: CommandResult) -> CommandResult:
    clean_result, _sidecar = split_runtime_sidecar(payload.result)
    return payload.model_copy(update={"result": clean_result})


def _inline_artifact_bytes(payload: CommandResult) -> int:
    """Return inline artifact bytes attached to a command result."""

    return sum(max(int(getattr(artifact, "size_bytes", 0) or 0), 0) for artifact in payload.artifacts)


def _should_persist_device_telemetry(
    payload: Mapping[str, object],
    *,
    trace_id: str,
    message_id: str,
    mode: str,
    sample_rate: float,
) -> bool:
    normalized_mode = mode.strip().lower()
    if normalized_mode == "raw":
        return True
    if normalized_mode in {"aggregate", "off"}:
        return False
    if normalized_mode != "sample" or sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    sample_key = "|".join(
        str(payload.get(key) or "")
        for key in (
            "device_id",
            "session_id",
            "lease_id",
            "command_id",
            "event_type",
            "tool_id",
            "stage",
            "device_event_timestamp",
            "device_timestamp",
        )
    )
    digest = hashlib.blake2s(f"{sample_key}|{trace_id}|{message_id}".encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") / float(2**64 - 1)
    return bucket < sample_rate
