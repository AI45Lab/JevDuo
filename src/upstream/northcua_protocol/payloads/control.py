"""Control and error payloads for the v1 protocol."""

from __future__ import annotations

from northcua_protocol.base import ProtocolModel
from northcua_protocol.errors import ErrorCode


class ErrorPayload(ProtocolModel):
    """Protocol error payload.

    Attributes:
        code: Stable protocol error code.
        message: Human-readable safe error text.
        entity_id: Related task, session, command, or device id.
        retryable: Whether the peer may retry the operation.
    """

    code: ErrorCode
    message: str
    entity_id: str = ""
    retryable: bool = False


class PingPayload(ProtocolModel):
    """Ping payload.

    Attributes:
        nonce: Opaque value echoed by the peer.
    """

    nonce: str


class PongPayload(ProtocolModel):
    """Pong payload.

    Attributes:
        nonce: Opaque value copied from the ping.
    """

    nonce: str


__all__ = ["ErrorPayload", "PingPayload", "PongPayload"]
