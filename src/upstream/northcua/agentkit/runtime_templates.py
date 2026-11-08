"""Reusable runtime templates for simple low-code AgentKit agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from northcua.core.budget import AgentBudgetPolicy, AgentBudgetScope, AgentStepBudget
from northcua.core.terminal import AgentRunStatus, AgentTerminalDecision, AgentTerminalReason

from .runtime import (
    AgentRoundRuntime,
    AgentRoundRuntimeSpec,
    AgentRuntimeCounterState,
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentSessionRuntime,
    AgentStepRuntime,
    SessionResourcePort,
)
from .spec import AgentSpec, ArtifactLayoutSpec, ObservationProvider, PromptProjector, SessionAdapter, TerminalPolicy


@dataclass(slots=True, frozen=True)
class AgentArtifactPaths:
    """Resolved common artifact paths for a single AgentKit session.

    Attributes:
        root: Artifact root for the session.
        result_path: Canonical final result JSON path.
        trace_path: Structured per-step trace path.
        runtime_log_path: Runtime event JSONL path.
        llm_trace_log_path: Provider/model trace JSONL path.
        screenshot_dir: Directory for screenshot artifacts.
        dom_dir: Directory for DOM or accessibility snapshot artifacts.
        replay_evidence_path: Optional replay/evidence report path.
        error_table_path: Optional structured error table path.
    """

    root: Path
    result_path: Path
    trace_path: Path
    runtime_log_path: Path
    llm_trace_log_path: Path
    context_summary_llm_trace_log_path: Path
    context_compaction_log_path: Path
    screenshot_dir: Path
    dom_dir: Path
    replay_evidence_path: Path
    error_table_path: Path

    @classmethod
    def from_layout(cls, root: Path, layout: ArtifactLayoutSpec) -> "AgentArtifactPaths":
        """Build common artifact paths from a root directory and layout spec.

        Args:
            root: Session artifact root directory.
            layout: Relative path names declared by the agent spec.

        Returns:
            Fully resolved artifact paths under ``root``.
        """
        return cls(
            root=root,
            result_path=root / layout.result_name,
            trace_path=root / layout.trace_name,
            runtime_log_path=root / layout.runtime_log_name,
            llm_trace_log_path=root / layout.llm_trace_name,
            context_summary_llm_trace_log_path=root / "context_summary_llm_trace.jsonl",
            context_compaction_log_path=root / "context_compaction.jsonl",
            screenshot_dir=root / layout.screenshot_dir,
            dom_dir=root / layout.dom_dir,
            replay_evidence_path=root / layout.replay_name,
            error_table_path=root / layout.error_table_name,
        )


@dataclass(slots=True, frozen=True)
class StepwiseToolStepContext:
    """Context passed to a low-code step executor.

    Attributes:
        spec: Registered agent spec that owns the current run.
        session: Open session adapter used for tool/probe execution.
        step_index: One-based step index for this round.
        session_step: One-based step index across the whole session.
        query: User task being solved.
        observation: Latest observation collected by the provider.
        messages: Prompt messages projected for the model call.
    """

    spec: AgentSpec
    session: SessionAdapter
    step_index: int
    session_step: int
    query: str
    observation: Mapping[str, object]
    messages: list[object]


@dataclass(slots=True, frozen=True)
class StepwiseToolStepResult:
    """Result returned by a low-code step executor.

    Attributes:
        step_index: One-based step index; ``0`` lets the template fill it in.
        status: Machine-readable status string for this step.
        summary: Human-readable step summary for terminal panels.
        output: Additional structured step data.
        done: Whether this step completed the user task.
        terminal_decision: Optional shared terminal decision. When omitted,
            the template preserves legacy ``done`` semantics.
    """

    step_index: int = 0
    status: str = "ok"
    summary: str = ""
    output: Mapping[str, object] = field(default_factory=dict)
    done: bool = False
    terminal_decision: AgentTerminalDecision | None = None

    def normalized_terminal_decision(self) -> AgentTerminalDecision:
        """Return the explicit or legacy-derived terminal decision.

        Returns:
            Shared terminal decision used by low-code runtime templates.
        """
        if self.terminal_decision is not None:
            return self.terminal_decision
        if self.done:
            return AgentTerminalDecision.finish(
                final_answer=self.summary, summary=self.summary or "The low-code agent completed the task."
            )
        if self.status.lower() in {"error", "fail", "failed"}:
            return AgentTerminalDecision.fail(
                reason=AgentTerminalReason.RUNTIME_ERROR,
                final_answer=self.summary,
                summary=self.summary or "The low-code step failed.",
                retryable=True,
            )
        return AgentTerminalDecision.continue_(
            final_answer=self.summary, summary=self.summary or "Further action is required."
        )


@dataclass(slots=True, frozen=True)
class StepwiseToolRunResult:
    """Terminal result for ``StepwiseToolAgentTemplate``.

    Attributes:
        status: Domain-neutral terminal run status.
        terminal_decision: Shared terminal decision for controller, CLI, and
            telemetry consumers.
        steps: Immutable sequence of completed step results.
        observations: Immutable sequence of observations collected per step.
    """

    status: AgentRunStatus
    terminal_decision: AgentTerminalDecision
    steps: tuple[StepwiseToolStepResult, ...]
    observations: tuple[Mapping[str, object], ...]

    def to_terminal_decision(self) -> AgentTerminalDecision:
        """Return the top-level terminal decision for shared adapters.

        Returns:
            Domain-neutral terminal decision selected by the template.
        """
        return self.terminal_decision


StepExecutor = Callable[[StepwiseToolStepContext], Awaitable[StepwiseToolStepResult]]


@dataclass(slots=True)
class _SessionAdapterResource(SessionResourcePort):
    """Runtime resource adapter around the public low-code session adapter."""

    session: SessionAdapter

    async def start(self) -> None:
        """Start the wrapped low-code session adapter."""
        await self.session.start()

    async def close(self) -> None:
        """Close the wrapped low-code session adapter."""
        await self.session.close()


class StepwiseToolAgentTemplate:
    """Minimal reusable loop for simple probe/prompt/tool agents.

    The template owns lifecycle, observation collection, prompt projection, max
    step handling, and terminal policy. A production agent supplies the
    ``step_executor`` that calls its model/tool surface.
    """

    def __init__(
        self,
        *,
        spec: AgentSpec,
        session: SessionAdapter | None = None,
        observation_provider: ObservationProvider | None = None,
        prompt_projector: PromptProjector | None = None,
        terminal_policy: TerminalPolicy | None = None,
        step_executor: StepExecutor | None = None,
    ) -> None:
        self.spec = spec
        self.session = session or SessionAdapter(agent_kind=spec.kind)
        self.observation_provider = observation_provider or ObservationProvider()
        self.prompt_projector = prompt_projector or PromptProjector()
        self.terminal_policy = terminal_policy or TerminalPolicy()
        self.step_executor = step_executor or self._default_step_executor

    async def run(
        self,
        *,
        query: str,
        max_steps: int,
        max_session_steps: int = 0,
        session_step_offset: int = 0,
        on_before_step: Callable[[int], None] | None = None,
        on_step: Callable[[StepwiseToolStepResult], None] | None = None,
    ) -> StepwiseToolRunResult:
        """Run the generic stepwise loop.

        Args:
            query: User task for the low-code agent.
            max_steps: Maximum number of steps for the current round/query.
            max_session_steps: Optional maximum number of steps across the
                session. ``0`` disables the session-level budget.
            session_step_offset: Number of steps already persisted before this
                run starts.
            on_before_step: Optional callback invoked before each observation.
            on_step: Optional callback invoked after each step result.

        Returns:
            Terminal run result containing all step outputs and observations.
        """
        steps: list[StepwiseToolStepResult] = []
        observations: list[Mapping[str, object]] = []
        budget = AgentStepBudget(max_round_steps=max_steps, max_session_steps=max_session_steps)
        budget_policy = AgentBudgetPolicy()
        counters = AgentRuntimeCounterState(
            session_id=self.session.session_id or f"{self.spec.kind}-session", next_session_step=session_step_offset + 1
        )
        session_runtime = AgentSessionRuntime(counters=counters, resource=_SessionAdapterResource(self.session))

        async def _execute_step(
            context: AgentRuntimeStepContext[Mapping[str, object], list[object]],
        ) -> AgentRuntimeStepOutcome[StepwiseToolStepResult]:
            return await self._execute_stepwise_runtime_step(
                context,
                observations=observations,
                budget_policy=budget_policy,
                budget=budget,
                max_steps=max_steps,
            )

        try:
            preflight_result = self._preflight_budget_result(
                budget_policy=budget_policy,
                budget=budget,
                session_step_offset=session_step_offset,
            )
            if preflight_result is not None:
                if on_step is not None:
                    on_step(preflight_result.steps[0])
                return preflight_result
            await session_runtime.start()
            round_runtime = AgentRoundRuntime(
                session_runtime=session_runtime,
                step_runtime=cast(
                    AgentStepRuntime[object, object, StepwiseToolStepResult],
                    AgentStepRuntime(step_executor=_execute_step),
                ),
            )
            round_result = await round_runtime.run(
                AgentRoundRuntimeSpec(query=query, budget=budget, fallback_round_steps=max_steps),
                on_before_step=on_before_step,
                on_step=on_step,
            )
            steps = list(round_result.steps)
            final_decision = round_result.terminal_decision
            status = round_result.status
            return StepwiseToolRunResult(
                status=status, terminal_decision=final_decision, steps=tuple(steps), observations=tuple(observations)
            )
        finally:
            await session_runtime.close()

    async def _execute_stepwise_runtime_step(
        self,
        context: AgentRuntimeStepContext[Mapping[str, object], list[object]],
        *,
        observations: list[Mapping[str, object]],
        budget_policy: AgentBudgetPolicy,
        budget: AgentStepBudget,
        max_steps: int,
    ) -> AgentRuntimeStepOutcome[StepwiseToolStepResult]:
        step_index = context.cursor.round_step
        session_step = context.cursor.session_step
        observation = await self.observation_provider.collect(session=self.session, step_index=step_index)
        observations.append(observation)
        messages = self.prompt_projector.project(
            query=context.query, observation=observation, history=tuple(context.history)
        )
        result = await self.step_executor(
            StepwiseToolStepContext(
                spec=self.spec,
                session=self.session,
                step_index=step_index,
                session_step=session_step,
                query=context.query,
                observation=observation,
                messages=messages,
            )
        )
        if result.step_index <= 0:
            result = replace(result, step_index=step_index)
        terminal_decision = result.normalized_terminal_decision()
        if not terminal_decision.is_terminal:
            budget_decision = budget_policy.terminal_decision_after_step(
                budget=budget, round_step=step_index, session_step=session_step, final_answer=result.summary
            )
            if budget_decision is not None:
                result = replace(result, terminal_decision=budget_decision)
                terminal_decision = budget_decision
        if not terminal_decision.is_terminal and self.terminal_policy.should_stop(
            step_index=step_index, max_steps=max_steps, result=result
        ):
            terminal_decision = AgentBudgetPolicy.max_step_decision(
                scope=AgentBudgetScope.ROUND,
                limit=max_steps,
                current=step_index,
                final_answer=result.summary
                or "Task reached the configured step limit before reaching a terminal state.",
            )
            result = replace(result, terminal_decision=terminal_decision)
        return AgentRuntimeStepOutcome(
            cursor=context.cursor, result=result, terminal_decision=terminal_decision, summary=result.summary
        )

    @staticmethod
    def _preflight_budget_result(
        *,
        budget_policy: AgentBudgetPolicy,
        budget: AgentStepBudget,
        session_step_offset: int,
    ) -> StepwiseToolRunResult | None:
        pre_decision = budget_policy.terminal_decision_before_step(
            budget=budget,
            next_round_step=1,
            next_session_step=session_step_offset + 1,
            final_answer="Task reached the configured step limit before running another step.",
        )
        if pre_decision is None:
            return None
        result = StepwiseToolStepResult(
            step_index=1, status="max_steps", summary=pre_decision.final_answer, terminal_decision=pre_decision
        )
        return StepwiseToolRunResult(
            status=AgentRunStatus.MAX_STEPS,
            terminal_decision=pre_decision,
            steps=(result,),
            observations=tuple(),
        )

    async def _default_step_executor(self, context: StepwiseToolStepContext) -> StepwiseToolStepResult:
        """Return a no-op step result for harness smoke tests."""
        return StepwiseToolStepResult(
            step_index=context.step_index,
            status="ok",
            summary=f"{context.spec.kind} observed step {context.step_index}",
            output={"messages": len(context.messages)},
            done=True,
        )
