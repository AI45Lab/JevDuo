"""Step-level transaction runtime for AgentKit agents."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, cast

from northcua.core.json_types import JsonObject
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason

from .cursor import AgentRuntimeCursor
from .ports import (
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    EvaluationPort,
    ObservationT,
    PromptT,
    StepExecutor,
    StepPersistencePort,
    StepResultT,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentStepRuntime(Generic[ObservationT, PromptT, StepResultT]):
    """Execute and persist one runtime step transaction.

    Attributes:
        step_executor: Agent/domain-specific step executor.
        persistence: Optional persistence adapter.
        evaluation: Optional evaluation/post-step adapter.
    """

    step_executor: StepExecutor[ObservationT, PromptT, StepResultT]
    persistence: StepPersistencePort[StepResultT] | None = None
    evaluation: EvaluationPort[StepResultT] | None = None

    async def run_step(
        self,
        *,
        cursor: AgentRuntimeCursor,
        query: str,
        history: Sequence[StepResultT],
        observation: ObservationT | None = None,
        prompt: PromptT | None = None,
        metadata: JsonObject | None = None,
    ) -> AgentRuntimeStepOutcome[StepResultT]:
        """Run one agent step and persist hooks.

        Args:
            cursor: Runtime cursor for this step.
            query: User query for the active round.
            history: Prior completed step results.
            observation: Optional observation collected before calling the
                executor.
            prompt: Optional prompt projected before calling the executor.
            metadata: Optional runtime metadata visible to the step executor.

        Returns:
            Step outcome with a domain result and shared terminal decision.

        Raises:
            Exception: Propagates the domain-specific executor exception after
                a debug log. Outer agent runners remain responsible for mapping
                failures into their public result contracts.
        """

        context: AgentRuntimeStepContext[ObservationT, PromptT] = AgentRuntimeStepContext(
            cursor=cursor,
            query=query,
            history=tuple(history),
            observation=observation,
            prompt=prompt,
            metadata=dict(metadata or {}),
        )
        try:
            outcome = await self.step_executor(context)
        except Exception:
            logger.debug(
                "agent runtime step failed",
                extra={
                    "session_id": cursor.session_id,
                    "round_num": cursor.round_num,
                    "round_step": cursor.round_step,
                    "phase": cursor.phase.value,
                },
                exc_info=True,
            )
            raise
        if self.persistence is not None:
            await self.persistence.persist_step(cast(AgentRuntimeStepContext[object, object], context), outcome.result)
        if self.evaluation is not None:
            await self.evaluation.after_step(cast(AgentRuntimeStepContext[object, object], context), outcome.result)
        return outcome


def no_tool_call_decision(response_text: str = "") -> AgentTerminalDecision:
    """Build the standard no-tool-call failure decision.

    Args:
        response_text: Assistant text returned without an executable tool call.

    Returns:
        Shared retryable no-tool-call failure decision.
    """

    return AgentTerminalDecision.fail(
        reason=AgentTerminalReason.NO_TOOL_CALL,
        final_answer=response_text,
        summary="The model did not produce an executable tool call.",
        retryable=True,
    )


__all__ = ["AgentStepRuntime", "no_tool_call_decision"]
