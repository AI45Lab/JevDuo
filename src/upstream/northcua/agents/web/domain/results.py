"""Typed result contracts for generic web-agent runs."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from northcua.core.json_types import JsonObject
from northcua.core.models import AssistantToolCallRecord
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason, TerminalDecisionPolicy

from .actions import WebAgentActionType
from .evaluation_artifacts import (
    WebObservationArtifact,
    WebTerminalVerificationRecord,
    WebToolResultArtifact,
)
from .observations import WebSnapshotObservation


class WebStepStatus(str, Enum):
    """Stable per-step status values."""

    OK = "ok"
    ERROR = "error"


class WebRunStatus(str, Enum):
    """Stable run status values."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    MAX_STEPS = "max_steps"


class WebTerminalStatus(str, Enum):
    """Stable terminal reason values."""

    RUNNING = "running"
    COMPLETED = "completed"
    NO_TOOL_CALL = "no_tool_call"
    TOOL_ERROR = "tool_error"
    MAX_STEPS = "max_steps"
    RUNTIME_ERROR = "runtime_error"
    INTERRUPTED = "interrupted"


class WebToolRecord(BaseModel):
    """One web tool execution captured inside a single model step.

    Attributes:
        action: Internal action type resolved from the tool call.
        tool_name: Provider-visible tool name emitted by the model.
        tool_input: Structured input passed to the tool.
        tool_output: Structured or textual result returned by the tool layer.
        is_error: Whether the tool layer marked the execution as failed.
        tool_call_id: Provider tool-call identifier, when available.
    """

    model_config = ConfigDict(extra="forbid")

    action: WebAgentActionType
    tool_name: str
    tool_input: JsonObject = Field(default_factory=dict)
    tool_output: JsonObject | str | None = None
    is_error: bool = False
    tool_call_id: str = ""


class WebStepRecord(BaseModel):
    """Persisted record for one web-agent step.

    Attributes:
        step_index: One-based step index inside the current round.
        session_id: Controller/session identity shared by every step in the run.
        round_num: One-based user query/round number.
        session_step: One-based step index across the whole session.
        runtime_cursor: Shared runtime cursor payload for trace/audit readers.
        status: Step status after executing all resolved tools.
        assistant_text: User-visible assistant text captured from the model.
        reasoning: Provider-supplied reasoning summary when available.
        assistant_tool_calls: Raw assistant tool-call records.
        tool_summary: Human-readable summary derived from this step's tools.
        tools: All web tool executions resolved for this step.
        url: Active page URL after the step observation.
        observation: Snapshot observation persisted for replay/debugging.
        error_text: Error summary when the step failed.
        screenshot_path: Persisted screenshot artifact path.
        dom_snapshot_path: Persisted text snapshot path.
        dom_snapshot_json_path: Persisted JSON snapshot path.
        observation_artifact: Typed current-observation artifact for this step.
        tool_artifacts: Tool-result visual evidence produced by actions.
        terminal_verification: Optional terminal verification override record.
        observation_metadata: Additional current-observation metadata.
    """

    model_config = ConfigDict(extra="forbid")

    step_index: int
    session_id: str = Field(min_length=1)
    round_num: int = 1
    session_step: int = 0
    runtime_cursor: JsonObject = Field(default_factory=dict)
    status: WebStepStatus
    assistant_text: str = ""
    reasoning: str = ""
    assistant_tool_calls: list[AssistantToolCallRecord] = Field(default_factory=list)
    tool_summary: str = ""
    tools: list[WebToolRecord] = Field(default_factory=list)
    url: str = ""
    observation: WebSnapshotObservation | None = None
    error_text: str = ""
    screenshot_path: str = ""
    dom_snapshot_path: str = ""
    dom_snapshot_json_path: str = ""
    observation_artifact: WebObservationArtifact | None = None
    tool_artifacts: list[WebToolResultArtifact] = Field(default_factory=list)
    terminal_verification: WebTerminalVerificationRecord | None = None
    observation_metadata: JsonObject = Field(default_factory=dict)


class WebAgentResult(BaseModel):
    """Full structured result for a web-agent run.

    Attributes:
        status: User-facing terminal status.
        terminal_status: Machine-readable terminal reason.
        session_id: Session/artifact identifier.
        user_query: Original task text.
        start_url: Optional URL opened before the first step.
        final_answer: Final assistant answer or error summary.
        success: Whether the model reported successful completion.
        steps: Persisted step records.
        trace_path: Optional path to the step trace JSONL.
        result_path: Optional path to this final result JSON.
        screenshots: Persisted screenshot paths.
        tool_errors: Tool-layer error summaries collected during the run.
        cleanup_errors: Resource cleanup failures collected after the run body.
        terminal_metadata: Optional shared terminal decision metadata.
        terminal_decision: Shared terminal projection written for batch,
            telemetry, and controller readers.
    """

    model_config = ConfigDict(extra="forbid")

    status: WebRunStatus
    terminal_status: WebTerminalStatus
    session_id: str
    user_query: str
    round_num: int = 1
    evaluation_report: JsonObject = Field(default_factory=dict)
    evaluation_report_path: str = ""
    evaluation_error: str = ""
    start_url: str = ""
    final_answer: str = ""
    success: bool = False
    steps: list[WebStepRecord] = Field(default_factory=list)
    trace_path: Path | None = None
    result_path: Path | None = None
    screenshots: list[str] = Field(default_factory=list)
    tool_errors: list[str] = Field(default_factory=list)
    cleanup_errors: list[str] = Field(default_factory=list)
    terminal_metadata: JsonObject = Field(default_factory=dict)
    terminal_decision: JsonObject = Field(default_factory=dict)

    @property
    def hit_step_limit(self) -> bool:
        """Return whether the run ended because the step budget was exhausted.

        Returns:
            ``True`` when the terminal reason is ``MAX_STEPS``.
        """

        return self.terminal_status == WebTerminalStatus.MAX_STEPS

    def to_terminal_decision(self) -> AgentTerminalDecision:
        """Normalize the web-specific result into the shared terminal contract.

        Returns:
            Domain-neutral terminal decision for shared renderers, telemetry,
            batch summaries, and low-code runtime templates.
        """

        policy = TerminalDecisionPolicy()
        explanation = self.final_answer or "; ".join(self.tool_errors)

        if self.terminal_status == WebTerminalStatus.RUNNING:
            return AgentTerminalDecision.continue_(
                final_answer=explanation,
                summary="The web task is still running.",
            )
        if self.terminal_status == WebTerminalStatus.COMPLETED:
            if self.success:
                return AgentTerminalDecision.finish(
                    final_answer=self.final_answer,
                    summary="The web task completed.",
                )
            return policy.failure_decision_from_text(
                explanation,
                summary="The web task reported failure.",
            )
        if self.terminal_status == WebTerminalStatus.NO_TOOL_CALL:
            return AgentTerminalDecision.fail(
                reason=AgentTerminalReason.NO_TOOL_CALL,
                final_answer=explanation,
                summary="The model did not call a web tool.",
                retryable=True,
            )
        if self.terminal_status == WebTerminalStatus.TOOL_ERROR:
            return AgentTerminalDecision.fail(
                reason=AgentTerminalReason.TOOL_ERROR,
                final_answer=explanation,
                summary="A web tool execution failed.",
                retryable=True,
            )
        if self.terminal_status == WebTerminalStatus.MAX_STEPS:
            return AgentTerminalDecision.fail(
                reason=AgentTerminalReason.MAX_STEPS,
                final_answer=self.final_answer,
                summary="The web task reached the max step limit.",
                metadata=self.terminal_metadata,
            )
        if self.terminal_status == WebTerminalStatus.INTERRUPTED:
            return AgentTerminalDecision.error(
                reason=AgentTerminalReason.INTERRUPTED,
                final_answer=explanation,
                summary="The web task was interrupted.",
                retryable=True,
            )
        return AgentTerminalDecision.error(
            reason=AgentTerminalReason.RUNTIME_ERROR,
            final_answer=explanation,
            summary="The web task encountered a runtime error.",
            retryable=True,
        )
