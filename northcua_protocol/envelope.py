"""Versioned protocol envelope for NorthCUA control/data plane messages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from northcua_protocol.base import ProtocolModel
from northcua_protocol.enums import MessageType
from northcua_protocol.payloads import (
    CommandRequest,
    CommandCancel,
    CommandResult,
    DeviceDrain,
    DeviceHeartbeat,
    DeviceHello,
    DeviceTelemetryEvent,
    ErrorPayload,
    Payload,
    PingPayload,
    PongPayload,
    SessionInterrupted,
    SessionLeaseGranted,
    SessionLeaseReleased,
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskListRequest,
    TaskListResponse,
    TaskRequest,
    TaskStatusRequest,
    TaskStatusResponse,
    TelemetrySubscribeRequest,
    TelemetryUpdate,
)


_PAYLOAD_BY_TYPE: dict[MessageType, type[BaseModel]] = {
    MessageType.DEVICE_HELLO: DeviceHello,
    MessageType.DEVICE_HEARTBEAT: DeviceHeartbeat,
    MessageType.DEVICE_TELEMETRY: DeviceTelemetryEvent,
    MessageType.DEVICE_DRAIN: DeviceDrain,
    MessageType.SESSION_LEASE_GRANTED: SessionLeaseGranted,
    MessageType.SESSION_LEASE_RELEASED: SessionLeaseReleased,
    MessageType.SESSION_INTERRUPTED: SessionInterrupted,
    MessageType.COMMAND_REQUEST: CommandRequest,
    MessageType.COMMAND_RESULT: CommandResult,
    MessageType.COMMAND_CANCEL: CommandCancel,
    MessageType.TASK_REQUEST: TaskRequest,
    MessageType.TASK_ACCEPTED: TaskAccepted,
    MessageType.TASK_FINISHED: TaskFinished,
    MessageType.TASK_CANCEL: TaskCancel,
    MessageType.TASK_STATUS_REQUEST: TaskStatusRequest,
    MessageType.TASK_STATUS_RESPONSE: TaskStatusResponse,
    MessageType.TASK_LIST_REQUEST: TaskListRequest,
    MessageType.TASK_LIST_RESPONSE: TaskListResponse,
    MessageType.TELEMETRY_SUBSCRIBE: TelemetrySubscribeRequest,
    MessageType.TELEMETRY_UPDATE: TelemetryUpdate,
    MessageType.ERROR: ErrorPayload,
    MessageType.PING: PingPayload,
    MessageType.PONG: PongPayload,
}


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Returns:
        Current UTC timestamp with timezone information attached.
    """

    return datetime.now(timezone.utc)


class Envelope(ProtocolModel):
    """Versioned envelope carrying exactly one typed protocol payload.

    Attributes:
        version: Wire protocol version. V1 remains the stable outer shape.
        type: Message discriminator used to coerce and validate payloads.
        message_id: Unique id for this envelope.
        correlation_id: Optional parent message id for request/reply tracking.
        trace_id: End-to-end trace id shared by controller, submitter, and device.
        sent_at: Send timestamp for diagnostics and ordering.
        payload: Typed payload matching ``type``.
    """

    version: Literal["v1"] = "v1"
    type: MessageType
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    trace_id: str
    sent_at: datetime = Field(default_factory=utc_now)
    payload: Payload

    @model_validator(mode="before")
    @classmethod
    def _coerce_payload(cls, data: object) -> object:
        """Coerce raw dict payloads using the message type."""

        if not isinstance(data, dict):
            return data
        raw_type = data.get("type")
        if raw_type is None:
            return data
        message_type = raw_type if isinstance(raw_type, MessageType) else MessageType(raw_type)
        payload = data.get("payload")
        payload_model = _PAYLOAD_BY_TYPE[message_type]
        coerced = payload if isinstance(payload, payload_model) else payload_model.model_validate(payload)
        return {**data, "type": message_type, "payload": coerced}

    @classmethod
    def wrap(
        cls,
        message_type: MessageType,
        payload: Payload,
        *,
        trace_id: str,
        correlation_id: str | None = None,
    ) -> "Envelope":
        """Build an envelope from a typed payload.

        Args:
            message_type: Wire message type discriminator.
            payload: Payload model matching ``message_type``.
            trace_id: End-to-end trace id.
            correlation_id: Optional request envelope id being answered.

        Returns:
            Validated v1 envelope ready for serialization.
        """

        return cls(type=message_type, payload=payload, trace_id=trace_id, correlation_id=correlation_id)


def parse_envelope(raw: str | bytes | dict[str, object]) -> Envelope:
    """Parse one v1 envelope from JSON text, bytes, or a raw mapping.

    Args:
        raw: JSON text, UTF-8 bytes, or already-decoded mapping.

    Returns:
        Validated envelope with payload coerced to the matching model.
    """

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return Envelope.model_validate_json(raw)
    return Envelope.model_validate(raw)
