"""Composable port-based step pipeline for AgentKit runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Generic, cast

from northcua.core.terminal import AgentTerminalDecision

from .ports import (
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    ModelResultT,
    ModelStepPort,
    ObservationPort,
    ObservationT,
    PromptPort,
    PromptT,
    StepResultT,
    ToolDispatchPort,
    ToolResultT,
)

StepResultBuilder = Callable[[AgentRuntimeStepContext[ObservationT, PromptT], ModelResultT, ToolResultT], StepResultT]
TerminalDecisionBuilder = Callable[
    [AgentRuntimeStepContext[ObservationT, PromptT], ModelResultT, ToolResultT],
    AgentTerminalDecision,
]


@dataclass(slots=True)
class PortStepPipelineExecutor(Generic[ObservationT, PromptT, ModelResultT, ToolResultT, StepResultT]):
    """Compose runtime ports into a complete step executor.

    Agents with simple lifecycles can provide independent observation, prompt,
    model, and tool ports instead of hand-writing one large closure.

    Attributes:
        observation_port: Port that collects step observations.
        prompt_port: Port that projects observations into prompts.
        model_step_port: Port that invokes the model.
        tool_dispatch_port: Port that resolves or executes model tool calls.
        result_builder: Callable that builds a domain step result.
        terminal_decision_builder: Callable that builds the shared terminal
            decision from model/tool outputs.
    """

    observation_port: ObservationPort[ObservationT]
    prompt_port: PromptPort[ObservationT, PromptT]
    model_step_port: ModelStepPort[PromptT, ModelResultT]
    tool_dispatch_port: ToolDispatchPort[ModelResultT, ToolResultT]
    result_builder: StepResultBuilder[ObservationT, PromptT, ModelResultT, ToolResultT, StepResultT]
    terminal_decision_builder: TerminalDecisionBuilder[ObservationT, PromptT, ModelResultT, ToolResultT]

    async def __call__(
        self,
        context: AgentRuntimeStepContext[ObservationT, PromptT],
    ) -> AgentRuntimeStepOutcome[StepResultT]:
        """Run one observation -> prompt -> model -> tools step.

        Args:
            context: Runtime context with cursor, query, and prior step history.

        Returns:
            Shared runtime step outcome.
        """

        observation = await self.observation_port.collect(cast(AgentRuntimeStepContext[ObservationT, object], context))
        observed_context = replace(context, observation=observation)
        prompt = self.prompt_port.project(observed_context)
        prompted_context = replace(observed_context, prompt=prompt)
        model_result = await self.model_step_port.run_model_step(
            cast(AgentRuntimeStepContext[object, PromptT], prompted_context)
        )
        tool_result = await self.tool_dispatch_port.dispatch_tools(
            cast(AgentRuntimeStepContext[object, object], prompted_context),
            model_result,
        )
        step_result = self.result_builder(prompted_context, model_result, tool_result)
        terminal_decision = self.terminal_decision_builder(prompted_context, model_result, tool_result)
        return AgentRuntimeStepOutcome(
            cursor=context.cursor,
            result=step_result,
            terminal_decision=terminal_decision,
            summary=terminal_decision.summary,
        )


__all__ = ["PortStepPipelineExecutor", "StepResultBuilder", "TerminalDecisionBuilder"]
