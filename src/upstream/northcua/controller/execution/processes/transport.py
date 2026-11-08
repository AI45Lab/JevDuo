"""Command transport abstractions for in-process and worker-process runners."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from northcua.controller.execution.command_bus import command_idempotency_key
from northcua.controller.execution.executor_protocol import ControllerProgressSink
from northcua.controller.execution.sdk import ControllerSDK
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import CommandRequest, CommandResult, SessionLeaseGranted


class CommandTransport(Protocol):
    """Minimal command authority used by remote runners."""

    @property
    def lease(self) -> SessionLeaseGranted:
        """Return the active controller lease for this transport.

        Returns:
            Session lease that scopes all runner commands.
        """
        ...

    @property
    def metadata_overrides(self) -> Mapping[str, object]:
        """Return command metadata overrides applied by the runner.

        Returns:
            Safe metadata values merged into each command request.
        """
        ...

    async def send_command(self, command: CommandRequest, *, trace_id: str) -> CommandResult:
        """Send a controller-scoped command and return the canonical result.

        Args:
            command: Command already scoped to the active lease.
            trace_id: Trace id used by controller command telemetry.

        Returns:
            Canonical command result produced by the controller.
        """
        ...

    async def emit_progress(self, message_type: MessageType, payload: object) -> None:
        """Emit best-effort progress for the submitter UI.

        Args:
            message_type: Protocol progress message type.
            payload: JSON-serializable or Pydantic payload to forward.
        """
        ...


class InProcessCommandTransport:
    """Command transport backed by the in-process controller SDK."""

    def __init__(
        self,
        *,
        sdk: ControllerSDK,
        lease: SessionLeaseGranted,
        progress_sink: ControllerProgressSink | None = None,
        metadata_overrides: Mapping[str, object] | None = None,
    ) -> None:
        self.sdk = sdk
        self.lease: SessionLeaseGranted = lease
        self.progress_sink = progress_sink
        self.metadata_overrides: Mapping[str, object] = dict(metadata_overrides or {})

    async def send_command(self, command: CommandRequest, *, trace_id: str) -> CommandResult:
        """Send one command through the local controller SDK.

        Args:
            command: Lease-scoped command request.
            trace_id: Trace id used by command telemetry.

        Returns:
            Canonical command result returned by the controller SDK.
        """

        return await self.sdk.send_command(command, trace_id=trace_id)

    async def emit_progress(self, message_type: MessageType, payload: object) -> None:
        """Forward command progress to the submitter when connected.

        Args:
            message_type: Protocol progress message type.
            payload: Payload to send to the submitter websocket.
        """

        if self.progress_sink is not None:
            await self.progress_sink(message_type, payload)


def build_command_request(
    *,
    lease: SessionLeaseGranted,
    metadata_overrides: Mapping[str, object],
    tool_id: str,
    parameters: JsonObject | None,
    command_id: str,
    timeout_seconds: float,
) -> CommandRequest:
    """Build a deterministic command request for a leased device.

    Args:
        lease: Active controller lease that owns the command.
        metadata_overrides: Safe metadata values merged into lease metadata.
        tool_id: Tool identifier requested by the runner.
        parameters: JSON parameters for the requested tool.
        command_id: Protocol command id supplied by the runner.
        timeout_seconds: Tool execution timeout requested by the runner.

    Returns:
        Command request with deterministic idempotency key.
    """

    command_metadata = {**dict(lease.metadata), **dict(metadata_overrides)}
    payload = {"tool_id": tool_id, "parameters": parameters or {}, "metadata": command_metadata}
    return CommandRequest(
        command_id=command_id,
        idempotency_key=command_idempotency_key(
            session_id=lease.session_id,
            lease_id=lease.lease_id,
            command_id=command_id,
            payload=payload,
        ),
        session_id=lease.session_id,
        lease_id=lease.lease_id,
        device_id=lease.device_id,
        tool_id=tool_id,
        parameters=parameters or {},
        metadata=command_metadata,
        timeout_seconds=timeout_seconds,
        lease_generation=lease.lease_generation,
    )
