"""Device lifecycle payloads for the v1 controller protocol."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from northcua_protocol.base import ProtocolModel
from northcua_protocol.capabilities import Capability, DeviceDescriptor, JsonObject

from .common import utc_now


class DeviceHello(ProtocolModel):
    """Device -> controller registration payload.

    Attributes:
        descriptor: Capabilities, tools, and concurrency limits advertised by the device.
        token: Device bearer token; empty only when dev auth is explicitly disabled.
    """

    descriptor: DeviceDescriptor
    token: str = ""


class DeviceHeartbeat(ProtocolModel):
    """Device -> controller liveness and load payload.

    Attributes:
        device_id: Stable worker id.
        active_sessions: Number of currently leased sessions.
        active_commands: Number of commands executing on the worker.
        load: Worker-reported load ratio used by scheduler heuristics.
        runtime_health: Runtime-specific health details for operators.
        capability_health: Capability-level health map.
        timestamp: Device-side heartbeat timestamp.
    """

    device_id: str
    active_sessions: int = 0
    active_commands: int = 0
    load: float = 0.0
    runtime_health: JsonObject = Field(default_factory=dict)
    capability_health: dict[Capability, bool] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class DeviceTelemetryEvent(ProtocolModel):
    """Device -> controller lightweight runtime telemetry.

    This payload is intentionally small and best-effort. Device workers use it
    for live command stage timings; full local logs still stay in
    ``device_runtime.jsonl`` for archive-time analysis.
    """

    device_id: str
    event_type: str
    entity_type: str = "device"
    entity_id: str = ""
    command_id: str = ""
    session_id: str = ""
    lease_id: str = ""
    tool_id: str = ""
    stage: str = ""
    timestamp: datetime = Field(default_factory=utc_now)
    payload: JsonObject = Field(default_factory=dict)


class DeviceDrain(ProtocolModel):
    """Controller -> device drain request.

    Attributes:
        device_id: Target device id.
        reason: Operator or controller reason for entering drain mode.
    """

    device_id: str
    reason: str = ""


__all__ = ["DeviceDrain", "DeviceHeartbeat", "DeviceHello", "DeviceTelemetryEvent"]
