"""Typed runtime protocol models for shared internal agent infrastructure."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

from northcua.platforms.protocol import PlatformCapability, PlatformKind


class AgentKind(str, Enum):
    """Supported internal agent kinds.

    Attributes:
        DESKTOP: Desktop-native CUA agent.
        WEB: Browser/web agent.
    """

    DESKTOP = "desktop"
    WEB = "web"


@dataclass(slots=True, frozen=True)
class AgentRunRequest:
    """Generic request envelope for one agent run.

    Attributes:
        session_id: Session identifier.
        query: User query/request text.
        max_steps: Optional round/query step override.
        max_session_steps: Optional session-wide step override.
        metadata: Additional request metadata.
    """

    session_id: str
    query: str
    max_steps: int = 0
    max_session_steps: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AgentRunResult:
    """Generic terminal metadata for one agent run.

    Attributes:
        session_id: Session identifier.
        terminal_status: Terminal status string.
        hit_step_limit: Whether execution stopped at the step limit.
        primary_result_path: Optional primary result artifact path.
        terminal_decision: Domain-neutral terminal decision payload when the
            agent produced or derived one.
    """

    session_id: str
    terminal_status: str | None = None
    hit_step_limit: bool = False
    primary_result_path: Path | None = None
    terminal_decision: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AgentStepEvent:
    """Shared step/panel event envelope.

    Attributes:
        agent_kind: Agent kind that emitted the event.
        step_index: One-based step index.
        status: Step status string.
        summary: Short step summary.
    """

    agent_kind: AgentKind
    step_index: int
    status: str
    summary: str


@dataclass(slots=True, frozen=True)
class DesktopStepEvent(AgentStepEvent):
    """Desktop-specific typed step-event projection.

    Attributes:
        request: User request for the step.
        thought: Reasoning/thinking summary.
        assistant_text: Assistant text response.
        tool_count: Number of assistant tool calls.
    """

    request: str = ""
    thought: str = ""
    assistant_text: str = ""
    tool_count: int = 0

    @classmethod
    def from_step_log(cls, step_log: object) -> "DesktopStepEvent":
        """Build a typed desktop step event from a step log record.

        Args:
            step_log: Desktop step log-like object.

        Returns:
            Desktop step event projection.
        """

        return cls(
            agent_kind=AgentKind.DESKTOP,
            step_index=int(getattr(step_log, "session_step", 0)),
            status=str(getattr(getattr(step_log, "status", None), "value", getattr(step_log, "status", ""))),
            summary=str(getattr(step_log, "result", "") or getattr(step_log, "assistant_text", "")),
            request=str(getattr(step_log, "request", "")),
            thought=str(
                getattr(step_log, "llm_reasoning", "")
                or getattr(step_log, "llm_thinking", "")
                or getattr(getattr(step_log, "llm_trace", None), "reasoning", "")
                or getattr(getattr(step_log, "llm_trace", None), "thinking", "")
                or ""
            ),
            assistant_text=str(getattr(step_log, "assistant_text", "")),
            tool_count=len(getattr(step_log, "assistant_tool_calls", []) or []),
        )


@dataclass(slots=True, frozen=True)
class BrowserStepEvent(AgentStepEvent):
    """Browser/web step-event projection.

    Attributes:
        url: Browser URL associated with the step.
        action: Web action/tool selected for the step.
    """

    url: str = ""
    action: str = ""


@dataclass(slots=True, frozen=True)
class WebStepEvent(BrowserStepEvent):
    """Web-agent-specific typed step-event projection.

    Attributes:
        assistant_text: Assistant text response.
        observation_summary: Compact page/DOM observation summary.
        success: Whether the step action succeeded.
    """

    assistant_text: str = ""
    observation_summary: str = ""
    success: bool = True

    @classmethod
    def from_panel_record(cls, panel_record: object) -> "WebStepEvent":
        """Build a typed web-agent step event from a panel record-like object.

        Args:
            panel_record: Web runner panel record or compatible test double.

        Returns:
            Generic typed event suitable for websocket/server callbacks.
        """

        return cls(
            agent_kind=AgentKind.WEB,
            step_index=int(getattr(panel_record, "step", 0)),
            status=str(getattr(panel_record, "status", "")),
            summary=str(
                getattr(panel_record, "summary", "")
                or getattr(panel_record, "observation_summary", "")
                or getattr(panel_record, "action", "")
            ),
            url=str(getattr(panel_record, "url", "")),
            action=str(getattr(panel_record, "action", "")),
            assistant_text=str(getattr(panel_record, "assistant_text", "")),
            observation_summary=str(getattr(panel_record, "observation_summary", "")),
            success=bool(getattr(panel_record, "success", True)),
        )


@dataclass(slots=True, frozen=True)
class AgentTerminalEvent:
    """Shared terminal event envelope.

    Attributes:
        agent_kind: Agent kind that emitted the event.
        session_id: Session identifier.
        terminal_status: Terminal status string.
        summary: Terminal summary.
    """

    agent_kind: AgentKind
    session_id: str
    terminal_status: str | None
    summary: str


@dataclass(slots=True, frozen=True)
class AgentTransportCapabilities:
    """Transport/runtime feature flags for one agent kind.

    Attributes:
        uses_in_memory_mcp: Whether the runtime uses in-memory MCP.
        supports_stepwise_local_boundary: Whether local step callbacks are supported.
        supports_remote_external_dispatch: Whether remote external dispatch is supported.
        supports_exact_message_seed: Whether exact seed messages can be supplied.
        requires_invocation_context: Whether invocation context is required.
    """

    uses_in_memory_mcp: bool = False
    supports_stepwise_local_boundary: bool = False
    supports_remote_external_dispatch: bool = False
    supports_exact_message_seed: bool = False
    requires_invocation_context: bool = False


@dataclass(slots=True, frozen=True)
class DesktopRunRequest(AgentRunRequest):
    """Typed run request for the desktop agent.

    Attributes:
        agent_kind: Desktop agent discriminator.
        max_steps: Desktop round/query max-step limit.
        max_session_steps: Optional desktop session-wide max-step limit.
    """

    agent_kind: AgentKind = field(default=AgentKind.DESKTOP, init=False)
    max_steps: int = 100


@dataclass(slots=True, frozen=True)
class DesktopRunResult(AgentRunResult):
    """Typed run result for the desktop agent.

    Attributes:
        agent_kind: Desktop agent discriminator.
        step_count: Number of completed steps.
        step_log_path: Per-step log path.
        runtime_log_path: Runtime event log path.
        evaluation_log_path: Evaluation log path.
    """

    agent_kind: AgentKind = field(default=AgentKind.DESKTOP, init=False)
    step_count: int = 0
    step_log_path: Path | None = None
    runtime_log_path: Path | None = None
    evaluation_log_path: Path | None = None


@dataclass(slots=True, frozen=True)
class WebRunRequest(AgentRunRequest):
    """Typed run request for the generic web agent.

    Attributes:
        agent_kind: Web agent discriminator.
        url: Optional initial URL to open before the first agent step.
        headed: Optional per-run override for showing the browser window.
        max_steps: Optional round/query max-step override.
        max_session_steps: Optional session-wide max-step override.
    """

    agent_kind: AgentKind = field(default=AgentKind.WEB, init=False)
    url: str = ""
    headed: bool | None = None
    max_steps: int = 0


@dataclass(slots=True, frozen=True)
class WebRunResult(AgentRunResult):
    """Typed run result for the generic web agent.

    Attributes:
        agent_kind: Web agent discriminator.
        status: Terminal status string.
        step_count: Number of completed steps.
        final_answer: Final assistant answer or fallback summary.
        success: Whether the task reached a successful terminal state.
        trace_path: Runtime trace path.
        result_path: Persisted structured result path.
        tool_errors: Tool error summaries.
    """

    agent_kind: AgentKind = field(default=AgentKind.WEB, init=False)
    status: str = ""
    step_count: int = 0
    final_answer: str = ""
    success: bool = False
    trace_path: Path | None = None
    result_path: Path | None = None
    tool_errors: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class AgentRuntimeDescriptor:
    """Stable runtime descriptor used by the internal registry.

    Attributes:
        kind: Agent kind described by the runtime.
        display_name: Human-readable runtime name.
        default_agent_config_selector: Agent kind used to select default config.
        transport_capabilities: Supported transport/runtime features.
        platform_kind: Optional platform kind backing this agent.
        platform_capabilities: Platform capabilities advertised by the agent.
        prompt_strategy_cls: Optional prompt strategy class for future generic orchestration.
        runtime_factory_cls: Optional platform/runtime factory class.
        enabled: Whether the runtime is enabled.
    """

    kind: AgentKind
    display_name: str
    default_agent_config_selector: AgentKind
    transport_capabilities: AgentTransportCapabilities = field(default_factory=AgentTransportCapabilities)
    platform_kind: PlatformKind | None = None
    platform_capabilities: tuple[PlatformCapability, ...] = ()
    prompt_strategy_cls: type[object] | None = None
    runtime_factory_cls: type[object] | None = None
    enabled: bool = True
