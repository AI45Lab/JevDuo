"""Task interruption payload helpers."""

from __future__ import annotations

from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason
from northcua_protocol.payloads import TaskFinished
from northcua_protocol.state import TaskState


def build_interrupted_task(*, task_id: str, trace_id: str, session_id: str, error: str) -> TaskFinished:
    """Build a terminal interrupted task payload.

    Args:
        task_id: Task being interrupted.
        trace_id: Trace id used for controller event correlation.
        session_id: Session associated with the task.
        error: Human-readable interruption reason.

    Returns:
        Terminal interrupted task payload.
    """

    return TaskFinished(
        task_id=task_id,
        trace_id=trace_id,
        session_id=session_id,
        state=TaskState.INTERRUPTED,
        error=error,
        terminal_decision=AgentTerminalDecision.error(
            reason=AgentTerminalReason.INTERRUPTED,
            final_answer=error,
            summary="The controller interrupted the task.",
            retryable=False,
        ).to_json(),
    )


__all__ = ["build_interrupted_task"]
