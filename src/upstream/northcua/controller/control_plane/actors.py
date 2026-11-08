"""Controller-side actor helpers for sessions and devices."""

from __future__ import annotations

import asyncio

from northcua.controller.execution.command_bus import CommandBus
from northcua_protocol.payloads import CommandRequest, CommandResult, SessionLeaseGranted


class SessionActor:
    """Controller actor that serializes commands for one leased session."""

    def __init__(self, *, lease: SessionLeaseGranted, command_bus: CommandBus) -> None:
        self.lease = lease
        self.command_bus = command_bus
        self._lock = asyncio.Lock()

    async def send_command(self, command: CommandRequest) -> CommandResult:
        """Send one command while preserving same-session ordering.

        Args:
            command: Command request scoped to this actor lease.

        Returns:
            Command result returned by the controller command bus.
        """

        async with self._lock:
            return await self.command_bus.send(command, trace_id=self.lease.trace_id)


class DeviceActor:
    """Controller actor representing device load and backpressure."""

    def __init__(self, device_id: str, max_sessions: int) -> None:
        self.device_id = device_id
        self.max_sessions = max_sessions
        self.active_session_ids: set[str] = set()

    def can_accept_session(self) -> bool:
        """Return whether the device can accept another session.

        Returns:
            `True` when active sessions are still below the actor limit.
        """

        return len(self.active_session_ids) < self.max_sessions
