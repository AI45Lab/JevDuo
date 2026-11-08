"""Typed contracts for local websocket step execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.context_value import ContextValue
from nexau.core.messages import Message

from northcua.core.json_types import JsonObject

AgentRoundResult = tuple[str | tuple[str, JsonObject], Agent | None]


@dataclass(slots=True)
class LocalStepModelResult:
    """One-step local model execution result before any tool batch runs.

    Attributes:
        response_text: Assistant text emitted by the single model call.
        stop_reason: NexAU-compatible stop reason surfaced to callers.
        pending_tool_calls: JSON-shaped tool calls that must be dispatched by
            the NorthCUA runtime instead of being executed inline.
        agent_state: Runtime state passed back for NexAU middleware/tool hooks.
        assistant_message: Provider-normalized assistant message emitted by the
            model. Remote stepwise continuation appends this before TOOL blocks
            so the model sees a valid assistant/tool/result history.
    """

    response_text: str
    stop_reason: str
    pending_tool_calls: list[JsonObject]
    agent_state: AgentState | None = None
    assistant_message: Message | None = None


RunAgentOnce = Callable[..., Awaitable[AgentRoundResult]]
PrepareAgent = Callable[..., Awaitable[Agent]]
BuildRuntimeAgentConfig = Callable[..., Awaitable[AgentConfig]]
ExactMessagesRunner = Callable[..., Awaitable[AgentRoundResult]]
LocalSingleModelStepRunner = Callable[..., Awaitable[LocalStepModelResult]]

__all__ = [
    "AgentRoundResult",
    "BuildRuntimeAgentConfig",
    "ExactMessagesRunner",
    "LocalSingleModelStepRunner",
    "LocalStepModelResult",
    "PrepareAgent",
    "RunAgentOnce",
]
