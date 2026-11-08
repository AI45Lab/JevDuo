"""Stop-condition policy aligned with legacy ComputerUsingAgent semantics."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.core.enums import TaskStatus
from northcua.core.models import AssistantToolCallRecord, StepLogRecord, ToolResultArtifact
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason, TerminalDecisionPolicy
from northcua.telemetry.failure_categories import failure_category_from_message


@dataclass(slots=True, frozen=True)
class StopDecision:
    """Decision returned by the stop policy.

    Attributes:
        status: Task status selected for the current step.
        result: Terminal or continuation result text.
        summary: Short human-readable policy rationale.
        terminal_decision: Domain-neutral terminal decision for shared
            telemetry, reports, and low-code adapters.
    """

    status: TaskStatus
    result: str
    summary: str
    terminal_decision: AgentTerminalDecision

    @classmethod
    def from_terminal_decision(
        cls, decision: AgentTerminalDecision, *, status: TaskStatus | None = None
    ) -> "StopDecision":
        """Build a desktop stop decision from a shared terminal decision.

        Args:
            decision: Shared terminal decision produced by the desktop policy.
            status: Optional desktop-specific status override for legacy
                callers.

        Returns:
            Desktop stop decision preserving both legacy fields and the shared
            terminal contract.
        """

        return cls(
            status=status or _task_status_from_terminal_decision(decision),
            result=decision.final_answer,
            summary=decision.summary,
            terminal_decision=decision,
        )


class StopPolicyService:
    """Decide whether a round should continue, finish, or fail."""

    _terminal_failure_prefixes = (
        "i cannot ",
        "i can't ",
        "sorry, i cannot ",
        "sorry, i can't ",
        "sorry i cannot ",
        "sorry i can't ",
        "i am unable ",
        "i'm unable ",
        "sorry, i'm unable ",
        "sorry i'm unable ",
        "unable to ",
        "cannot complete ",
        "can't complete ",
        "it is not possible ",
        "it's not possible ",
        "failed to ",
        "i failed ",
        "error:",
        "blocked:",
        "the task is blocked",
        "environment lacks",
        "the environment lacks",
        "missing requirement",
        "the environment is missing",
        "the requested app is not installed",
        "the requested application is not installed",
        "not installed in this environment",
        "the requested file is missing",
        "the required file is missing",
        "the required permission is missing",
        "the required account is unavailable",
        "the network resource is unavailable",
        "i could not complete",
        "could not complete the task",
    )

    def __init__(self, *, terminal_policy: TerminalDecisionPolicy | None = None) -> None:
        self._terminal_policy = terminal_policy or TerminalDecisionPolicy()

    def decide(
        self,
        *,
        assistant_text: str,
        assistant_tool_calls: list[AssistantToolCallRecord],
        tool_results: list[ToolResultArtifact],
        step_logs: list[StepLogRecord],
    ) -> StopDecision:
        """Return the terminal/continuation decision for the current step.

        Args:
            assistant_text: Text emitted by the assistant for this step.
            assistant_tool_calls: Tool calls emitted by the assistant.
            tool_results: Tool results produced for current tool calls.
            step_logs: Previous step logs used to detect repeated actions.

        Returns:
            Stop decision preserving legacy round lifecycle semantics.
        """

        if assistant_tool_calls:
            if self._is_repeated_ineffective_action(
                current_calls=assistant_tool_calls,
                current_results=tool_results,
                step_logs=step_logs,
            ):
                return StopDecision.from_terminal_decision(
                    AgentTerminalDecision.fail(
                        reason=AgentTerminalReason.REPEATED_INEFFECTIVE_ACTION,
                        final_answer=assistant_text or "Repeated ineffective action pattern detected.",
                        summary="Repeated ineffective actions with no visual change; task should fail.",
                    )
                )
            if tool_results and all(result.is_error for result in tool_results):
                metadata = self._tool_error_metadata(tool_results)
                return StopDecision.from_terminal_decision(
                    AgentTerminalDecision.continue_(
                        final_answer=assistant_text or "All tool executions failed.",
                        summary=(
                            "All tool executions in the current step failed; "
                            "the model should inspect the tool error and retry with a corrected action."
                        ),
                        metadata=metadata,
                    )
                )
            return StopDecision.from_terminal_decision(
                AgentTerminalDecision.continue_(
                    final_answer=assistant_text,
                    summary="Further actions are required.",
                )
            )

        normalized = assistant_text.strip()
        if not normalized:
            return StopDecision.from_terminal_decision(
                AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.EMPTY_RESPONSE,
                    final_answer="No action and no textual result were produced.",
                    summary="The model produced neither tool calls nor a terminal text response.",
                    retryable=True,
                )
            )

        lowered = normalized.lower()
        if self._terminal_policy.has_infeasible_tag(normalized):
            summary_text = self._terminal_policy.strip_infeasible_tag(normalized) or normalized
            return StopDecision.from_terminal_decision(
                AgentTerminalDecision.infeasible(
                    final_answer=summary_text,
                    summary=summary_text,
                    metadata={"raw_assistant_text": normalized},
                )
            )
        if self._is_plain_failure_report(lowered):
            return StopDecision.from_terminal_decision(
                AgentTerminalDecision.fail(
                    reason=AgentTerminalReason.MODEL_REPORTED_FAILURE,
                    final_answer=normalized,
                    summary="The model reported that the task is blocked or failed.",
                )
            )

        return StopDecision.from_terminal_decision(
            AgentTerminalDecision.finish(
                final_answer=normalized,
                summary="The model returned a terminal text-only answer.",
            )
        )

    @classmethod
    def _is_plain_failure_report(cls, lowered_text: str) -> bool:
        """Return true when a text-only terminal answer is an explicit failure.

        The policy intentionally checks leading phrases instead of searching the
        whole answer. Normal successful answers often contain domain text such as
        "a license cannot be suspended" or "documents may be refused"; those
        should not turn a completed information-gathering task into a failure.
        """

        stripped = lowered_text.lstrip()
        return any(stripped.startswith(marker) for marker in cls._terminal_failure_prefixes)

    @staticmethod
    def _tool_error_metadata(tool_results: list[ToolResultArtifact]) -> dict[str, object]:
        """Build compact terminal metadata from failed tool results."""

        tool_names = sorted({result.name for result in tool_results if result.name})
        combined_error_text = "\n".join(result.content for result in tool_results if result.content)
        metadata: dict[str, object] = {
            "tool_error_count": len(tool_results),
            "tool_names": tool_names,
        }
        if combined_error_text:
            metadata["tool_error_excerpt"] = combined_error_text[:500]
        category = failure_category_from_message(combined_error_text)
        if category is not None:
            metadata["failure_category"] = category.value
        return metadata

    @staticmethod
    def _is_repeated_ineffective_action(
        *,
        current_calls: list[AssistantToolCallRecord],
        current_results: list[ToolResultArtifact],
        step_logs: list[StepLogRecord],
    ) -> bool:
        """Detect repeated tool calls that keep producing the same prompt-facing image."""

        if len(current_calls) != 1 or len(current_results) != 1:
            return False
        if len(step_logs) < 2:
            return False

        current_call = current_calls[0]
        current_result = current_results[0]
        current_image = current_result.image_path or current_result.raw_image_path
        if not current_image:
            return False

        recent_steps = step_logs[-2:]
        for step in recent_steps:
            if len(step.assistant_tool_calls) != 1 or len(step.tool_results) != 1:
                return False
            prev_call = step.assistant_tool_calls[0]
            prev_result = step.tool_results[0]
            prev_image = prev_result.image_path or prev_result.raw_image_path
            if prev_call.name != current_call.name or prev_call.arguments != current_call.arguments:
                return False
            if prev_image != current_image:
                return False
        return True


def _task_status_from_terminal_decision(decision: AgentTerminalDecision) -> TaskStatus:
    if decision.action.value == TaskStatus.CONTINUE.value:
        return TaskStatus.CONTINUE
    if decision.action.value == TaskStatus.FINISH.value:
        return TaskStatus.FINISH
    if decision.action.value == TaskStatus.ERROR.value:
        return TaskStatus.ERROR
    return TaskStatus.FAIL
