"""Typed payload models for NorthCUA v1 protocol envelopes."""

from __future__ import annotations

from .command import (
    AGENTKIT_SETTINGS_PARAM,
    COMMAND_RUNTIME_SIDE_CHANNEL_KEYS,
    CommandCancel,
    CommandRequest,
    CommandResult,
    WebAgentRefAliasRuntimeSetting,
    WebAgentPostActionSettleRuntimeSettings,
    WebAgentRuntimeSettings,
    sanitize_agentkit_settings_side_channel,
    sanitize_command_runtime_side_channels,
)
from .common import AgentKind, utc_now
from .control import ErrorPayload, PingPayload, PongPayload
from .device import DeviceDrain, DeviceHeartbeat, DeviceHello, DeviceTelemetryEvent
from .session import SessionInterrupted, SessionLeaseGranted, SessionLeaseReleased
from .task import (
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskListRequest,
    TaskListResponse,
    TaskRequest,
    TaskStatusFilter,
    TaskStatusRequest,
    TaskStatusResponse,
    TaskStatusSnapshot,
    task_request_fingerprint_sha256,
)
from .telemetry import TelemetrySubscribeRequest, TelemetryUpdate

Payload = (
    DeviceHello
    | DeviceHeartbeat
    | DeviceTelemetryEvent
    | DeviceDrain
    | SessionLeaseGranted
    | SessionLeaseReleased
    | SessionInterrupted
    | CommandRequest
    | CommandResult
    | CommandCancel
    | TaskRequest
    | TaskAccepted
    | TaskFinished
    | TaskCancel
    | TaskStatusRequest
    | TaskStatusResponse
    | TaskListRequest
    | TaskListResponse
    | TelemetrySubscribeRequest
    | TelemetryUpdate
    | ErrorPayload
    | PingPayload
    | PongPayload
)

__all__ = [
    "AGENTKIT_SETTINGS_PARAM",
    "AgentKind",
    "COMMAND_RUNTIME_SIDE_CHANNEL_KEYS",
    "CommandCancel",
    "CommandRequest",
    "CommandResult",
    "DeviceDrain",
    "DeviceHeartbeat",
    "DeviceHello",
    "DeviceTelemetryEvent",
    "ErrorPayload",
    "Payload",
    "PingPayload",
    "PongPayload",
    "SessionInterrupted",
    "SessionLeaseGranted",
    "SessionLeaseReleased",
    "TaskAccepted",
    "TaskCancel",
    "TaskFinished",
    "TaskListRequest",
    "TaskListResponse",
    "TaskRequest",
    "TaskStatusFilter",
    "TaskStatusRequest",
    "TaskStatusResponse",
    "TaskStatusSnapshot",
    "task_request_fingerprint_sha256",
    "TelemetrySubscribeRequest",
    "TelemetryUpdate",
    "WebAgentRefAliasRuntimeSetting",
    "WebAgentPostActionSettleRuntimeSettings",
    "WebAgentRuntimeSettings",
    "sanitize_agentkit_settings_side_channel",
    "sanitize_command_runtime_side_channels",
    "utc_now",
]
