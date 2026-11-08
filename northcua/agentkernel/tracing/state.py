"""Shared state and payload aliases for trace capture."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from nexau.archs.llm.llm_config import LLMConfig

from northcua.agentkernel.json_types import JsonObject, ProviderToolChoice, ProviderToolSpecs
from northcua.core.models import AssistantToolCallRecord, ToolExecutionRecord

ProviderRequestPayload: TypeAlias = JsonObject
ProviderResponsePayload: TypeAlias = JsonObject


@dataclass(slots=True)
class TraceCaptureState:
    """Mutable per-round capture state filled by NexAU hooks.

    Attributes:
        request_payload: Provider-facing request payload snapshot used for trace logging.
        response_payload: Provider response payload snapshot used for trace logging.
        stop_reason: Provider stop reason, when available.
        assistant_text: Assistant text captured after model execution.
        assistant_tool_calls: Assistant tool calls captured after model execution.
        tool_executions: Tool executions captured after tool callbacks.
        reasoning: Plain reasoning content, when available.
        reasoning_encrypted_content: Encrypted reasoning payload, when available.
        usage: Token usage captured from the provider response.
        cost: Cost captured from the provider response.
        provider_tools: Provider-normalized tool definitions.
        trace_tool_definitions: Private-pin neutral tool definitions retained
            for durable trace compatibility.
        provider_tool_choice: Provider-normalized tool-choice setting.
        provider_family: Provider family used for payload capture.
        trace_error: Non-fatal trace payload preparation error.
        llm_config: LLM config used for this capture state.
    """

    request_payload: ProviderRequestPayload = field(default_factory=dict)
    response_payload: ProviderResponsePayload = field(default_factory=dict)
    stop_reason: str | None = None
    assistant_text: str = ""
    assistant_tool_calls: list[AssistantToolCallRecord] = field(default_factory=list)
    tool_executions: list[ToolExecutionRecord] = field(default_factory=list)
    reasoning: str = ""
    reasoning_encrypted_content: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    provider_tools: ProviderToolSpecs = field(default_factory=list)
    trace_tool_definitions: ProviderToolSpecs = field(default_factory=list)
    provider_tool_choice: ProviderToolChoice = None
    provider_family: str = ""
    trace_error: str = ""
    llm_config: LLMConfig | None = None

    def release_provider_payloads(self) -> None:
        """Drop bulky provider request/response payloads after trace persistence."""

        self.request_payload = {}
        self.response_payload = {}

    def release_runtime_payloads(self) -> None:
        """Drop bulky runtime trace payloads after step artifacts are built."""

        self.release_provider_payloads()
        self.tool_executions.clear()


AgentTraceCaptureState = TraceCaptureState

__all__ = [
    "AgentTraceCaptureState",
    "ProviderRequestPayload",
    "ProviderResponsePayload",
    "TraceCaptureState",
]
