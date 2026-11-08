"""Domain-neutral terminal decisions for AgentKit agents.

The contracts in this module intentionally avoid imports from concrete agent
domains. Built-in and future low-code agents adapt their own native
status/result models into these values at their package boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from northcua.core.json_types import JsonObject


class AgentTerminalAction(str, Enum):
    """High-level action selected by an agent step or run."""

    CONTINUE = "continue"
    FINISH = "finish"
    FAIL = "fail"
    ERROR = "error"


class AgentTerminalReason(str, Enum):
    """Stable reason explaining a terminal or continuation decision."""

    COMPLETED = "completed"
    NEEDS_MORE_STEPS = "needs_more_steps"
    MAX_STEPS = "max_steps"
    TOOL_ERROR = "tool_error"
    NO_TOOL_CALL = "no_tool_call"
    RUNTIME_ERROR = "runtime_error"
    INTERRUPTED = "interrupted"
    REPEATED_INEFFECTIVE_ACTION = "repeated_ineffective_action"
    EMPTY_RESPONSE = "empty_response"
    MODEL_REPORTED_FAILURE = "model_reported_failure"
    INFEASIBLE = "infeasible"


class AgentRunStatus(str, Enum):
    """Domain-neutral run status derived from a terminal decision.

    ``AgentTerminalAction`` describes what the agent decided to do. This enum
    is the compact run-level state stored by reusable templates and CLI panels.
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"
    ERROR = "error"
    MAX_STEPS = "max_steps"

    @classmethod
    def from_terminal_decision(cls, decision: "AgentTerminalDecision") -> "AgentRunStatus":
        """Derive a reusable run status from a shared terminal decision.

        Args:
            decision: Domain-neutral terminal decision emitted by an agent or
                adapter.

        Returns:
            Compact run status suitable for reusable templates, summaries, and
            CLI panels.
        """

        if decision.action == AgentTerminalAction.CONTINUE:
            return cls.RUNNING
        if decision.action == AgentTerminalAction.FINISH:
            return cls.SUCCESS
        if decision.action == AgentTerminalAction.ERROR:
            return cls.ERROR
        if decision.reason == AgentTerminalReason.MAX_STEPS:
            return cls.MAX_STEPS
        return cls.FAIL


@dataclass(frozen=True, slots=True)
class AgentTerminalDecision:
    """Normalized decision shared across agent implementations.

    Attributes:
        action: Coarse lifecycle action selected by the agent.
        reason: Stable machine-readable reason for the action.
        success: Whether the user task is considered successfully completed.
        retryable: Whether a higher-level orchestrator may reasonably retry the
            whole case after this decision.
        final_answer: User-visible final answer or failure explanation.
        summary: Short operational summary for panels and telemetry.
        metadata: Optional JSON-compatible domain metadata.
    """

    action: AgentTerminalAction
    reason: AgentTerminalReason
    success: bool = False
    retryable: bool = False
    final_answer: str = ""
    summary: str = ""
    metadata: JsonObject = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Return whether this decision stops the current run.

        Returns:
            ``True`` for finish/fail/error decisions and ``False`` for
            continuation decisions.
        """

        return self.action != AgentTerminalAction.CONTINUE

    def to_json(self) -> JsonObject:
        """Return a JSON-compatible representation for traces and telemetry.

        Returns:
            Stable JSON object with enum values serialized as lowercase strings.
        """

        return {
            "action": self.action.value,
            "reason": self.reason.value,
            "success": self.success,
            "retryable": self.retryable,
            "final_answer": self.final_answer,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def continue_(
        cls,
        *,
        reason: AgentTerminalReason = AgentTerminalReason.NEEDS_MORE_STEPS,
        summary: str = "Further action is required.",
        final_answer: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> "AgentTerminalDecision":
        """Build a continuation decision.

        Args:
            reason: Machine-readable continuation reason.
            summary: Short operational summary for panels and telemetry.
            final_answer: Optional assistant text associated with the step.
            metadata: Optional JSON-compatible domain metadata.

        Returns:
            Shared decision whose action is ``continue``.
        """

        return cls(
            action=AgentTerminalAction.CONTINUE,
            reason=reason,
            success=False,
            retryable=False,
            final_answer=final_answer,
            summary=summary,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def finish(
        cls,
        *,
        reason: AgentTerminalReason = AgentTerminalReason.COMPLETED,
        final_answer: str = "",
        summary: str = "The task completed successfully.",
        metadata: Mapping[str, object] | None = None,
    ) -> "AgentTerminalDecision":
        """Build a successful terminal decision.

        Args:
            reason: Machine-readable completion reason.
            final_answer: User-visible final answer.
            summary: Short operational summary for panels and telemetry.
            metadata: Optional JSON-compatible domain metadata.

        Returns:
            Shared decision whose action is ``finish``.
        """

        return cls(
            action=AgentTerminalAction.FINISH,
            reason=reason,
            success=True,
            retryable=False,
            final_answer=final_answer,
            summary=summary,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def fail(
        cls,
        *,
        reason: AgentTerminalReason,
        final_answer: str = "",
        summary: str = "The task failed.",
        retryable: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> "AgentTerminalDecision":
        """Build a failed terminal decision.

        Args:
            reason: Stable failure reason.
            final_answer: User-visible failure explanation.
            summary: Short operational summary for panels and telemetry.
            retryable: Whether an outer runner may retry the whole case.
            metadata: Optional JSON-compatible domain metadata.

        Returns:
            Shared decision whose action is ``fail``.
        """

        return cls(
            action=AgentTerminalAction.FAIL,
            reason=reason,
            success=False,
            retryable=retryable,
            final_answer=final_answer,
            summary=summary,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def error(
        cls,
        *,
        reason: AgentTerminalReason = AgentTerminalReason.RUNTIME_ERROR,
        final_answer: str = "",
        summary: str = "The agent encountered a runtime error.",
        retryable: bool = True,
        metadata: Mapping[str, object] | None = None,
    ) -> "AgentTerminalDecision":
        """Build a runtime-error terminal decision.

        Args:
            reason: Stable runtime error reason.
            final_answer: User-visible error explanation.
            summary: Short operational summary for panels and telemetry.
            retryable: Whether an outer runner may retry the whole case.
            metadata: Optional JSON-compatible domain metadata.

        Returns:
            Shared decision whose action is ``error``.
        """

        return cls(
            action=AgentTerminalAction.ERROR,
            reason=reason,
            success=False,
            retryable=retryable,
            final_answer=final_answer,
            summary=summary,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def infeasible(
        cls,
        *,
        final_answer: str,
        summary: str = "The environment does not satisfy the task requirements.",
        retryable: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> "AgentTerminalDecision":
        """Build a failed decision caused by an unsuitable environment.

        Args:
            final_answer: User-visible explanation of the missing environment
                requirement.
            summary: Short operational summary for panels and telemetry.
            retryable: Whether the case may succeed after reprovisioning.
            metadata: Optional JSON-compatible domain metadata.

        Returns:
            Shared failure decision with reason ``infeasible``.
        """

        return cls.fail(
            reason=AgentTerminalReason.INFEASIBLE,
            final_answer=final_answer,
            summary=summary,
            retryable=retryable,
            metadata=metadata,
        )


class TerminalDecisionAdapter(Protocol):
    """Adapter boundary from a domain result into a terminal decision."""

    def to_terminal_decision(self) -> AgentTerminalDecision:
        """Map a domain-specific object into a domain-neutral decision.

        Returns:
            Shared terminal decision for renderer, trace, telemetry, and batch
            consumers.
        """
        ...


class TerminalDecisionPolicy:
    """Reusable helpers for consistent terminal reason classification."""

    INFEASIBLE_TAG = "[INFEASIBLE]"
    _INFEASIBLE_MARKERS = (
        "access is blocked",
        "access denied",
        "permission denied",
        "insufficient permission",
        "missing permission",
        "not installed",
        "application is missing",
        "app is missing",
        "display is unavailable",
        "no display",
        "unavailable in this environment",
        "environment does not",
        "environment cannot",
        "does not exist in this environment",
        "not found in this environment",
        "unsupported environment",
    )

    def failure_reason_from_text(self, text: str) -> AgentTerminalReason:
        """Classify model/user-visible failure text into a stable reason.

        Args:
            text: User-visible failure text or model explanation.

        Returns:
            ``infeasible`` for environment gaps, otherwise
            ``model_reported_failure``.
        """

        return (
            AgentTerminalReason.INFEASIBLE
            if self.has_infeasible_tag(text) or self.is_infeasible_text(text)
            else AgentTerminalReason.MODEL_REPORTED_FAILURE
        )

    def failure_decision_from_text(
        self,
        text: str,
        *,
        summary: str = "The model reported that the task is blocked or failed.",
        retryable: bool = False,
    ) -> AgentTerminalDecision:
        """Build a failed decision by classifying a text explanation.

        Args:
            text: User-visible failure text or model explanation.
            summary: Short operational summary attached to the decision.
            retryable: Whether an outer runner may retry the whole case.

        Returns:
            Shared failed decision with reason derived from ``text``.
        """

        reason = self.failure_reason_from_text(text)
        if reason == AgentTerminalReason.INFEASIBLE:
            return AgentTerminalDecision.infeasible(final_answer=text, summary=summary, retryable=retryable)
        return AgentTerminalDecision.fail(
            reason=reason,
            final_answer=text,
            summary=summary,
            retryable=retryable,
        )

    def is_infeasible_text(self, text: str) -> bool:
        """Return whether text describes an environment-level impossibility.

        Args:
            text: Failure text to classify.

        Returns:
            ``True`` when the text matches a known environment gap marker.
        """

        normalized = text.lower()
        return any(marker in normalized for marker in self._INFEASIBLE_MARKERS)

    def has_infeasible_tag(self, text: str) -> bool:
        """Return whether a model explicitly tagged the task as infeasible.

        Args:
            text: Assistant text to inspect.

        Returns:
            ``True`` when ``[INFEASIBLE]`` appears in the text.
        """

        return self.INFEASIBLE_TAG in text.upper()

    def strip_infeasible_tag(self, text: str) -> str:
        """Remove the explicit infeasible tag from user-facing summaries.

        Args:
            text: Assistant text that may include the tag.

        Returns:
            Text with the first infeasible tag removed and surrounding
            whitespace trimmed.
        """

        tag_index = text.upper().find(self.INFEASIBLE_TAG)
        if tag_index < 0:
            return text.strip()
        return (text[:tag_index] + text[tag_index + len(self.INFEASIBLE_TAG) :]).strip()


__all__ = [
    "AgentRunStatus",
    "AgentTerminalAction",
    "AgentTerminalDecision",
    "AgentTerminalReason",
    "TerminalDecisionAdapter",
    "TerminalDecisionPolicy",
]
