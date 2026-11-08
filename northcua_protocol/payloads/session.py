"""Session and lease payloads for the v1 controller protocol."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from northcua_protocol.base import ProtocolModel
from northcua_protocol.capabilities import Capability, JsonObject
from northcua_protocol.state import SessionState


class SessionLeaseGranted(ProtocolModel):
    """Controller -> device session lease assignment.

    Attributes:
        lease_id: Lease id authorizing this session on the device.
        session_id: Session id whose commands must execute serially.
        task_id: Task associated with the session.
        device_id: Device receiving the lease.
        trace_id: End-to-end trace id.
        lease_generation: Monotonic generation used to reject stale commands.
        capabilities: Capabilities reserved for this session.
        metadata: Small runtime metadata inherited from the accepted task.
        expires_at: Optional lease expiry timestamp.
        state: Lease state carried on the wire.
    """

    lease_id: str
    session_id: str
    task_id: str
    device_id: str
    trace_id: str
    lease_generation: int = 1
    capabilities: set[Capability] = Field(default_factory=set)
    metadata: JsonObject = Field(default_factory=dict)
    expires_at: datetime | None = None
    state: SessionState = SessionState.LEASED


class SessionLeaseReleased(ProtocolModel):
    """Session lease release notification.

    Attributes:
        lease_id: Lease being released.
        session_id: Session being released.
        device_id: Device that owned the lease.
        state: Terminal session state.
        reason: Human-readable release reason.
    """

    lease_id: str
    session_id: str
    device_id: str
    state: SessionState = SessionState.RELEASED
    reason: str = ""

    @model_validator(mode="after")
    def _validate_released_state(self) -> "SessionLeaseReleased":
        """Ensure release notifications only carry a released state."""

        if self.state != SessionState.RELEASED:
            raise ValueError("SessionLeaseReleased.state must be released")
        return self


class SessionInterrupted(ProtocolModel):
    """Controller -> device/submitter interruption notification.

    Attributes:
        lease_id: Lease interrupted by disconnect, cancel, or policy.
        session_id: Session affected by the interruption.
        device_id: Device that owned the lease.
        state: Interrupted state marker.
        reason: Reason written to events and traces.
    """

    lease_id: str
    session_id: str
    device_id: str
    state: SessionState = SessionState.INTERRUPTED
    reason: str = ""

    @model_validator(mode="after")
    def _validate_interrupted_state(self) -> "SessionInterrupted":
        """Ensure interruption notifications only carry an interrupted state."""

        if self.state != SessionState.INTERRUPTED:
            raise ValueError("SessionInterrupted.state must be interrupted")
        return self


__all__ = ["SessionInterrupted", "SessionLeaseGranted", "SessionLeaseReleased"]
