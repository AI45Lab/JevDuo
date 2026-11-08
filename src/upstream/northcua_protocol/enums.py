"""Wire protocol enums shared by controller and device packages."""

from __future__ import annotations

from enum import Enum


class MessageType(str, Enum):
    """NorthCUA v1 controller/device websocket message types."""

    DEVICE_HELLO = "device_hello"
    DEVICE_HEARTBEAT = "device_heartbeat"
    DEVICE_TELEMETRY = "device_telemetry"
    DEVICE_DRAIN = "device_drain"
    SESSION_LEASE_GRANTED = "session_lease_granted"
    SESSION_LEASE_RELEASED = "session_lease_released"
    SESSION_INTERRUPTED = "session_interrupted"
    COMMAND_REQUEST = "command_request"
    COMMAND_RESULT = "command_result"
    COMMAND_CANCEL = "command_cancel"
    TASK_REQUEST = "task_request"
    TASK_ACCEPTED = "task_accepted"
    TASK_FINISHED = "task_finished"
    TASK_CANCEL = "task_cancel"
    TASK_STATUS_REQUEST = "task_status_request"
    TASK_STATUS_RESPONSE = "task_status_response"
    TASK_LIST_REQUEST = "task_list_request"
    TASK_LIST_RESPONSE = "task_list_response"
    TELEMETRY_SUBSCRIBE = "telemetry_subscribe"
    TELEMETRY_UPDATE = "telemetry_update"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
