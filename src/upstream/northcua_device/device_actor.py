"""Device actor tracking local leases, health, and load."""

from __future__ import annotations

from northcua_device.executor import CommandExecutor
from northcua_device.session_actor import SessionActor
from northcua_protocol.payloads import DeviceHeartbeat, SessionLeaseGranted, SessionLeaseReleased
from northcua_protocol.state import DeviceState, SessionState


class DeviceActor:
    """Manages active session actors for one device worker process."""

    def __init__(
        self,
        *,
        device_id: str,
        executor: CommandExecutor,
        max_sessions: int,
        max_concurrent_commands: int = 1,
    ) -> None:
        self.device_id = device_id
        self.executor = executor
        self.max_sessions = max_sessions
        self.max_concurrent_commands = max_concurrent_commands
        self.state = DeviceState.CONNECTING
        self.active_commands = 0
        self._sessions: dict[str, SessionActor] = {}

    @property
    def active_sessions(self) -> int:
        """Number of active sessions currently leased to this worker.

        Returns:
            Count of session actors held by the worker.
        """

        return len(self._sessions)

    def accept_lease(self, lease: SessionLeaseGranted) -> SessionActor:
        """Accept a new session lease and create its actor.

        Args:
            lease: Controller-issued session lease.

        Returns:
            Session actor responsible for serial command execution.

        Raises:
            RuntimeError: If the lease targets another device, the worker is
                draining, or the worker is at session capacity.
        """

        if lease.device_id != self.device_id:
            raise RuntimeError("lease targets a different device")
        if self.state == DeviceState.DRAINING:
            raise RuntimeError("device is draining")
        if len(self._sessions) >= self.max_sessions:
            raise RuntimeError("device max_sessions exceeded")
        actor = SessionActor(lease, self.executor)
        self._sessions[lease.session_id] = actor
        self.state = DeviceState.ONLINE
        return actor

    def release_lease(self, release: SessionLeaseReleased) -> None:
        """Release a session lease.

        Args:
            release: Release notification from the controller.

        Raises:
            RuntimeError: If the release does not match the active lease.
        """

        actor = self._sessions.get(release.session_id)
        if actor is None:
            return
        if actor.lease.device_id != release.device_id or actor.lease.lease_id != release.lease_id:
            raise RuntimeError("lease release does not match the active device lease")
        self._sessions.pop(release.session_id, None)
        actor.state = SessionState.RELEASED

    def session(self, session_id: str) -> SessionActor | None:
        """Return the session actor for a session id.

        Args:
            session_id: Session id to look up.

        Returns:
            Matching session actor, or ``None`` when no active lease exists.
        """

        return self._sessions.get(session_id)

    def heartbeat(self) -> DeviceHeartbeat:
        """Build the current heartbeat payload.

        Returns:
            Load and activity snapshot advertised to the controller.
        """

        return DeviceHeartbeat(
            device_id=self.device_id,
            active_sessions=len(self._sessions),
            active_commands=self.active_commands,
            load=float(len(self._sessions)) / max(1, self.max_sessions),
        )

    def start_command(self) -> None:
        """Increment active command pressure.

        Raises:
            RuntimeError: If executing another command would exceed local capacity.
        """

        if self.active_commands >= self.max_concurrent_commands:
            raise RuntimeError("device max_concurrent_commands exceeded")
        self.active_commands += 1

    def finish_command(self) -> None:
        """Decrement active command pressure.

        The counter is clamped defensively so cleanup after a partially failed
        command cannot make future heartbeats report negative load.
        """

        self.active_commands = max(0, self.active_commands - 1)

    def start_draining(self) -> None:
        """Enter drain mode and reject future leases."""

        self.state = DeviceState.DRAINING
