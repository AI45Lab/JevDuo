"""Typed ports used by the shared AgentKit runtime hierarchy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Protocol, TypeVar

from northcua.core.json_types import JsonObject
from northcua.core.terminal import AgentTerminalDecision

from .cursor import AgentRuntimeCursor

ObservationT = TypeVar("ObservationT")
PromptT = TypeVar("PromptT")
ModelResultT = TypeVar("ModelResultT")
ToolResultT = TypeVar("ToolResultT")
StepResultT = TypeVar("StepResultT")
ModelResultT_co = TypeVar("ModelResultT_co", covariant=True)
ToolModelResultT_contra = TypeVar("ToolModelResultT_contra", contravariant=True)
ToolResultT_co = TypeVar("ToolResultT_co", covariant=True)
StepResultT_contra = TypeVar("StepResultT_contra", contravariant=True)


@dataclass(frozen=True, slots=True)
class AgentRuntimeStepContext(Generic[ObservationT, PromptT]):
    """Context shared by one runtime step transaction.

    Attributes:
        cursor: Runtime cursor for this step.
        query: User query for the active round.
        history: Prior step results retained by the round runtime.
        observation: Latest observation, when already collected.
        prompt: Prompt envelope/messages, when already projected.
        metadata: Additional JSON-compatible runtime metadata.
    """

    cursor: AgentRuntimeCursor
    query: str
    history: Sequence[object] = ()
    observation: ObservationT | None = None
    prompt: PromptT | None = None
    metadata: JsonObject = field(default_factory=dict)

    @property
    def step_index(self) -> int:
        """Return the one-based step index for the current phase.

        Returns:
            ``cursor.round_step`` for main-agent phases, otherwise
            ``cursor.phase_step``. Callers that need the main-agent anchor can
            read ``cursor.round_step`` explicitly.
        """

        if self.cursor.phase.counts_toward_session:
            return self.cursor.round_step
        return self.cursor.phase_step

    @property
    def session_step(self) -> int:
        """Return the one-based main-agent session step.

        Returns:
            ``cursor.session_step`` for main-agent phases. Evaluator/setup
            phases may carry the same value without consuming it.
        """

        return self.cursor.session_step


@dataclass(frozen=True, slots=True)
class AgentRuntimeStepOutcome(Generic[StepResultT]):
    """Result of one shared step transaction.

    Attributes:
        cursor: Runtime cursor used for the step.
        result: Domain-specific persisted or panel-ready step result.
        terminal_decision: Shared decision derived from the step.
        summary: Human-readable summary for logs and callbacks.
        should_advance: Whether the owning session runtime should advance
            counters after this transaction.
    """

    cursor: AgentRuntimeCursor
    result: StepResultT
    terminal_decision: AgentTerminalDecision
    summary: str = ""
    should_advance: bool = True


class AgentRuntimeTerminalPoint(str, Enum):
    """Where a round-level terminal decision was selected.

    Concrete agents can use this stable value to project budget or loop
    exhaustion decisions back into their native persisted record without
    duplicating the step loop.
    """

    PRE_STEP_LIMIT = "pre_step_limit"
    POST_STEP_LIMIT = "post_step_limit"
    LOOP_EXHAUSTED = "loop_exhausted"


@dataclass(frozen=True, slots=True)
class AgentRuntimeTerminalContext(Generic[StepResultT]):
    """Context for projecting a runtime terminal decision into an agent record.

    Attributes:
        query: User query attached to the active round.
        decision: Shared terminal decision selected by the round runtime.
        point: Loop location that selected the terminal decision.
        cursor: Reserved cursor for synthetic records, when one exists.
        steps: Completed domain step records available for rewrite decisions.
        metadata: Optional JSON-compatible adapter metadata.
    """

    query: str
    decision: AgentTerminalDecision
    point: AgentRuntimeTerminalPoint
    cursor: AgentRuntimeCursor | None = None
    steps: Sequence[StepResultT] = ()
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRuntimeTerminalProjection(Generic[StepResultT]):
    """Projected terminal record returned by a concrete agent adapter.

    Attributes:
        result: Domain-specific terminal step record.
        should_advance: Whether session counters should advance for this
            projected record. Pre-step synthetic terminal records usually
            advance; post-step rewrites do not.
        emit_step_callback: Whether the round runtime should emit the step
            callback for this projected record.
    """

    result: StepResultT
    should_advance: bool = False
    emit_step_callback: bool = True


class TerminalProjectionPort(Protocol[StepResultT]):
    """Project shared terminal decisions into concrete agent step records."""

    async def project_terminal(
        self,
        context: AgentRuntimeTerminalContext[StepResultT],
    ) -> AgentRuntimeTerminalProjection[StepResultT] | None:
        """Return a terminal projection or ``None`` to keep default behavior.

        Args:
            context: Shared terminal decision plus domain-specific cursor/step
                data that the adapter can project into a native record.

        Returns:
            Domain terminal record override, or ``None`` when the shared runtime
            should return the decision without emitting or rewriting a step.
        """
        ...


class SessionResourcePort(Protocol):
    """Optional platform/session resource managed at session scope."""

    async def start(self) -> None:
        """Start or attach to the session resource."""

    async def close(self) -> None:
        """Close the session resource idempotently."""


class ObservationPort(Protocol[ObservationT]):
    """Collect the latest runtime observation for one step."""

    async def collect(self, context: AgentRuntimeStepContext[ObservationT, object]) -> ObservationT:
        """Collect observation data for ``context``.

        Args:
            context: Runtime step context containing cursor, query, and history.

        Returns:
            Domain-specific observation payload for prompt projection and
            artifact persistence.
        """
        ...


class PromptPort(Protocol[ObservationT, PromptT]):
    """Project a prompt from query, observation, and history."""

    def project(self, context: AgentRuntimeStepContext[ObservationT, PromptT]) -> PromptT:
        """Build a prompt envelope or message list for ``context``.

        Args:
            context: Runtime step context with observation and prior results.

        Returns:
            Provider-neutral or provider-specific prompt payload consumed by the
            model step port.
        """
        ...


class ModelStepPort(Protocol[PromptT, ModelResultT_co]):
    """Run one model step using a prompt."""

    async def run_model_step(self, context: AgentRuntimeStepContext[object, PromptT]) -> ModelResultT_co:
        """Run the model for ``context``.

        Args:
            context: Runtime step context carrying the prompt payload.

        Returns:
            Model response object produced by the concrete agent runtime.
        """
        ...


class ToolDispatchPort(Protocol[ToolModelResultT_contra, ToolResultT_co]):
    """Resolve or dispatch tool calls from a model result."""

    async def dispatch_tools(
        self,
        context: AgentRuntimeStepContext[object, object],
        model_result: ToolModelResultT_contra,
    ) -> ToolResultT_co:
        """Execute or resolve tool calls for ``model_result``.

        Args:
            context: Runtime step context for trace and dispatch metadata.
            model_result: Model response containing tool calls or externalized
                tool-call metadata.

        Returns:
            Domain-specific tool result summary.
        """
        ...


class StepPersistencePort(Protocol[StepResultT_contra]):
    """Persist one step result and related traces/artifacts."""

    async def persist_step(
        self,
        context: AgentRuntimeStepContext[object, object],
        result: StepResultT_contra,
    ) -> None:
        """Persist a step result for later trace replay.

        Args:
            context: Runtime step context for artifact paths and cursor fields.
            result: Domain-specific step record to persist.
        """
        ...


class EvaluationPort(Protocol[StepResultT_contra]):
    """Run post-step or post-round evaluation hooks."""

    async def after_step(
        self,
        context: AgentRuntimeStepContext[object, object],
        result: StepResultT_contra,
    ) -> None:
        """Run after one persisted step.

        Args:
            context: Runtime step context for the completed step.
            result: Domain-specific step result.
        """
        ...

    async def after_round(
        self,
        query: str,
        steps: Sequence[StepResultT_contra],
        terminal_decision: AgentTerminalDecision,
    ) -> None:
        """Run after a round reaches a terminal decision.

        Args:
            query: User query for the completed round.
            steps: Completed step results in execution order.
            terminal_decision: Shared terminal decision selected by the round.
        """
        ...


StepExecutor = Callable[
    [AgentRuntimeStepContext[ObservationT, PromptT]],
    Awaitable[AgentRuntimeStepOutcome[StepResultT]],
]


class RuntimeStateStorePort(Protocol):
    """Persist runtime counter state independently of concrete agent domains."""

    async def hydrate(self) -> None:
        """Refresh state from the authoritative backing store."""

    async def save(self) -> None:
        """Persist the current state to the backing store."""


__all__ = [
    "AgentRuntimeStepContext",
    "AgentRuntimeStepOutcome",
    "AgentRuntimeTerminalContext",
    "AgentRuntimeTerminalPoint",
    "AgentRuntimeTerminalProjection",
    "EvaluationPort",
    "ModelStepPort",
    "ObservationPort",
    "PromptPort",
    "RuntimeStateStorePort",
    "SessionResourcePort",
    "StepExecutor",
    "StepPersistencePort",
    "TerminalProjectionPort",
    "ToolDispatchPort",
]
