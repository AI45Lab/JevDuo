"""Controller message routing for device workers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from northcua_device.config import DeviceClientConfig
from northcua_device.connection import DeviceConnection
from northcua_device.device_actor import DeviceActor
from northcua_protocol.enums import MessageType
from northcua_protocol.envelope import Envelope
from northcua_protocol.payloads import (
    CommandRequest,
    CommandResult,
    DeviceDrain,
    PingPayload,
    PongPayload,
    SessionLeaseGranted,
    SessionLeaseReleased,
    TaskAccepted,
)
from northcua_protocol.state import CommandState
from northcua_runtime_shared.execution import execution_scope

logger = logging.getLogger(__name__)


class DeviceMessageRouter:
    """Route controller envelopes to the local device actor and connection."""

    def __init__(
        self,
        *,
        config: DeviceClientConfig,
        actor: DeviceActor,
        connection: DeviceConnection,
        event_writer: Callable[..., None] | None = None,
        command_received_at: dict[str, float] | None = None,
    ) -> None:
        self.config = config
        self.actor = actor
        self.connection = connection
        self._event_writer = event_writer
        self._command_received_at = command_received_at if command_received_at is not None else {}

    def _write_event(self, event: str, **payload: object) -> None:
        """Write device telemetry without breaking protocol progress."""

        if self._event_writer is None:
            return
        try:
            self._event_writer(event, **payload)
        except Exception:  # pragma: no cover - telemetry sink is best-effort
            logger.debug("device router event telemetry write failed", exc_info=True)

    async def handle(self, envelope: Envelope) -> None:
        """Handle one controller envelope addressed to this device worker.

        Args:
            envelope: Parsed controller envelope to route.

        Raises:
            Exception: Propagates command-result send failures after recording
                telemetry and logging the failed command context.
        """

        payload = envelope.payload
        if envelope.type == MessageType.DEVICE_DRAIN and isinstance(payload, DeviceDrain):
            if not self._message_targets_this_device(payload.device_id, message_type=envelope.type.value):
                return
            self.actor.start_draining()
            self._write_event("device_draining", device_id=payload.device_id, reason=payload.reason)
            return
        if envelope.type == MessageType.SESSION_LEASE_GRANTED and isinstance(payload, SessionLeaseGranted):
            if not self._message_targets_this_device(payload.device_id, message_type=envelope.type.value):
                return
            self.actor.accept_lease(payload)
            self._write_event("lease_started", session_id=payload.session_id, lease_id=payload.lease_id)
            return
        if envelope.type == MessageType.SESSION_LEASE_RELEASED and isinstance(payload, SessionLeaseReleased):
            if not self._message_targets_this_device(payload.device_id, message_type=envelope.type.value):
                return
            self.actor.release_lease(payload)
            self._write_event("lease_released", session_id=payload.session_id, lease_id=payload.lease_id)
            return
        if envelope.type == MessageType.COMMAND_REQUEST and isinstance(payload, CommandRequest):
            if not self._message_targets_this_device(payload.device_id, message_type=envelope.type.value):
                return
            started_at = time.monotonic()
            received_at = self._command_received_at.pop(payload.command_id, None)
            queue_wait_ms = int((started_at - received_at) * 1000) if received_at is not None else 0
            self._write_event(
                "device_command_dequeued",
                command_id=payload.command_id,
                session_id=payload.session_id,
                tool_id=payload.tool_id,
                timeout_seconds=payload.timeout_seconds,
                queue_wait_ms=queue_wait_ms,
            )
            self._write_event(
                "command_started",
                command_id=payload.command_id,
                session_id=payload.session_id,
                tool_id=payload.tool_id,
                timeout_seconds=payload.timeout_seconds,
                queue_wait_ms=queue_wait_ms,
            )
            # Queue delay consumes the device budget; do not restart a full
            # timeout after waiting behind another command/session operation.
            deadline = (received_at if received_at is not None else started_at) + payload.timeout_seconds
            with execution_scope(deadline):
                result = await self.execute_command(payload)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            artifact_count, artifact_bytes = _artifact_metrics(result)
            self._write_event(
                "command_result_ready",
                command_id=payload.command_id,
                session_id=payload.session_id,
                tool_id=payload.tool_id,
                status=result.status,
                elapsed_ms=elapsed_ms,
                artifact_count=artifact_count,
                artifact_bytes=artifact_bytes,
                error=result.error or "",
            )
            try:
                result_envelope = Envelope.wrap(
                    MessageType.COMMAND_RESULT,
                    result,
                    trace_id=envelope.trace_id,
                    correlation_id=envelope.message_id,
                )
                self._write_event(
                    "device_result_json_serialize_started",
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                    tool_id=payload.tool_id,
                    status=result.status,
                )
                serialize_started_at = time.perf_counter()
                raw_result_envelope = result_envelope.model_dump_json()
                serialize_elapsed_ms = int((time.perf_counter() - serialize_started_at) * 1000)
                result_envelope_bytes = len(raw_result_envelope.encode("utf-8"))
                self._write_event(
                    "device_result_json_serialize_finished",
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                    tool_id=payload.tool_id,
                    status=result.status,
                    elapsed_ms=serialize_elapsed_ms,
                    envelope_bytes=result_envelope_bytes,
                )
                self._write_event(
                    "device_websocket_send_started",
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                    tool_id=payload.tool_id,
                    status=result.status,
                    envelope_bytes=result_envelope_bytes,
                )
                send_raw = getattr(self.connection, "send_raw", None)
                if send_raw is not None:
                    send_stats = await send_raw(raw_result_envelope)
                else:
                    send_stats = await self.connection.send(result_envelope)
            except Exception as exc:
                logger.exception(
                    "Command result send failed command_id=%s session_id=%s tool_id=%s status=%s elapsed_ms=%s",
                    payload.command_id,
                    payload.session_id,
                    payload.tool_id,
                    result.status,
                    elapsed_ms,
                )
                self._write_event(
                    "command_result_send_failed",
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                    tool_id=payload.tool_id,
                    status=result.status,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )
                raise
            self._write_event(
                "device_websocket_send_finished",
                command_id=payload.command_id,
                status=result.status,
                tool_id=payload.tool_id,
                elapsed_ms=elapsed_ms,
                artifact_count=artifact_count,
                artifact_bytes=artifact_bytes,
                envelope_bytes=getattr(send_stats, "envelope_bytes", 0),
                send_elapsed_ms=getattr(send_stats, "elapsed_ms", 0),
                error=result.error or "",
            )
            self._write_event(
                "command_result_sent",
                command_id=payload.command_id,
                status=result.status,
                tool_id=payload.tool_id,
                elapsed_ms=elapsed_ms,
                artifact_count=artifact_count,
                artifact_bytes=artifact_bytes,
                envelope_bytes=getattr(send_stats, "envelope_bytes", 0),
                send_elapsed_ms=getattr(send_stats, "elapsed_ms", 0),
                error=result.error or "",
            )
            return
        if envelope.type == MessageType.PING and isinstance(payload, PingPayload):
            await self.connection.send(
                Envelope.wrap(
                    MessageType.PONG,
                    PongPayload(nonce=payload.nonce),
                    trace_id=envelope.trace_id,
                    correlation_id=envelope.message_id,
                )
            )
            return
        if envelope.type == MessageType.TASK_ACCEPTED and isinstance(payload, TaskAccepted):
            self._write_event("task_accepted", task_id=payload.task_id, session_id=payload.session_id)

    async def execute_command(self, payload: CommandRequest) -> CommandResult:
        """Execute one command through the owning session actor.

        Args:
            payload: Command request received from the controller.

        Returns:
            Command result from the session actor, or a lease-expired failure
            when the command references an unknown session.
        """

        self.actor.start_command()
        try:
            actor = self.actor.session(payload.session_id)
            if actor is None:
                self._write_event(
                    "command_rejected_missing_session",
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                )
                return CommandResult(
                    command_id=payload.command_id,
                    session_id=payload.session_id,
                    lease_id=payload.lease_id,
                    device_id=payload.device_id,
                    lease_generation=payload.lease_generation,
                    status=CommandState.FAILED,
                    error="LEASE_EXPIRED",
                )
            return await actor.execute(payload)
        finally:
            self.actor.finish_command()

    def _message_targets_this_device(self, device_id: str, *, message_type: str) -> bool:
        """Return whether a controller message is addressed to this worker."""

        if device_id == self.config.device_id:
            return True
        logger.warning(
            "device worker rejected message for another device",
            extra={
                "configured_device_id": self.config.device_id,
                "payload_device_id": device_id,
                "message_type": message_type,
            },
        )
        self._write_event(
            "message_rejected_wrong_device",
            configured_device_id=self.config.device_id,
            payload_device_id=device_id,
            message_type=message_type,
        )
        return False


def _artifact_metrics(result: CommandResult) -> tuple[int, int]:
    artifacts = list(result.artifacts or [])
    return len(artifacts), sum(int(artifact.size_bytes or 0) for artifact in artifacts)


__all__ = ["DeviceMessageRouter"]
