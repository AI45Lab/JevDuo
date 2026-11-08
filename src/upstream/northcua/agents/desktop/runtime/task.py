"""Desktop task runtime built on the shared AgentKit hierarchy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from northcua.agentkit.runtime import AgentRoundRuntimeSpec
from northcua.core.budget import AgentStepBudget
from northcua.core.enums import TaskStatus
from northcua.core.models import RoundInput, StepLogRecord
from northcua.core.terminal import AgentTerminalReason

from .contracts import DesktopRuntimeOptions
from .factory import DesktopRuntimeFactory

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DesktopTaskRunResult:
    """Structured result from one desktop shared-runtime task run.

    Attributes:
        steps: Step logs produced by the shared round runtime.
        terminal_status: Final desktop task status, when a step was produced.
        hit_step_limit: Whether the terminal decision came from max-step budget.
    """

    steps: list[StepLogRecord] = field(default_factory=list)
    terminal_status: TaskStatus | None = None
    hit_step_limit: bool = False


class DesktopTaskFacade(Protocol):
    """Facade surface consumed by ``DesktopTaskRuntime``."""

    @property
    def session_id(self) -> str:
        """Return the active desktop session id.

        Returns:
            Stable desktop session id.
        """
        ...

    @property
    def runtime_factory(self) -> DesktopRuntimeFactory:
        """Return the runtime factory for this facade.

        Returns:
            Factory that constructs shared-runtime round instances.
        """
        ...

    async def enqueue_initial_request(self, query: str, metadata: dict[str, object] | None = None) -> None:
        """Queue the initial desktop request.

        Args:
            query: User request text.
            metadata: Optional request metadata for remote/CUA adapters.
        """
        ...

    async def next_round(self) -> RoundInput | None:
        """Return the next queued round input.

        Returns:
            Queued round input, or ``None`` when the queue is empty.
        """
        ...

    async def drain_background_tasks(self) -> None:
        """Drain async evaluator work."""
        ...


@dataclass(slots=True)
class DesktopTaskRuntime:
    """Run desktop tasks through ``AgentSessionRuntime``/``AgentRoundRuntime``.

    Attributes:
        facade: Desktop facade that provides request queueing and runtime wiring.
    """

    facade: DesktopTaskFacade

    async def run(
        self,
        query: str,
        *,
        max_steps: int,
        max_session_steps: int = 0,
        metadata: dict[str, object] | None = None,
        options: DesktopRuntimeOptions | None = None,
        on_before_step: Callable[[int], None] | None = None,
        on_step: Callable[[StepLogRecord], None] | None = None,
        drain_background_tasks: bool = True,
    ) -> DesktopTaskRunResult:
        """Queue and run one desktop query to a terminal shared-runtime decision.

        Args:
            query: User request text.
            max_steps: Per-round step limit.
            max_session_steps: Optional cumulative session step limit.
            metadata: Request metadata forwarded to the desktop round queue.
            options: Local/remote runtime strategy overrides.
            on_before_step: Optional callback before each shared-runtime step.
            on_step: Optional callback after each emitted desktop step log.
            drain_background_tasks: Whether to drain async evaluator tasks before
                returning or re-raising.

        Returns:
            Desktop task run result with produced steps and terminal status.
        """

        await self.facade.enqueue_initial_request(query, metadata=metadata)
        round_input = await self.facade.next_round()
        if round_input is None:
            if drain_background_tasks:
                await self.facade.drain_background_tasks()
            return DesktopTaskRunResult()
        return await self.run_round_input(
            round_input,
            max_steps=max_steps,
            max_session_steps=max_session_steps,
            options=options,
            on_before_step=on_before_step,
            on_step=on_step,
            drain_background_tasks=drain_background_tasks,
        )

    async def run_round_input(
        self,
        round_input: RoundInput,
        *,
        max_steps: int,
        max_session_steps: int = 0,
        options: DesktopRuntimeOptions | None = None,
        on_before_step: Callable[[int], None] | None = None,
        on_step: Callable[[StepLogRecord], None] | None = None,
        drain_background_tasks: bool = True,
    ) -> DesktopTaskRunResult:
        """Run an already queued desktop round input through shared runtime.

        Args:
            round_input: Queued desktop round input.
            max_steps: Per-round step limit.
            max_session_steps: Optional cumulative session step limit.
            options: Local/remote runtime strategy overrides.
            on_before_step: Optional callback before each shared-runtime step.
            on_step: Optional callback after each emitted desktop step log.
            drain_background_tasks: Whether to drain async evaluator tasks before
                returning or re-raising.

        Returns:
            Desktop task run result with produced steps and terminal status.

        Raises:
            Exception: Re-raises shared-runtime failures after optional evaluator
                drain.
        """

        runtime_options = options or DesktopRuntimeOptions()
        effective_max_steps = max(1, max_steps)
        round_runtime = self.facade.runtime_factory.build_round_runtime(options=runtime_options)
        try:
            round_result = await round_runtime.run(
                AgentRoundRuntimeSpec(
                    query=round_input.query,
                    budget=AgentStepBudget(
                        max_round_steps=effective_max_steps,
                        max_session_steps=max_session_steps,
                    ),
                    fallback_round_steps=effective_max_steps,
                    begin_new_round=False,
                    metadata=dict(round_input.metadata),
                ),
                on_before_step=on_before_step,
                on_step=on_step,
            )
        except Exception:
            logger.exception("Desktop shared task runtime failed; draining background tasks before re-raising")
            if drain_background_tasks:
                await self.facade.drain_background_tasks()
            raise
        steps = list(round_result.steps)
        if drain_background_tasks:
            await self.facade.drain_background_tasks()
        terminal_status = steps[-1].status if steps else None
        return DesktopTaskRunResult(
            steps=steps,
            terminal_status=terminal_status,
            hit_step_limit=round_result.terminal_decision.reason == AgentTerminalReason.MAX_STEPS,
        )


__all__ = ["DesktopTaskFacade", "DesktopTaskRunResult", "DesktopTaskRuntime"]
