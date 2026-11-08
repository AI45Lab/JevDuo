"""Pending command-result futures for websocket command dispatch."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from northcua.controller.control_plane.connections import ConnectionRegistry
from northcua.controller.observability.event_log import ControllerEventLog
from northcua.controller.storage.async_store import ControllerStorePriority, controller_store_call
from northcua.controller.storage.persistence import SQLiteControllerStore, command_storage_key
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import CommandState

logger = logging.getLogger(__name__)
_DEVICE_CONNECTION_WAIT_SECONDS_ENV = "NORTHCUA_CONTROLLER_DEVICE_CONNECTION_WAIT_SECONDS"
_DEVICE_CONNECTION_POLL_SECONDS_ENV = "NORTHCUA_CONTROLLER_DEVICE_CONNECTION_POLL_SECONDS"
_DEFAULT_DEVICE_CONNECTION_WAIT_SECONDS = 2.0
_DEFAULT_DEVICE_CONNECTION_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class PendingCommandResult:
    """In-memory authority and waiter state for one dispatched command."""

    command_id: str
    session_id: str
    lease_id: str
    device_id: str
    lease_generation: int
    future: asyncio.Future[CommandResult]

    @classmethod
    def from_request(
        cls,
        request: CommandRequest,
        *,
        future: asyncio.Future[CommandResult],
    ) -> PendingCommandResult:
        return cls(
            command_id=request.command_id,
            session_id=request.session_id,
            lease_id=request.lease_id,
            device_id=request.device_id,
            lease_generation=request.lease_generation,
            future=future,
        )

    def done(self) -> bool:
        """Preserve the former future-like test and compatibility surface."""

        return self.future.done()

    def set_result(self, result: CommandResult) -> None:
        """Resolve the underlying future."""

        self.future.set_result(result)

    def result(self) -> CommandResult:
        """Return the underlying future result."""

        return self.future.result()


def _env_float(name: str, default: float) -> float:
    """Return a positive float from the environment, falling back safely."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


class PendingCommandResultRegistry:
    """Track command requests that are awaiting device websocket results."""

    def __init__(
        self,
        *,
        store: SQLiteControllerStore,
        connections: ConnectionRegistry,
        events: ControllerEventLog,
        device_connection_wait_seconds: float | None = None,
        device_connection_poll_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.connections = connections
        self.events = events
        self.pending_results: dict[str, PendingCommandResult | asyncio.Future[CommandResult]] = {}
        self.device_connection_wait_seconds = (
            _env_float(_DEVICE_CONNECTION_WAIT_SECONDS_ENV, _DEFAULT_DEVICE_CONNECTION_WAIT_SECONDS)
            if device_connection_wait_seconds is None
            else max(0.0, float(device_connection_wait_seconds))
        )
        self.device_connection_poll_seconds = (
            _env_float(_DEVICE_CONNECTION_POLL_SECONDS_ENV, _DEFAULT_DEVICE_CONNECTION_POLL_SECONDS)
            if device_connection_poll_seconds is None
            else max(0.01, float(device_connection_poll_seconds))
        )

    async def send_to_device(self, device_id: str, command: CommandRequest, trace_id: str) -> CommandResult:
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

        connection = await self._wait_for_device_connection(device_id, command=command, trace_id=trace_id)
        if connection is None:
            raise RuntimeError(f"device is not connected: {device_id}")
        future: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        pending_key = command_storage_key(
            session_id=command.session_id,
            lease_id=command.lease_id,
            command_id=command.command_id,
        )
        self.pending_results[pending_key] = PendingCommandResult.from_request(command, future=future)
        started_at = time.perf_counter()
        envelope_json = Envelope.wrap(
            MessageType.COMMAND_REQUEST,
            command,
            trace_id=trace_id,
        ).model_dump_json()
        try:
            send_started_at = time.perf_counter()
            try:
                await connection.websocket.send(envelope_json)
            except Exception as exc:
                logger.debug("failed to send command request to device websocket", exc_info=True)
                self.events.write(
                    "command_device_send_failed",
                    trace_id=trace_id,
                    entity_type="command",
                    entity_id=command.command_id,
                    session_id=command.session_id,
                    device_id=device_id,
                    tool_id=command.tool_id,
                    connection_id=connection.connection_id,
                    pending_command_waiters=len(self.pending_results),
                    envelope_bytes=len(envelope_json.encode("utf-8")),
                    websocket_send_ms=int((time.perf_counter() - send_started_at) * 1000),
                    error=str(exc)[:500],
                )
                raise
            send_finished_at = time.perf_counter()
            self.events.write(
                "command_device_send_done",
                trace_id=trace_id,
                entity_type="command",
                entity_id=command.command_id,
                session_id=command.session_id,
                device_id=device_id,
                tool_id=command.tool_id,
                connection_id=connection.connection_id,
                pending_command_waiters=len(self.pending_results),
                envelope_bytes=len(envelope_json.encode("utf-8")),
                websocket_send_ms=int((send_finished_at - send_started_at) * 1000),
            )
            result = await future
            self.events.write(
                "command_device_result_wait_resolved",
                trace_id=trace_id,
                entity_type="command",
                entity_id=command.command_id,
                session_id=command.session_id,
                device_id=device_id,
                tool_id=command.tool_id,
                status=result.status.value,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                result_wait_ms=int((time.perf_counter() - send_finished_at) * 1000),
            )
            return result
        except asyncio.CancelledError:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "controller command result wait cancelled",
                extra={
                    "command_id": command.command_id,
                    "session_id": command.session_id,
                    "device_id": device_id,
                    "tool_id": command.tool_id,
                    "elapsed_ms": elapsed_ms,
                    "timeout_seconds": command.timeout_seconds,
                },
            )
            self.events.write(
                "command_device_result_wait_cancelled",
                trace_id=trace_id,
                entity_type="command",
                entity_id=command.command_id,
                session_id=command.session_id,
                device_id=device_id,
                tool_id=command.tool_id,
                elapsed_ms=elapsed_ms,
                timeout_seconds=command.timeout_seconds,
            )
            raise
        finally:
            self.pending_results.pop(pending_key, None)

    async def _wait_for_device_connection(
        self,
        device_id: str,
        *,
        command: CommandRequest,
        trace_id: str,
    ):
        """Return the active device websocket, allowing short reconnect races.

        Device heartbeats and websocket reconnects can cross controller-origin
        command dispatch by a few milliseconds. Desktop remote runs already
        tolerate this through their retry/drain lifecycle; AgentKit web actions
        need the same small grace window so a transient connection replacement
        does not become a permanent tool failure.
        """

        connection = self.connections.for_device(device_id)
        if connection is not None or self.device_connection_wait_seconds <= 0:
            return connection
        started_at = time.perf_counter()
        deadline = started_at + self.device_connection_wait_seconds
        self.events.write(
            "command_device_connection_wait_started",
            trace_id=trace_id,
            entity_type="command",
            entity_id=command.command_id,
            session_id=command.session_id,
            device_id=device_id,
            tool_id=command.tool_id,
            wait_seconds=self.device_connection_wait_seconds,
        )
        while time.perf_counter() < deadline:
            await asyncio.sleep(self.device_connection_poll_seconds)
            connection = self.connections.for_device(device_id)
            if connection is not None:
                self.events.write(
                    "command_device_connection_wait_resolved",
                    trace_id=trace_id,
                    entity_type="command",
                    entity_id=command.command_id,
                    session_id=command.session_id,
                    device_id=device_id,
                    tool_id=command.tool_id,
                    waited_ms=int((time.perf_counter() - started_at) * 1000),
                    connection_id=connection.connection_id,
                )
                return connection
        self.events.write(
            "command_device_connection_wait_failed",
            trace_id=trace_id,
            entity_type="command",
            entity_id=command.command_id,
            session_id=command.session_id,
            device_id=device_id,
            tool_id=command.tool_id,
            waited_ms=int((time.perf_counter() - started_at) * 1000),
        )
        return None

    def pop_future_for_result(self, result: CommandResult) -> asyncio.Future[CommandResult] | None:
        """Pop the waiter associated with a received command result.

        Args:
            result: Command result whose storage identity identifies the waiter.

        Returns:
            Pending future for the command, if the controller is still waiting.
        """

        pending = self.pending_results.pop(self._pending_key(result), None)
        return _pending_future(pending)

    def pending_for_result(self, result: CommandResult) -> PendingCommandResult | None:
        """Return in-memory authority for a result without consuming its waiter."""

        pending = self.pending_results.get(self._pending_key(result))
        if isinstance(pending, PendingCommandResult) and not pending.future.done():
            return pending
        return None

    @staticmethod
    def _pending_key(result: CommandResult) -> str:
        return command_storage_key(
            session_id=result.session_id,
            lease_id=result.lease_id,
            command_id=result.command_id,
        )

    def cancel_for_device(self, device_id: str) -> None:
        """Resolve in-memory command waits when a device disappears.

        Args:
            device_id: Device whose active websocket disconnected.
        """

        pending_rows = self.store.command_rows_for_device(
            device_id,
            {CommandState.QUEUED, CommandState.SENT, CommandState.RUNNING},
        )
        for row in pending_rows:
            request = CommandRequest.model_validate_json(row["request_json"])
            pending = self.pending_results.get(
                command_storage_key(
                    session_id=request.session_id,
                    lease_id=request.lease_id,
                    command_id=request.command_id,
                )
            )
            future = _pending_future(pending)
            if future is None or future.done():
                continue
            future.set_result(
                CommandResult(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    lease_id=request.lease_id,
                    device_id=request.device_id,
                    lease_generation=request.lease_generation,
                    status=CommandState.CANCELLED,
                    error="device disconnected",
                )
            )

    async def cancel_for_device_async(self, device_id: str) -> None:
        """Resolve device command waits without blocking the event loop.

        Args:
            device_id: Device whose active websocket disconnected.

        Returns:
            None.
        """

        active_statuses = {CommandState.QUEUED, CommandState.SENT, CommandState.RUNNING}

        def _persist(store: SQLiteControllerStore) -> list[CommandResult]:
            rows = store.command_rows_for_device(device_id, active_statuses)
            results: list[CommandResult] = []
            for row in rows:
                request = CommandRequest.model_validate_json(row["request_json"])
                result = CommandResult(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    lease_id=request.lease_id,
                    device_id=request.device_id,
                    lease_generation=request.lease_generation,
                    status=CommandState.CANCELLED,
                    error="device disconnected",
                )
                store.record_command_result(result)
                results.append(result)
            return results

        results = await controller_store_call(
            self.store.path,
            _persist,
            operation_name="command_cancel_device_disconnect",
            priority=ControllerStorePriority.CRITICAL,
        )
        for result in results:
            pending = self.pending_results.get(
                command_storage_key(
                    session_id=result.session_id,
                    lease_id=result.lease_id,
                    command_id=result.command_id,
                )
            )
            future = _pending_future(pending)
            if future is None or future.done():
                continue
            future.set_result(result)

    def cancel_for_session(self, session_id: str, trace_id: str, reason: str) -> None:
        """Cancel persisted and in-memory command waits for a terminal session.

        Args:
            session_id: Session that reached a terminal task/lease state.
            trace_id: Trace id used for cancellation events.
            reason: Human-readable cancellation reason stored on results/events.
        """

        active_statuses = {CommandState.QUEUED, CommandState.SENT, CommandState.RUNNING}
        pending_rows = self.store.command_rows_for_session(session_id, active_statuses)
        self.store.update_commands_for_session(session_id, active_statuses, CommandState.CANCELLED)
        for row in pending_rows:
            request = CommandRequest.model_validate_json(row["request_json"])
            pending = self.pending_results.get(
                command_storage_key(
                    session_id=request.session_id,
                    lease_id=request.lease_id,
                    command_id=request.command_id,
                )
            )
            result = CommandResult(
                command_id=request.command_id,
                session_id=request.session_id,
                lease_id=request.lease_id,
                device_id=request.device_id,
                lease_generation=request.lease_generation,
                status=CommandState.CANCELLED,
                error=reason,
            )
            future = _pending_future(pending)
            if future is not None and not future.done():
                future.set_result(result)
            self.store.record_command_result(result)
            self.events.write(
                "command_cancelled_after_session_terminal",
                trace_id=trace_id,
                entity_type="command",
                entity_id=request.command_id,
                session_id=session_id,
                device_id=request.device_id,
                reason=reason,
            )

    async def cancel_for_session_async(self, session_id: str, trace_id: str, reason: str) -> None:
        """Cancel persisted and in-memory command waits off the event loop.

        Args:
            session_id: Session that reached a terminal task/lease state.
            trace_id: Trace id used for cancellation events.
            reason: Human-readable cancellation reason stored on results/events.

        Returns:
            None.
        """

        active_statuses = {CommandState.QUEUED, CommandState.SENT, CommandState.RUNNING}

        def _persist(store: SQLiteControllerStore) -> list[CommandResult]:
            rows = store.command_rows_for_session(session_id, active_statuses)
            store.update_commands_for_session(session_id, active_statuses, CommandState.CANCELLED)
            results: list[CommandResult] = []
            for row in rows:
                request = CommandRequest.model_validate_json(row["request_json"])
                result = CommandResult(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    lease_id=request.lease_id,
                    device_id=request.device_id,
                    lease_generation=request.lease_generation,
                    status=CommandState.CANCELLED,
                    error=reason,
                )
                store.record_command_result(result)
                results.append(result)
            return results

        results = await controller_store_call(
            self.store.path,
            _persist,
            operation_name="command_cancel_session_terminal",
            priority=ControllerStorePriority.CRITICAL,
        )
        for result in results:
            pending = self.pending_results.get(
                command_storage_key(
                    session_id=result.session_id,
                    lease_id=result.lease_id,
                    command_id=result.command_id,
                )
            )
            future = _pending_future(pending)
            if future is not None and not future.done():
                future.set_result(result)
            self.events.write(
                "command_cancelled_after_session_terminal",
                trace_id=trace_id,
                entity_type="command",
                entity_id=result.command_id,
                session_id=session_id,
                device_id=result.device_id,
                reason=reason,
            )


def _pending_future(
    pending: PendingCommandResult | asyncio.Future[CommandResult] | None,
) -> asyncio.Future[CommandResult] | None:
    if pending is None:
        return None
    if isinstance(pending, PendingCommandResult):
        return pending.future
    return pending
