"""Typed configuration and protocol primitives for NorthCUA.

The package exports config and model types eagerly, but keeps websocket
protocol models lazy so lightweight runtime consumers, including the standalone
device package mirror, do not import `agentinfra` unless they actually need the
legacy websocket envelope layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import (
    ArtifactConfig,
    CoordinatePolicyConfig,
    DesktopPolicyConfig,
    EvaluationConfig,
    EvaluationOutputConfig,
    ExecutionConfig,
    NorthCUAAppConfig,
    ObservationConfig,
    ProbeChainConfig,
    ProbeToolCallConfig,
    PromptPolicyConfig,
    WebSocketServerConfig,
)
from .models import (
    AssistantToolCallRecord,
    CommandExecutionResultRecord,
    CommandRequestRecord,
    DesktopStateSnapshot,
    EvaluationLogRecord,
    LLMTraceRecord,
    ObservationArtifact,
    ObservationContext,
    RoundInput,
    RoundState,
    StepLogRecord,
    ToolExecutionRecord,
    ToolResultArtifact,
    WindowState,
)

if TYPE_CHECKING:
    from .protocol import (
        CommandRequestPayload,
        CommandResultPayload,
        DesktopTaskRequestPayload,
        ErrorPayload,
        HeartbeatPayload,
        NorthCUAWebSocketEnvelope,
        TaskAcceptedPayload,
        TaskFinishedPayload,
        WebTaskRequestPayload,
        parse_task_request_payload,
    )

__all__ = [
    "CommandRequestPayload",
    "CommandResultPayload",
    "CommandExecutionResultRecord",
    "CommandRequestRecord",
    "CoordinatePolicyConfig",
    "DesktopTaskRequestPayload",
    "DesktopStateSnapshot",
    "DesktopPolicyConfig",
    "EvaluationConfig",
    "ErrorPayload",
    "EvaluationLogRecord",
    "EvaluationOutputConfig",
    "ExecutionConfig",
    "HeartbeatPayload",
    "LLMTraceRecord",
    "NorthCUAAppConfig",
    "NorthCUAWebSocketEnvelope",
    "ObservationArtifact",
    "ObservationConfig",
    "ObservationContext",
    "ProbeChainConfig",
    "ProbeToolCallConfig",
    "PromptPolicyConfig",
    "RoundInput",
    "RoundState",
    "StepLogRecord",
    "TaskAcceptedPayload",
    "TaskFinishedPayload",
    "ToolExecutionRecord",
    "ToolResultArtifact",
    "WebTaskRequestPayload",
    "WebSocketServerConfig",
    "WindowState",
    "parse_task_request_payload",
]


def __getattr__(name: str) -> object:
    """Lazily expose websocket protocol helpers on demand."""

    if name in {
        "CommandRequestPayload",
        "CommandResultPayload",
        "DesktopTaskRequestPayload",
        "ErrorPayload",
        "HeartbeatPayload",
        "NorthCUAWebSocketEnvelope",
        "TaskAcceptedPayload",
        "TaskFinishedPayload",
        "WebTaskRequestPayload",
        "parse_task_request_payload",
    }:
        from . import protocol as _protocol

        return getattr(_protocol, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
