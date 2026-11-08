"""Typed websocket transport envelopes for NorthCUA."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from northcua.agentinfra.runtime.protocol import AgentKind

from .enums import ResultStatus, TaskStatus, WebSocketMessageType
from .json_types import JsonObject
from .models import CommandExecutionResultRecord, CommandRequestRecord


class DesktopTaskRequestPayload(BaseModel):
    """Client -> server desktop task request.

    Fields:
        session_id: Optional requested session identifier.
        agent_kind: Desktop agent discriminator.
        request: User task/query text.
        metadata: Additional task metadata.
    """

    session_id: str | None = None
    agent_kind: Literal[AgentKind.DESKTOP] = AgentKind.DESKTOP
    request: str
    metadata: JsonObject = Field(default_factory=dict)


class WebTaskRequestPayload(BaseModel):
    """Client -> server generic web-agent task request.

    Fields:
        session_id: Optional requested session identifier.
        agent_kind: Web agent discriminator.
        request: User web task/query text.
        url: Optional initial URL.
        headed: Optional per-task override for showing the browser window.
        max_steps: Optional round/query max-step override.
        max_session_steps: Optional session-wide max-step override.
    """

    session_id: str | None = None
    agent_kind: Literal[AgentKind.WEB] = AgentKind.WEB
    request: str
    url: str = ""
    headed: bool | None = None
    max_steps: int = 0
    max_session_steps: int = 0
    metadata: JsonObject = Field(default_factory=dict)


class GenericTaskRequestPayload(BaseModel):
    """Registry-driven task request for low-code AgentKit agents.

    Attributes:
        model_config: Allows custom low-code agents to carry extra typed fields
            while protocol validation remains registry-driven.
        session_id: Optional requested session identifier.
        agent_kind: String registry id for the target AgentKit agent.
        request: User task/query text.
        max_steps: Optional round/query max-step override.
        max_session_steps: Optional session-wide max-step override.
        metadata: Additional task metadata passed to the runner.
    """

    model_config: ConfigDict = ConfigDict(extra="allow")

    session_id: str | None = None
    agent_kind: str
    request: str
    max_steps: int = 0
    max_session_steps: int = 0
    metadata: JsonObject = Field(default_factory=dict)


TaskRequestPayload = DesktopTaskRequestPayload | WebTaskRequestPayload | GenericTaskRequestPayload
_BUILTIN_TASK_MODELS: dict[str, type[BaseModel]] = {
    AgentKind.DESKTOP.value: DesktopTaskRequestPayload,
    AgentKind.WEB.value: WebTaskRequestPayload,
}


def parse_task_request_payload(
    payload: TaskRequestPayload | JsonObject,
    *,
    load_agentkit_specs: bool = True,
) -> TaskRequestPayload:
    """Parse a typed task request payload from a raw envelope payload.

    Args:
        payload: Typed task payload or raw payload mapping.

    Returns:
        Validated task request payload.

    Raises:
        pydantic.ValidationError: If the raw payload cannot be validated.
    """

    if isinstance(payload, (DesktopTaskRequestPayload, WebTaskRequestPayload, GenericTaskRequestPayload)):
        return payload
    agent_kind = str(payload.get("agent_kind") or "")
    model = _BUILTIN_TASK_MODELS.get(agent_kind)
    if model is not None:
        return model.model_validate(payload)  # type: ignore[return-value]
    from northcua.agentkit.registry import get_agentkit_registry, load_all_agent_specs

    if load_agentkit_specs:
        load_all_agent_specs()
    spec = get_agentkit_registry().maybe_get(agent_kind)
    if spec is None:
        raise ValueError(f"Unknown AgentKit task agent_kind: {agent_kind}")
    if spec.task_payload_model is not None:
        return spec.task_payload_model.model_validate(payload)  # type: ignore[return-value]
    return GenericTaskRequestPayload.model_validate(payload)


class TaskAcceptedPayload(BaseModel):
    """Server -> client acknowledgement that task execution started.

    Fields:
        session_id: Accepted session identifier.
        accepted: Whether the task was accepted.
    """

    session_id: str
    accepted: bool = True


class CommandRequestPayload(BaseModel):
    """Server -> device command request.

    Fields:
        session_id: Active session identifier.
        round_num: Current round number.
        round_step: Current step within the round.
        metadata: Runtime metadata shared by commands in this batch.
        commands: Command requests to execute.
    """

    session_id: str
    round_num: int
    round_step: int
    metadata: JsonObject = Field(default_factory=dict)
    commands: list[CommandRequestRecord] = Field(default_factory=list)


class CommandResultPayload(BaseModel):
    """Client -> server command execution result.

    Fields:
        session_id: Active session identifier.
        status: Aggregate command result status.
        results: Per-command execution results.
    """

    session_id: str
    status: ResultStatus
    results: list[CommandExecutionResultRecord] = Field(default_factory=list)


class TaskFinishedPayload(BaseModel):
    """Server -> client task completion.

    Fields:
        session_id: Completed session identifier.
        status: Terminal task status.
        result: Terminal result artifact or summary.
        error: Optional terminal error text.
        terminal_decision: Domain-neutral terminal decision payload when the
            agent produced or derived one.
    """

    session_id: str
    status: TaskStatus
    result: str = ""
    error: str | None = None
    terminal_decision: JsonObject = Field(default_factory=dict)


class HeartbeatPayload(BaseModel):
    """Bidirectional heartbeat payload.

    Fields:
        session_id: Optional session identifier.
        timestamp: Heartbeat timestamp.
    """

    session_id: str | None = None
    timestamp: float


class ErrorPayload(BaseModel):
    """Error transport payload.

    Fields:
        session_id: Optional session identifier.
        error: Error message.
    """

    session_id: str | None = None
    error: str


PayloadType = (
    DesktopTaskRequestPayload
    | WebTaskRequestPayload
    | GenericTaskRequestPayload
    | TaskAcceptedPayload
    | CommandRequestPayload
    | CommandResultPayload
    | TaskFinishedPayload
    | HeartbeatPayload
    | ErrorPayload
)


def _payload_for_message_type(
    message_type: WebSocketMessageType,
    payload: PayloadType | JsonObject,
) -> PayloadType:
    """Coerce a raw payload into the typed model for the envelope message type."""

    if message_type == WebSocketMessageType.TASK_REQUEST:
        if isinstance(
            payload,
            (
                DesktopTaskRequestPayload,
                WebTaskRequestPayload,
                GenericTaskRequestPayload,
            ),
        ):
            return payload
        return parse_task_request_payload(cast(JsonObject, payload))
    if message_type == WebSocketMessageType.TASK_ACCEPTED:
        return payload if isinstance(payload, TaskAcceptedPayload) else TaskAcceptedPayload.model_validate(payload)
    if message_type == WebSocketMessageType.COMMAND_REQUEST:
        return payload if isinstance(payload, CommandRequestPayload) else CommandRequestPayload.model_validate(payload)
    if message_type == WebSocketMessageType.COMMAND_RESULT:
        return payload if isinstance(payload, CommandResultPayload) else CommandResultPayload.model_validate(payload)
    if message_type == WebSocketMessageType.TASK_FINISHED:
        return payload if isinstance(payload, TaskFinishedPayload) else TaskFinishedPayload.model_validate(payload)
    if message_type == WebSocketMessageType.HEARTBEAT:
        return payload if isinstance(payload, HeartbeatPayload) else HeartbeatPayload.model_validate(payload)
    if message_type == WebSocketMessageType.ERROR:
        return payload if isinstance(payload, ErrorPayload) else ErrorPayload.model_validate(payload)
    raise ValueError(f"Unsupported websocket message type: {message_type}")


class NorthCUAWebSocketEnvelope(BaseModel):
    """Envelope carrying one typed payload.

    Fields:
        type: Websocket message type discriminator.
        payload: Message-type-specific payload model.
    """

    type: WebSocketMessageType
    payload: PayloadType

    @model_validator(mode="before")
    @classmethod
    def _coerce_payload(cls, data: object) -> object:
        """Validate payload using the message-type-specific payload model."""

        if not isinstance(data, dict):
            return data
        message_type_raw = data.get("type")
        if message_type_raw is None:
            return data
        message_type = (
            message_type_raw
            if isinstance(message_type_raw, WebSocketMessageType)
            else WebSocketMessageType(message_type_raw)
        )
        payload = data.get("payload")
        if payload is None:
            return data
        return {
            "type": message_type,
            "payload": _payload_for_message_type(message_type, payload),
        }

    @classmethod
    def wrap(cls, message_type: WebSocketMessageType, payload: PayloadType) -> "NorthCUAWebSocketEnvelope":
        """Build an envelope from a typed payload.

        Args:
            message_type: Websocket message type.
            payload: Typed payload for the message type.

        Returns:
            Typed websocket envelope.
        """

        return cls(type=message_type, payload=payload)
