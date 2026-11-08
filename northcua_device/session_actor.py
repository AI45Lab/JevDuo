"""Session actor that serializes commands for one leased session."""

from __future__ import annotations

import asyncio

from northcua_device.executor import CommandExecutor
from northcua_protocol.payloads import CommandRequest, CommandResult, SessionLeaseGranted
from northcua_protocol.state import CommandState, SessionState


class SessionActor:
    """Owns sequential command execution for one session lease."""

    def __init__(self, lease: SessionLeaseGranted, executor: CommandExecutor) -> None:
        self.lease = lease
        self.executor = executor
        self.state = SessionState.LEASED
        self._lock = asyncio.Lock()
        self._result_by_command_id: dict[str, CommandResult] = {}
        self._request_by_command_id: dict[str, CommandRequest] = {}

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one command serially and return cached result on duplicate IDs.

        Args:
            command: Controller-issued command request for this session.

        Returns:
            Adapter result, cached duplicate result, or a protocol failure for
            stale leases / command id conflicts.
        """

        if command.command_id in self._result_by_command_id:
            if self._request_by_command_id[command.command_id] != command:
                return self._conflict(command)
            return self._result_by_command_id[command.command_id]
        async with self._lock:
            if command.command_id in self._result_by_command_id:
                if self._request_by_command_id[command.command_id] != command:
                    return self._conflict(command)
                return self._result_by_command_id[command.command_id]
            if (
                command.session_id != self.lease.session_id
                or command.device_id != self.lease.device_id
                or command.lease_id != self.lease.lease_id
                or command.lease_generation != self.lease.lease_generation
            ):
                result = CommandResult(
                    command_id=command.command_id,
                    session_id=command.session_id,
                    lease_id=command.lease_id,
                    device_id=command.device_id,
                    lease_generation=command.lease_generation,
                    status=CommandState.FAILED,
                    error="LEASE_EXPIRED",
                )
            else:
                self.state = SessionState.RUNNING
                result = await self.executor.execute(command)
            self._request_by_command_id[command.command_id] = command
            self._result_by_command_id[command.command_id] = result
            return result

    @staticmethod
    def _conflict(command: CommandRequest) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            device_id=command.device_id,
            lease_generation=command.lease_generation,
            status=CommandState.FAILED,
            error="COMMAND_ID_CONFLICT",
        )
