"""Terminal projection adapter for desktop max-step semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from northcua.agentkit.runtime import (
    AgentRuntimeTerminalContext,
    AgentRuntimeTerminalPoint,
    AgentRuntimeTerminalProjection,
    TerminalProjectionPort,
)
from northcua.core.enums import TaskStatus
from northcua.core.models import StepLogRecord
from northcua.core.terminal import AgentTerminalDecision
from northcua.transport.ws.remote_round import ExternalToolDispatcher


class DesktopTerminalPersistenceFacade(Protocol):
    """Facade methods required for terminal projection persistence."""

    async def persist_step_limit_terminal(
        self,
        *,
        request: str,
        step_log: StepLogRecord,
        decision: AgentTerminalDecision | None = None,
        advance_counters: bool = True,
        remote_tool_dispatcher: ExternalToolDispatcher | None = None,
    ) -> None:
        """Persist verification, evaluator, and artifact rewrites for max-step.

        Args:
            request: User query associated with the terminal override.
            step_log: Synthetic or copied step log to persist.
            decision: Optional shared terminal decision to apply first.
            advance_counters: Whether legacy callers should advance counters.
            remote_tool_dispatcher: Optional remote dispatcher used for final
                verification/evaluator client tools.
        """
        ...


@dataclass(slots=True)
class DesktopTerminalProjectionPort(TerminalProjectionPort[StepLogRecord]):
    """Project shared max-step decisions into desktop ``StepLogRecord`` values.

    Attributes:
        facade: Persistence facade that owns verification/evaluator side effects.
        remote_tool_dispatcher: Optional remote dispatcher for max-step final
            verification and evaluator live tools.
    """

    facade: DesktopTerminalPersistenceFacade
    remote_tool_dispatcher: ExternalToolDispatcher | None = None

    async def project_terminal(
        self,
        context: AgentRuntimeTerminalContext[StepLogRecord],
    ) -> AgentRuntimeTerminalProjection[StepLogRecord] | None:
        """Create or rewrite desktop step logs for runtime terminal decisions.

        Args:
            context: Shared terminal context containing decision, cursor, and
                prior steps.

        Returns:
            Desktop terminal projection, or ``None`` if no step can be emitted.
        """

        if context.point == AgentRuntimeTerminalPoint.PRE_STEP_LIMIT:
            if context.cursor is None:
                return None
            step_log = StepLogRecord(
                session_id=context.cursor.session_id,
                session_step=context.cursor.session_step,
                round_step=context.cursor.round_step,
                round_num=max(1, context.cursor.round_num),
                status=TaskStatus.FAIL,
                request=context.query,
                result=context.decision.final_answer,
                terminal_decision=context.decision.to_json(),
            )
            await self.facade.persist_step_limit_terminal(
                request=context.query,
                step_log=step_log,
                decision=context.decision,
                advance_counters=False,
                remote_tool_dispatcher=self.remote_tool_dispatcher,
            )
            return AgentRuntimeTerminalProjection(result=step_log, should_advance=True)

        if not context.steps:
            return None
        step_log = context.steps[-1].model_copy(deep=True)
        step_log.status = TaskStatus.FAIL
        step_log.result = context.decision.final_answer
        step_log.terminal_decision = context.decision.to_json()
        await self.facade.persist_step_limit_terminal(
            request=context.query,
            step_log=step_log,
            decision=context.decision,
            advance_counters=False,
            remote_tool_dispatcher=self.remote_tool_dispatcher,
        )
        return AgentRuntimeTerminalProjection(result=step_log, should_advance=False)


__all__ = ["DesktopTerminalPersistenceFacade", "DesktopTerminalProjectionPort"]
