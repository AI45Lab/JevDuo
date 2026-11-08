"""Round/query-level runtime orchestration for AgentKit agents."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic

from northcua.core.budget import AgentBudgetPolicy, AgentBudgetScope, AgentStepBudget
from northcua.core.json_types import JsonObject
from northcua.core.terminal import AgentRunStatus, AgentTerminalDecision

from .cursor import AgentRuntimeCursor, AgentRuntimePhase
from .ports import (
    AgentRuntimeTerminalContext,
    AgentRuntimeTerminalPoint,
    ObservationT,
    PromptT,
    StepResultT,
    TerminalProjectionPort,
)
from .session import AgentSessionRuntime
from .step import AgentStepRuntime


@dataclass(frozen=True, slots=True)
class AgentRoundRunResult(Generic[StepResultT]):
    """Terminal result of one user-query round.

    Attributes:
        status: Domain-neutral run status.
        terminal_decision: Terminal decision selected by the round.
        steps: Completed step results in execution order.
    """

    status: AgentRunStatus
    terminal_decision: AgentTerminalDecision
    steps: tuple[StepResultT, ...]


@dataclass(frozen=True, slots=True)
class AgentRoundRuntimeSpec:
    """Runtime settings for one round/query.

    Attributes:
        query: User query being executed.
        budget: Step budget for this round/session.
        fallback_round_steps: Positive fallback when the budget has no explicit
            round limit.
        phase: Runtime phase used by the step loop.
        begin_new_round: Whether to start a fresh round before executing. Set
            to ``False`` when resuming a hydrated in-progress round.
        metadata: Per-round task policy metadata copied into each step context.
    """

    query: str
    budget: AgentStepBudget
    fallback_round_steps: int
    phase: AgentRuntimePhase = AgentRuntimePhase.MAIN
    begin_new_round: bool = True
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class AgentRoundRuntime(Generic[ObservationT, PromptT, StepResultT]):
    """Own a user-query round and drive step transactions.

    Attributes:
        session_runtime: Session-scoped runtime that owns counters and resource
            lifecycle.
        step_runtime: Step-scoped runtime that executes a single transaction.
        terminal_projection: Optional adapter that maps shared terminal
            decisions into domain step records.
        budget_policy: Shared policy used for round/session max-step decisions.
    """

    session_runtime: AgentSessionRuntime
    step_runtime: AgentStepRuntime[ObservationT, PromptT, StepResultT]
    terminal_projection: TerminalProjectionPort[StepResultT] | None = None
    budget_policy: AgentBudgetPolicy = field(default_factory=AgentBudgetPolicy)

    async def run(
        self,
        spec: AgentRoundRuntimeSpec,
        *,
        on_before_step: Callable[[int], None] | None = None,
        on_step: Callable[[StepResultT], None] | None = None,
    ) -> AgentRoundRunResult[StepResultT]:
        """Run a round until terminal decision or budget exhaustion.

        Args:
            spec: Query and budget for the active round.
            on_before_step: Optional callback before each step.
            on_step: Optional callback after each step.

        Returns:
            Terminal result with completed steps.
        """

        await self.session_runtime.hydrate()
        effective_budget = self._budget_for_phase(spec)
        starts_new_round = spec.begin_new_round or self.session_runtime.counters.current_round_num <= 0
        pre_decision = self._pre_step_terminal_decision(
            spec=spec,
            budget=effective_budget,
            starts_new_round=starts_new_round,
        )
        if pre_decision is not None:
            projected_steps = await self._project_pre_step_terminal(
                spec=spec,
                decision=pre_decision,
                starts_new_round=starts_new_round,
                on_step=on_step,
            )
            return AgentRoundRunResult(
                status=AgentRunStatus.from_terminal_decision(pre_decision),
                terminal_decision=pre_decision,
                steps=projected_steps,
            )

        await self.session_runtime.start()
        if starts_new_round:
            self.session_runtime.begin_round()
        terminal_decision, steps = await self._run_step_loop(
            spec=spec,
            budget=effective_budget,
            on_before_step=on_before_step,
            on_step=on_step,
        )
        if self.step_runtime.evaluation is not None:
            await self.step_runtime.evaluation.after_round(spec.query, steps, terminal_decision)
        return AgentRoundRunResult(
            status=AgentRunStatus.from_terminal_decision(terminal_decision),
            terminal_decision=terminal_decision,
            steps=steps,
        )

    async def _run_step_loop(
        self,
        *,
        spec: AgentRoundRuntimeSpec,
        budget: AgentStepBudget,
        on_before_step: Callable[[int], None] | None,
        on_step: Callable[[StepResultT], None] | None,
    ) -> tuple[AgentTerminalDecision, tuple[StepResultT, ...]]:
        """Run model/tool steps after preflight budget checks pass."""

        steps: list[StepResultT] = []
        remaining_steps = self.budget_policy.remaining_round_iterations(
            budget=budget,
            current_round_step=self._next_step_for_budget(spec, starts_new_round=False),
            current_session_step=self.session_runtime.counters.next_session_step,
            fallback_round_steps=spec.fallback_round_steps,
        )
        terminal_decision: AgentTerminalDecision | None = None
        last_cursor = None
        for step_offset in range(remaining_steps):
            cursor = self.session_runtime.reserve_step(spec.phase)
            last_cursor = cursor
            if on_before_step is not None:
                on_before_step(cursor.round_step if cursor.phase.counts_toward_session else cursor.phase_step)
            outcome = await self.step_runtime.run_step(
                cursor=cursor,
                query=spec.query,
                history=tuple(steps),
                metadata={
                    **spec.metadata,
                    "step_budget": self._step_budget_metadata(
                        spec=spec,
                        budget=budget,
                        cursor=cursor,
                    ),
                },
            )
            if outcome.should_advance:
                self.session_runtime.complete_step(cursor)
                await self.session_runtime.save()
            steps.append(outcome.result)
            terminal_decision = outcome.terminal_decision
            if terminal_decision.is_terminal:
                if on_step is not None:
                    on_step(outcome.result)
                break
            budget_decision = self.budget_policy.terminal_decision_after_step(
                budget=budget,
                round_step=cursor.round_step if cursor.phase.counts_toward_session else cursor.phase_step,
                session_step=cursor.session_step,
                final_answer=terminal_decision.final_answer,
            )
            if budget_decision is not None:
                terminal_decision = budget_decision
                projected = await self._project_terminal_rewrite(
                    query=spec.query,
                    decision=budget_decision,
                    point=AgentRuntimeTerminalPoint.POST_STEP_LIMIT,
                    cursor=cursor,
                    steps=tuple(steps),
                )
                if projected is not None:
                    steps[-1] = projected
                if on_step is not None:
                    on_step(steps[-1])
                break
            if step_offset < remaining_steps - 1 and on_step is not None:
                on_step(outcome.result)
        if terminal_decision is None or not terminal_decision.is_terminal:
            terminal_decision = AgentBudgetPolicy.max_step_decision(
                scope=AgentBudgetScope.ROUND,
                limit=budget.round_limit(spec.fallback_round_steps),
                current=len(steps),
                final_answer="Task reached the configured step limit before reaching a terminal state.",
            )
            projected = await self._project_terminal_rewrite(
                query=spec.query,
                decision=terminal_decision,
                point=AgentRuntimeTerminalPoint.LOOP_EXHAUSTED,
                cursor=last_cursor,
                steps=tuple(steps),
            )
            if projected is not None:
                steps[-1] = projected
            if steps and on_step is not None:
                on_step(steps[-1])
        return terminal_decision, tuple(steps)

    def _pre_step_terminal_decision(
        self,
        *,
        spec: AgentRoundRuntimeSpec,
        budget: AgentStepBudget,
        starts_new_round: bool,
    ) -> AgentTerminalDecision | None:
        """Return a budget terminal decision before reserving resources."""

        return self.budget_policy.terminal_decision_before_step(
            budget=budget,
            next_round_step=self._next_step_for_budget(spec, starts_new_round=starts_new_round),
            next_session_step=self.session_runtime.counters.next_session_step,
            final_answer="Task reached the configured step limit before running another step.",
        )

    async def _project_pre_step_terminal(
        self,
        *,
        spec: AgentRoundRuntimeSpec,
        decision: AgentTerminalDecision,
        starts_new_round: bool,
        on_step: Callable[[StepResultT], None] | None,
    ) -> tuple[StepResultT, ...]:
        """Project a pre-step terminal decision before starting session resources."""

        if self.terminal_projection is None:
            return tuple()
        cursor = self.session_runtime.reserve_step(spec.phase)
        projection = await self.terminal_projection.project_terminal(
            AgentRuntimeTerminalContext(
                query=spec.query,
                decision=decision,
                point=AgentRuntimeTerminalPoint.PRE_STEP_LIMIT,
                cursor=cursor,
            )
        )
        if projection is None:
            return tuple()
        if projection.should_advance:
            if starts_new_round:
                self.session_runtime.begin_round()
            self.session_runtime.complete_step(cursor)
            await self.session_runtime.save()
        if projection.emit_step_callback and on_step is not None:
            on_step(projection.result)
        return (projection.result,)

    async def _project_terminal_rewrite(
        self,
        *,
        query: str,
        decision: AgentTerminalDecision,
        point: AgentRuntimeTerminalPoint,
        cursor: AgentRuntimeCursor | None,
        steps: tuple[StepResultT, ...],
    ) -> StepResultT | None:
        """Project a terminal decision as a rewrite of the latest domain step."""

        if not steps or self.terminal_projection is None:
            return None
        projection = await self.terminal_projection.project_terminal(
            AgentRuntimeTerminalContext(
                query=query,
                decision=decision,
                point=point,
                cursor=cursor,
                steps=steps,
            )
        )
        if projection is None:
            return None
        return projection.result

    @staticmethod
    def _budget_for_phase(spec: AgentRoundRuntimeSpec) -> AgentStepBudget:
        """Return the budget that applies to the requested runtime phase."""

        if spec.phase.counts_toward_session:
            return spec.budget
        return AgentStepBudget(
            max_model_iterations_per_turn=spec.budget.max_model_iterations_per_turn,
            max_round_steps=spec.budget.max_round_steps,
            max_session_steps=0,
        )

    def _next_step_for_budget(self, spec: AgentRoundRuntimeSpec, *, starts_new_round: bool) -> int:
        """Return the counter used for round-like budget checks.

        Main-agent phases use the round counter. Non-main phases, such as
        evaluator/setup/cleanup, use their phase-local counter so they can be
        executed after a main round has already advanced.
        """

        if spec.phase.counts_toward_session:
            return 1 if starts_new_round else self.session_runtime.counters.next_round_step
        return self.session_runtime.counters.phase_steps.get(spec.phase, 1)

    def _step_budget_metadata(
        self,
        *,
        spec: AgentRoundRuntimeSpec,
        budget: AgentStepBudget,
        cursor: AgentRuntimeCursor,
    ) -> JsonObject:
        """Return prompt-safe budget metadata for the step being executed."""

        current_round_step = cursor.round_step if cursor.phase.counts_toward_session else cursor.phase_step
        remaining_round_steps = self.budget_policy.remaining_round_iterations(
            budget=budget,
            current_round_step=current_round_step,
            current_session_step=cursor.session_step,
            fallback_round_steps=spec.fallback_round_steps,
        )
        return {
            "phase": cursor.phase.value,
            "current_round_step": current_round_step,
            "round_limit": budget.round_limit(spec.fallback_round_steps),
            "remaining_round_steps": remaining_round_steps,
            "current_session_step": cursor.session_step,
            "session_limit": budget.max_session_steps,
        }


__all__ = ["AgentRoundRunResult", "AgentRoundRuntime", "AgentRoundRuntimeSpec"]
