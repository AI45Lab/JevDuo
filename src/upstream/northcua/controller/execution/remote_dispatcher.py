"""Agent-facing remote command dispatcher over the controller SDK."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

from northcua.controller.execution.executor_protocol import ControllerProgressSink
from northcua.controller.execution.processes.transport import (
    CommandTransport,
    InProcessCommandTransport,
    build_command_request,
)
from northcua.controller.execution.sdk import ControllerSDK
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import CommandResult, SessionLeaseGranted

logger = logging.getLogger(__name__)


class RemoteCommandDispatcher:
    """Dispatch agent tool calls through a leased controller device."""

    def __init__(
        self,
        *,
        sdk: ControllerSDK | None = None,
        lease: SessionLeaseGranted | None = None,
        progress_sink: ControllerProgressSink | None = None,
        metadata_overrides: Mapping[str, object] | None = None,
        transport: CommandTransport | None = None,
    ) -> None:
        resolved_transport = transport
        if resolved_transport is None:
            if sdk is None or lease is None:
                raise ValueError("RemoteCommandDispatcher requires either transport or sdk+lease")
            resolved_transport = InProcessCommandTransport(
                sdk=sdk,
                lease=lease,
                progress_sink=progress_sink,
                metadata_overrides=metadata_overrides,
            )
        self.transport: CommandTransport = resolved_transport

    async def dispatch(
        self,
        *,
        tool_id: str,
        parameters: JsonObject | None = None,
        command_id: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        """Send one tool command to the leased device and return its result.

        Args:
            tool_id: Registered device tool id.
            parameters: JSON parameters validated against the tool descriptor.
            command_id: Optional caller-provided id for idempotent replay.
            timeout_seconds: Command timeout enforced by controller and worker.

        Returns:
            Command result returned by the leased device.
        """

        resolved_command_id = command_id or uuid.uuid4().hex
        command = build_command_request(
            lease=self.transport.lease,
            metadata_overrides=self.transport.metadata_overrides,
            tool_id=tool_id,
            parameters=parameters,
            command_id=resolved_command_id,
            timeout_seconds=timeout_seconds,
        )
        await self._emit_progress(MessageType.COMMAND_REQUEST, command)
        result = await self.transport.send_command(command, trace_id=self.transport.lease.trace_id)
        await self._emit_progress(MessageType.COMMAND_RESULT, result)
        return result

    async def _emit_progress(self, message_type: MessageType, payload: object) -> None:
        """Best-effort stream of command progress back to the submitter UI."""

        try:
            await self.transport.emit_progress(message_type, payload)
        except Exception:
            logger.debug(
                "remote command progress sink failed",
                extra={
                    "message_type": message_type.value,
                    "session_id": self.transport.lease.session_id,
                    "task_id": self.transport.lease.task_id,
                },
                exc_info=True,
            )
