"""Shared runner scaffolding for internal agents."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from northcua.agentinfra.runtime.contracts import SessionScopedFacade

TStepEvent = TypeVar("TStepEvent")
TRunResult = TypeVar("TRunResult")


@dataclass(slots=True, frozen=True)
class StepCallbackSet(Generic[TStepEvent]):
    """Shared callback bundle for step-based runners.

    Attributes:
        on_before_step: Optional callback fired before a step starts.
        on_step: Optional callback fired after a step event is produced.
    """

    on_before_step: Callable[[int], None] | None = None
    on_step: Callable[[TStepEvent], None] | None = None


class AgentRunnerBase(ABC, Generic[TStepEvent]):
    """Common callback plumbing for agent runners."""

    @staticmethod
    def emit_before_step(callbacks: StepCallbackSet[TStepEvent], step_index: int) -> None:
        """Emit the optional before-step callback.

        Args:
            callbacks: Callback bundle for the runner.
            step_index: One-based step index about to run.
        """

        if callbacks.on_before_step is not None:
            callbacks.on_before_step(step_index)

    @staticmethod
    def emit_step(callbacks: StepCallbackSet[TStepEvent], step_event: TStepEvent) -> None:
        """Emit the optional step event callback.

        Args:
            callbacks: Callback bundle for the runner.
            step_event: Step event produced by the runner.
        """

        if callbacks.on_step is not None:
            callbacks.on_step(step_event)


class AgentTaskRunner(AgentRunnerBase[TStepEvent], Generic[TStepEvent, TRunResult]):
    """Shared queue-style agent task runner scaffold."""

    def __init__(self, facade: SessionScopedFacade) -> None:
        """Initialize the task runner.

        Args:
            facade: Session-scoped facade backing the runner.
        """

        self.facade = facade

    async def drain_background_tasks(self) -> None:
        """Drain optional facade background tasks before runner shutdown."""

        drain = getattr(self.facade, "drain_background_tasks", None)
        if callable(drain):
            await drain()
