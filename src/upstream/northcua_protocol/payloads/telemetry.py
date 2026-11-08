"""Telemetry monitor payloads for controller websocket clients."""

from __future__ import annotations

from pydantic import Field

from northcua_protocol.base import ProtocolModel
from northcua_protocol.capabilities import JsonObject


class TelemetrySubscribeRequest(ProtocolModel):
    """Request a snapshot-plus-events telemetry update from the controller.

    Attributes:
        token: Submitter read token.
        experiment_id: Optional experiment namespace filter.
        batch_id: Optional batch namespace filter.
        case_ids: Optional case ids to include.
        event_types: Optional event type strings to include.
        since_event_id: Only include events after this controller event id.
        include_snapshot: Whether latest case snapshots should be included.
        limit: Maximum telemetry events returned by one update.
    """

    token: str = ""
    experiment_id: str = ""
    batch_id: str = ""
    case_ids: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    since_event_id: int = 0
    include_snapshot: bool = True
    limit: int = 500


class TelemetryUpdate(ProtocolModel):
    """Controller telemetry response for monitor clients.

    Attributes:
        events: Redacted telemetry events newer than the requested cursor.
        case_snapshots: Latest case snapshots matching the requested filter.
        summary: Small aggregate summary for dashboards.
        next_event_id: Cursor callers can use on the next poll.
    """

    events: list[JsonObject] = Field(default_factory=list)
    case_snapshots: list[JsonObject] = Field(default_factory=list)
    summary: JsonObject = Field(default_factory=dict)
    next_event_id: int = 0


__all__ = ["TelemetrySubscribeRequest", "TelemetryUpdate"]
