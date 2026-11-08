"""State-machine enums for product-grade remote CUA orchestration."""

from __future__ import annotations

from enum import Enum


class DeviceState(str, Enum):
    """Device lifecycle state tracked by controller registry and worker heartbeats."""

    CONNECTING = "connecting"
    ONLINE = "online"
    DRAINING = "draining"
    OFFLINE = "offline"
    UNHEALTHY = "unhealthy"


class SessionState(str, Enum):
    """Session lease lifecycle state for sticky device assignment."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASED = "released"


class CommandState(str, Enum):
    """Command lifecycle state for dispatch, timeout, and idempotency handling."""

    QUEUED = "queued"
    SENT = "sent"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TaskState(str, Enum):
    """Task lifecycle state for queueing, execution, and terminal reporting."""

    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
