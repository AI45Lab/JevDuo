"""Protocol-level error codes exchanged by controller, submitter, and device."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Stable error codes for controller/device communication."""

    AUTH_FAILED = "auth_failed"
    DEVICE_NOT_ALLOWED = "device_not_allowed"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    LEASE_EXPIRED = "lease_expired"
    COMMAND_TIMED_OUT = "command_timed_out"
    COMMAND_CANCELLED = "command_cancelled"
    SESSION_INTERRUPTED = "session_interrupted"
    DEVICE_UNAVAILABLE = "device_unavailable"
    VALIDATION_FAILED = "validation_failed"
    INTERNAL_ERROR = "internal_error"
