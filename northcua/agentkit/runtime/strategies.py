"""Default strategy adapters for the shared AgentKit runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable

from .ports import AgentRuntimeStepContext, ObservationPort, ObservationT, PromptPort, PromptT, SessionResourcePort

ObservationCollector = Callable[[AgentRuntimeStepContext[ObservationT, object]], ObservationT | Awaitable[ObservationT]]
PromptProjectorFn = Callable[[AgentRuntimeStepContext[ObservationT, PromptT]], PromptT]


class NoOpSessionResource(SessionResourcePort):
    """Session resource adapter that intentionally does nothing."""

    async def start(self) -> None:
        """Start no external resource."""

    async def close(self) -> None:
        """Close no external resource."""


@dataclass(slots=True)
class CallableObservationPort(ObservationPort[ObservationT]):
    """Observation port backed by a callable.

    Attributes:
        collect_fn: Sync or async callable that accepts a runtime step context
            and returns an observation payload.
    """

    collect_fn: ObservationCollector[ObservationT]

    async def collect(self, context: AgentRuntimeStepContext[ObservationT, object]) -> ObservationT:
        """Collect an observation via ``collect_fn``.

        Args:
            context: Runtime step context.

        Returns:
            Observation returned by the configured callable.
        """

        result = self.collect_fn(context)
        if isawaitable(result):
            return await result
        return result


@dataclass(slots=True)
class StaticPromptPort(PromptPort[Mapping[str, object], list[object]]):
    """Prompt port that returns a compact default message payload."""

    def project(self, context: AgentRuntimeStepContext[Mapping[str, object], list[object]]) -> list[object]:
        """Project query and observation into a simple prompt payload.

        Args:
            context: Runtime step context with query, observation, and history.

        Returns:
            Minimal list of message-like dictionaries for smoke tests.
        """

        return [
            {"role": "user", "content": context.query},
            {"role": "observation", "content": dict(context.observation or {})},
            {"role": "history", "content": [str(item) for item in context.history]},
        ]


@dataclass(slots=True)
class CallablePromptPort(PromptPort[ObservationT, PromptT]):
    """Prompt port backed by a synchronous callable.

    Attributes:
        project_fn: Callable that maps a runtime step context to a prompt
            envelope or message list.
    """

    project_fn: PromptProjectorFn[ObservationT, PromptT]

    def project(self, context: AgentRuntimeStepContext[ObservationT, PromptT]) -> PromptT:
        """Project a prompt via ``project_fn``.

        Args:
            context: Runtime step context with observation and history.

        Returns:
            Prompt payload produced by ``project_fn``.
        """

        return self.project_fn(context)


class InMemoryStepPersistence:
    """No-op persistence adapter for tests and simple low-code agents."""

    async def persist_step(self, context: AgentRuntimeStepContext[object, object], result: object) -> None:
        """Ignore a completed step.

        Args:
            context: Runtime step context.
            result: Domain-specific step result.
        """

        del context, result


class NoOpEvaluation:
    """No-op evaluation adapter for agents without post-run evaluators."""

    async def after_step(self, context: AgentRuntimeStepContext[object, object], result: object) -> None:
        """Ignore a completed step.

        Args:
            context: Runtime step context.
            result: Domain-specific step result.
        """

        del context, result

    async def after_round(self, query: str, steps: Sequence[object], terminal_decision: object) -> None:
        """Ignore a completed round.

        Args:
            query: User query for the completed round.
            steps: Completed step results.
            terminal_decision: Terminal decision selected by the round.
        """

        del query, steps, terminal_decision


__all__ = [
    "CallableObservationPort",
    "CallablePromptPort",
    "InMemoryStepPersistence",
    "NoOpEvaluation",
    "NoOpSessionResource",
    "StaticPromptPort",
]
