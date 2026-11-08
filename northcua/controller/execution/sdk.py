"""In-process controller SDK used by agent runtimes and tests."""

from __future__ import annotations

from northcua.controller.control_plane.actors import SessionActor
from northcua.controller.control_plane.leases import LeaseManager
from northcua.controller.execution.command_bus import CommandBus
from northcua.controller.execution.tasks import TaskService
from northcua_protocol.payloads import (
    CommandRequest,
    CommandResult,
    SessionLeaseGranted,
    SessionLeaseReleased,
    TaskRequest,
)


class ControllerSDK:
    """Small facade that keeps agent code away from controller internals."""

    def __init__(self, *, task_service: TaskService, leases: LeaseManager, command_bus: CommandBus) -> None:
        self.task_service = task_service
        self.leases = leases
        self.command_bus = command_bus
        self._session_actors: dict[str, SessionActor] = {}

    def acquire_lease(
        self, request: TaskRequest, *, session_id: str, preferred_device_id: str = ""
    ) -> SessionLeaseGranted:
        """Acquire a device lease for an agent-owned task.

        Args:
            request: Task request built by an agent or submitter path.
            session_id: Controller session id to bind to the lease.
            preferred_device_id: Optional connection-affinity device preference.

        Returns:
            Lease payload that identifies the selected device and generation.
        """

        return self.leases.acquire_for_task(request, session_id=session_id, preferred_device_id=preferred_device_id)

    async def send_command(self, command: CommandRequest, *, trace_id: str) -> CommandResult:
        """Send one command through the controller command bus.

        Args:
            command: Tool command request scoped to an active lease.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Command result returned or synthesized by the command bus.
        """

        actor = self._session_actors.get(command.session_id)
        if actor is None:
            actor = SessionActor(
                lease=SessionLeaseGranted(
                    lease_id=command.lease_id,
                    session_id=command.session_id,
                    task_id="",
                    device_id=command.device_id,
                    trace_id=trace_id,
                    lease_generation=command.lease_generation,
                ),
                command_bus=self.command_bus,
            )
            self._session_actors[command.session_id] = actor
        return await actor.send_command(command)

    def record_result(self, result: CommandResult, *, trace_id: str) -> CommandResult:
        """Record a command result received outside the websocket adapter.

        Args:
            result: Command result to persist.
            trace_id: Trace id used for controller event correlation.

        Returns:
            Controller-canonical command result.
        """

        return self.command_bus.record_result(result, trace_id=trace_id)

    async def release_lease(self, release: SessionLeaseReleased, *, trace_id: str) -> None:
        """Release one session lease.

        Args:
            release: Lease release payload for the active session.
            trace_id: Trace id used for controller event correlation.
        """

        self._session_actors.pop(release.session_id, None)
        await self.task_service.release_lease(release, trace_id=trace_id)
