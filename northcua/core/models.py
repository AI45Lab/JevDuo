"""Typed runtime records and state containers for NorthCUA.

These Pydantic models and dataclasses normalize desktop observations, tool
results, LLM traces, and round/session state before they are written to logs or
passed across runtime boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from .accessibility import AccessibilityStepRecord
from .enums import (
    ObservationArtifactKind,
    ResultStatus,
    ScreenshotSelectionKind,
    TargetKind,
    TaskStatus,
)
from .json_types import JsonArray, JsonObject


class WindowState(BaseModel):
    """Normalized active-window metadata.

    Attributes:
        name: Display name or title for the active window.
        type: Platform-specific window/control type.
        rect: Optional screen-space rectangle.
    """

    name: str = ""
    type: str = ""
    rect: list[int] | None = None


class TargetInfo(BaseModel):
    """Normalized open-window/control record.

    Attributes:
        kind: Target category.
        name: Display name for the target.
        id: Optional platform target identifier.
        type: Optional platform target type.
        rect: Optional screen-space rectangle.
    """

    kind: TargetKind
    name: str
    id: str | None = None
    type: str | None = None
    rect: list[int] | None = None


class DesktopStateSnapshot(BaseModel):
    """Single round desktop observation state.

    Attributes:
        platform_name: Source platform name.
        current_window: Active-window metadata.
        current_controls: Raw visible control records.
        clean_screenshot_path: Filesystem path to the current screenshot.
        clean_screenshot_url: URL for the current screenshot when available.
        selected_history_image_path: Filesystem path to the history image selected for prompting.
        selected_history_image_url: URL for the selected history image when available.
        screenshot_metadata: Raw screenshot metadata.
    """

    platform_name: str = ""
    current_window: WindowState = Field(default_factory=WindowState)
    current_controls: list[JsonObject] = Field(default_factory=list)
    clean_screenshot_path: str = ""
    clean_screenshot_url: str = ""
    selected_history_image_path: str = ""
    selected_history_image_url: str = ""
    screenshot_metadata: JsonObject = Field(default_factory=dict)

    def release_image_payloads(self, *, drop_unbacked_inline: bool = False) -> None:
        """Drop inline image data when an equivalent artifact path exists."""

        if self.clean_screenshot_path or (drop_unbacked_inline and self.clean_screenshot_url.startswith("data:image/")):
            self.clean_screenshot_url = ""
        if self.selected_history_image_path or (
            drop_unbacked_inline and self.selected_history_image_url.startswith("data:image/")
        ):
            self.selected_history_image_url = ""


class ObservationArtifact(BaseModel):
    """Normalized current-round observation artifact.

    Attributes:
        kind: Artifact category.
        role: Prompt/runtime role for the artifact.
        source: Artifact source identifier.
        path: Filesystem artifact path.
        url: Artifact URL when available.
        text: Textual artifact content or summary.
        payload: Structured artifact payload.
        metadata: Additional artifact metadata.
    """

    kind: ObservationArtifactKind
    role: str
    source: str
    path: str = ""
    url: str = ""
    text: str = ""
    payload: JsonObject | JsonArray | None = None
    metadata: JsonObject = Field(default_factory=dict)

    def release_image_payloads(self, *, drop_unbacked_inline: bool = False) -> None:
        """Drop inline image data when a durable artifact path exists."""

        if self.kind == ObservationArtifactKind.IMAGE and (
            self.path or (drop_unbacked_inline and self.url.startswith("data:image/"))
        ):
            self.url = ""


class ObservationContext(BaseModel):
    """Normalized current-round observation context before prompt projection.

    Attributes:
        platform_name: Source platform name.
        current_window: Active-window metadata.
        current_controls: Raw visible control records.
        current_artifacts: Current-round observation artifacts.
        current_clean_screenshot_path: Filesystem path to the current screenshot.
        current_clean_screenshot_url: URL for the current screenshot when available.
        selected_history_image_path: Filesystem path to the history image selected for prompting.
        selected_history_image_url: URL for the selected history image when available.
        accessibility: Accessibility record associated with the step.
        observation_metadata: Additional observation metadata.
    """

    platform_name: str = ""
    current_window: WindowState = Field(default_factory=WindowState)
    current_controls: list[JsonObject] = Field(default_factory=list)
    current_artifacts: list[ObservationArtifact] = Field(default_factory=list)
    current_clean_screenshot_path: str = ""
    current_clean_screenshot_url: str = ""
    selected_history_image_path: str = ""
    selected_history_image_url: str = ""
    accessibility: AccessibilityStepRecord | None = None
    observation_metadata: JsonObject = Field(default_factory=dict)


def _json_int(value: object) -> int:
    """Return an integer from permissive JSON metadata, defaulting to zero."""

    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


class ScreenResolution(BaseModel):
    """Current screenshot geometry recorded for one step.

    Attributes:
        width: Screenshot width in pixels.
        height: Screenshot height in pixels.
        left: Left offset of the captured region.
        top: Top offset of the captured region.
        all_screens: Whether the capture spans all screens.
    """

    width: int = 0
    height: int = 0
    left: int = 0
    top: int = 0
    all_screens: bool = False

    @classmethod
    def from_metadata(cls, metadata: JsonObject | None) -> ScreenResolution:
        """Build a typed screen resolution record from screenshot metadata.

        Args:
            metadata: Raw screenshot metadata mapping.

        Returns:
            Screen resolution record with missing fields defaulted to zero/False.
        """

        data = metadata or {}
        return cls(
            width=_json_int(data.get("width")),
            height=_json_int(data.get("height")),
            left=_json_int(data.get("left")),
            top=_json_int(data.get("top")),
            all_screens=bool(data.get("all_screens", False)),
        )


class AssistantToolCallRecord(BaseModel):
    """Normalized assistant tool-call record.

    Attributes:
        id: Provider tool-call identifier.
        name: Tool/function name.
        arguments: Serialized tool arguments.
        type: Provider tool-call type.
    """

    id: str
    name: str
    arguments: str
    type: str = "function"


class CommandRequestRecord(BaseModel):
    """Typed command request payload for websocket relay.

    Attributes:
        tool_name: Runtime tool name to execute.
        server_name: Runtime server handling the tool.
        parameters: Tool parameters.
        tool_type: Tool category.
        registration_hook: Optional module:function hook used to register agent-owned MCP servers.
        call_id: Correlation identifier.
    """

    tool_name: str
    server_name: str = ""
    parameters: JsonObject = Field(default_factory=dict)
    tool_type: str = "action"
    registration_hook: str = ""
    call_id: str = ""


class CommandExecutionResultRecord(BaseModel):
    """Typed command result payload for websocket relay.

    Attributes:
        call_id: Correlation identifier.
        status: Command execution status.
        error: Optional error message.
        result: Optional command result payload.
    """

    call_id: str
    status: ResultStatus
    error: str | None = None
    result: JsonObject | str | None = None

    def release_image_payloads(self, *, drop_unbacked_inline: bool = False) -> None:
        """Drop bulky inline image data from retained command results."""

        self.result = _release_image_payloads_from_value(
            self.result,
            drop_unbacked_inline=drop_unbacked_inline,
        )


class ToolExecutionRecord(BaseModel):
    """Raw tool execution trace captured from NexAU hooks.

    Attributes:
        tool_call_id: Provider tool-call identifier.
        tool_name: Runtime tool name.
        tool_input: Raw tool input payload.
        tool_input_model: Tool input as emitted by the model.
        tool_input_pixels: Tool input with coordinate fields resolved to
            screenshot-relative pixels.
        tool_input_runtime_pixels: Tool input with coordinate fields resolved
            to runtime-absolute pixels when available.
        tool_output: Raw tool output payload.
        llm_tool_output: Provider-facing tool output payload.
        is_error: Whether the tool execution failed.
    """

    tool_call_id: str
    tool_name: str
    tool_input: JsonObject = Field(default_factory=dict)
    tool_input_model: JsonObject = Field(default_factory=dict)
    tool_input_pixels: JsonObject = Field(default_factory=dict)
    tool_input_runtime_pixels: JsonObject = Field(default_factory=dict)
    tool_output: JsonObject | str | None = None
    llm_tool_output: JsonObject | str | None = None
    is_error: bool = False

    @model_validator(mode="after")
    def _populate_coordinate_input_views(self) -> "ToolExecutionRecord":
        """Backfill model/pixel input views from raw input and tool metadata."""

        tool_output_payload = _tool_output_payload(self.tool_output)
        llm_tool_output_payload = _tool_output_payload(self.llm_tool_output)
        if not self.tool_input_model:
            self.tool_input_model = (
                _tool_input_payload(tool_output_payload, "tool_input_model")
                or _tool_input_payload(llm_tool_output_payload, "tool_input_model")
                or dict(self.tool_input)
            )
        if not self.tool_input_pixels:
            self.tool_input_pixels = _tool_input_payload(
                tool_output_payload,
                "tool_input_pixels",
            ) or _tool_input_payload(llm_tool_output_payload, "tool_input_pixels")
        if not self.tool_input_runtime_pixels:
            self.tool_input_runtime_pixels = (
                _tool_input_payload(tool_output_payload, "tool_input_runtime_pixels")
                or _tool_input_payload(llm_tool_output_payload, "tool_input_runtime_pixels")
                or {}
            )
        return self

    def release_image_payloads(self, *, drop_unbacked_inline: bool = False) -> None:
        """Drop bulky inline image data from retained tool execution payloads."""

        self.tool_output = _release_image_payloads_from_value(
            self.tool_output,
            drop_unbacked_inline=drop_unbacked_inline,
        )
        self.llm_tool_output = _release_image_payloads_from_value(
            self.llm_tool_output,
            drop_unbacked_inline=drop_unbacked_inline,
        )


def _tool_output_payload(output: JsonObject | str | None) -> JsonObject:
    if output is None:
        return {}
    value: object
    if isinstance(output, str):
        stripped = output.strip()
        if not stripped.startswith("{"):
            return {}
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
    else:
        value = output
    if isinstance(value, dict) and set(value.keys()) == {"result"}:
        nested = value.get("result")
        if isinstance(nested, (dict, str)):
            return _tool_output_payload(nested)
    if not isinstance(value, dict):
        return {}
    return {str(payload_key): payload_value for payload_key, payload_value in value.items()}


def _tool_input_payload(output: JsonObject, key: str) -> JsonObject:
    payload = output.get(key)
    if not isinstance(payload, dict):
        return {}
    return {str(payload_key): payload_value for payload_key, payload_value in payload.items()}


class ToolResultArtifact(BaseModel):
    """Serialized tool result with prompt-facing artifact fields.

    Attributes:
        tool_call_id: Provider tool-call identifier.
        name: Tool name.
        content: Prompt-facing result text.
        status: Tool execution status.
        is_error: Whether the result represents an error.
        raw_image_path: Path to the raw image artifact.
        raw_image_url: URL for the raw image artifact.
        overlay_image_path: Path to the overlay image artifact.
        overlay_image_url: URL for the overlay image artifact.
        image_path: Path to the selected image artifact.
        image_url: URL for the selected image artifact.
        selected_image_kind: Selected screenshot/image category.
        source_image_path: Path to the source image artifact.
        overlay_spec: Optional overlay drawing specification.
        action_function: Optional action function name.
        metadata: Additional structured tool-result metadata for runtime-only
            consumers such as verification and inspectors.
    """

    tool_call_id: str
    name: str
    content: str
    status: str
    is_error: bool = False
    raw_image_path: str = ""
    raw_image_url: str = ""
    overlay_image_path: str = ""
    overlay_image_url: str = ""
    image_path: str = ""
    image_url: str = ""
    selected_image_kind: ScreenshotSelectionKind = ScreenshotSelectionKind.NONE
    source_image_path: str = ""
    overlay_spec: JsonObject | None = None
    action_function: str | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Backward-compatible alias used by ad-hoc smoke scripts.

        Returns:
            Prompt-facing result content.
        """

        return self.content

    def release_image_payloads(self, *, drop_unbacked_inline: bool = False) -> None:
        """Drop prompt image data URLs while keeping artifact paths."""

        if self.raw_image_path or (drop_unbacked_inline and self.raw_image_url.startswith("data:image/")):
            self.raw_image_url = ""
        if self.overlay_image_path or (drop_unbacked_inline and self.overlay_image_url.startswith("data:image/")):
            self.overlay_image_url = ""
        if self.image_path or (drop_unbacked_inline and self.image_url.startswith("data:image/")):
            self.image_url = ""


class LLMTraceRecord(BaseModel):
    """Raw request/response source-of-truth for one model call.

    Attributes:
        request: Raw provider request payload.
        response: Raw provider response payload.
        usage: Provider token usage.
        stop_reason: Provider stop reason.
        cost: Estimated call cost.
        reasoning: Reasoning summary text when available.
        thinking: Thinking text when available.
        reasoning_encrypted_content: Encrypted reasoning payload when available.
        assistant_text: Assistant text extracted from the provider response.
        assistant_tool_calls: Assistant tool calls from the response.
        tool_executions: Tool executions associated with the call.
    """

    request: JsonObject = Field(default_factory=dict)
    response: JsonObject = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None
    cost: float = 0.0
    reasoning: str = ""
    thinking: str = ""
    reasoning_encrypted_content: str = ""
    assistant_text: str = ""
    assistant_tool_calls: list[AssistantToolCallRecord] = Field(default_factory=list)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list)

    def release_provider_payloads(self) -> None:
        """Drop bulky request/response payloads after the full trace is written."""

        self.request = {}
        self.response = {}

    def release_runtime_payloads(self) -> None:
        """Drop retained tool payloads that are already persisted in llm trace."""

        self.release_provider_payloads()
        self.tool_executions = []


class StepLogRecord(BaseModel):
    """Focused per-step response log record.

    Attributes:
        agent_type: Logical agent type.
        agent_name: Logical agent name.
        session_id: Session identifier.
        session_step: Step number across the session.
        round_step: Step number within the current round.
        round_num: Round number.
        status: Step/task status.
        request: User request for the round.
        result: Step result text.
        llm_cost: Estimated LLM cost for the step.
        platform_name: Source platform name.
        screen_resolution: Captured screen geometry.
        current_window: Active-window metadata.
        clean_screenshot_path: Path to the clean screenshot artifact.
        llm_trace_path: Path to the LLM trace artifact.
        llm_reasoning: Reasoning summary text when available.
        llm_thinking: Thinking text when available.
        llm_reasoning_encrypted_content: Encrypted reasoning state when available.
        llm_trace: Raw LLM trace excluded from serialized step logs.
        assistant_text: Assistant text response.
        assistant_tool_calls: Assistant tool calls.
        tool_results: Tool result artifacts.
        terminal_decision: Domain-neutral terminal decision payload when the
            agent emitted or derived one.
        accessibility: Accessibility record associated with the step.
        observation_artifacts: Observation artifacts for the step.
        observation_metadata: Additional observation metadata.
    """

    agent_type: str = "NorthCUA"
    agent_name: str = "NorthCUAAgent"
    session_id: str
    session_step: int
    round_step: int
    round_num: int
    status: TaskStatus
    request: str
    result: str = ""
    llm_cost: float = 0.0
    platform_name: str = ""
    screen_resolution: ScreenResolution = Field(default_factory=ScreenResolution)
    current_window: WindowState = Field(default_factory=WindowState)
    clean_screenshot_path: str = ""
    llm_trace_path: str = ""
    llm_reasoning: str = ""
    llm_thinking: str = ""
    llm_reasoning_encrypted_content: str = ""
    llm_trace: LLMTraceRecord = Field(default_factory=LLMTraceRecord, exclude=True)
    assistant_text: str = ""
    assistant_tool_calls: list[AssistantToolCallRecord] = Field(default_factory=list)
    tool_results: list[ToolResultArtifact] = Field(default_factory=list)
    terminal_decision: JsonObject = Field(default_factory=dict)
    accessibility: AccessibilityStepRecord | None = None
    observation_artifacts: list[ObservationArtifact] = Field(default_factory=list)
    observation_metadata: JsonObject = Field(default_factory=dict)
    _retained_payloads_compacted: bool = PrivateAttr(default=False)

    @property
    def retained_payloads_compacted(self) -> bool:
        """Whether non-replay payloads were dropped from this in-memory record."""

        return self._retained_payloads_compacted

    def release_retained_payloads(
        self,
        *,
        drop_unbacked_inline: bool = False,
        keep_replay_payloads: bool = True,
    ) -> None:
        """Drop inline payloads not needed for in-memory history replay."""

        self.llm_trace.release_runtime_payloads()
        for result in self.tool_results:
            result.release_image_payloads(drop_unbacked_inline=drop_unbacked_inline)
        for artifact in self.observation_artifacts:
            artifact.release_image_payloads(drop_unbacked_inline=drop_unbacked_inline)
        if not keep_replay_payloads:
            self.llm_reasoning = ""
            self.llm_thinking = ""
            self.llm_reasoning_encrypted_content = ""
            self._retained_payloads_compacted = True


class RoundLogRecord(BaseModel):
    """Round-level structural log record.

    Attributes:
        round_num: Round number.
        request: User request for the round.
        steps: Step logs in the round.
    """

    round_num: int
    request: str
    steps: list[StepLogRecord] = Field(default_factory=list)


class EvaluationLogRecord(BaseModel):
    """Session-level evaluation/summary record for terminal states.

    Attributes:
        session_id: Session identifier.
        round_num: Round number.
        session_step: Terminal session step number.
        status: Terminal task status.
        request: User request being evaluated.
        result: Terminal result text.
        summary: Optional evaluator summary.
        terminal_decision: Domain-neutral terminal decision payload when
            available.
    """

    session_id: str
    round_num: int
    session_step: int
    status: TaskStatus
    request: str
    result: str
    summary: str = ""
    terminal_decision: JsonObject = Field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RoundInput:
    """Immutable input to one round.

    Attributes:
        query: User query for the round.
        finish_after_round: Whether the session should finish after this round.
        metadata: Optional round metadata.
    """

    query: str
    finish_after_round: bool = False
    metadata: JsonObject | None = None


@dataclass(slots=True)
class RoundState:
    """Mutable round/session state for the NorthCUA loop.

    Attributes:
        session_id: Session identifier.
        session_step: Step number across the session.
        round_num: Current round number.
        round_step: Step number within the current round.
        latest_request: Latest user request text.
        latest_snapshot: Latest desktop state snapshot.
        pending_round_inputs: Queued follow-up round inputs.
        step_logs: Step records accumulated for the session.
        response_log_path: Optional response log path.
        evaluation_log_path: Optional evaluation log path.
        pending_command_results: Command results awaiting resume.
        pending_resume_results: Resume results awaiting processing.
    """

    session_id: str
    session_step: int = 1
    round_num: int = 0
    round_step: int = 1
    latest_request: str = ""
    latest_snapshot: DesktopStateSnapshot = field(default_factory=DesktopStateSnapshot)
    pending_round_inputs: list[RoundInput] = field(default_factory=list)
    step_logs: list[StepLogRecord] = field(default_factory=list)
    response_log_path: Path | None = None
    evaluation_log_path: Path | None = None
    pending_command_results: dict[str, CommandExecutionResultRecord] = field(default_factory=dict)
    pending_resume_results: list[CommandExecutionResultRecord] = field(default_factory=list)

    def enqueue(self, round_input: RoundInput) -> None:
        """Append a follow-up round input.

        Args:
            round_input: Round input to append to the pending queue.
        """

        self.pending_round_inputs.append(round_input)

    def pop_next(self) -> RoundInput | None:
        """Pop the next round input if available.

        Returns:
            Next pending round input, or None when the queue is empty.
        """

        if not self.pending_round_inputs:
            return None
        return self.pending_round_inputs.pop(0)

    def release_retained_payloads(
        self,
        *,
        retain_recent_steps: int | None = None,
        compact_replay_payloads: bool = False,
    ) -> None:
        """Drop bulky inline image payloads from retained session state."""

        self.latest_snapshot.release_image_payloads()
        compact_before = 0
        if retain_recent_steps is not None:
            compact_before = max(0, len(self.step_logs) - max(0, int(retain_recent_steps)))
        for index, step in enumerate(self.step_logs):
            keep_replay_payloads = not compact_replay_payloads or index >= compact_before
            step.release_retained_payloads(
                drop_unbacked_inline=not keep_replay_payloads,
                keep_replay_payloads=keep_replay_payloads,
            )
        for result in self.pending_command_results.values():
            result.release_image_payloads()
        for result in self.pending_resume_results:
            result.release_image_payloads()


def _release_image_payloads_from_value(value: object, *, drop_unbacked_inline: bool = False) -> object:
    """Return ``value`` with inline image data URLs removed when paths exist."""

    if isinstance(value, str):
        if drop_unbacked_inline and value.startswith("data:image/"):
            return ""
        return value
    if isinstance(value, list):
        return [_release_image_payloads_from_value(item, drop_unbacked_inline=drop_unbacked_inline) for item in value]
    if not isinstance(value, dict):
        return value
    compacted = {
        str(key): _release_image_payloads_from_value(item, drop_unbacked_inline=drop_unbacked_inline)
        for key, item in value.items()
    }
    for path_key, url_key in (
        ("screenshot_path", "screenshot_url"),
        ("image_path", "image_url"),
        ("raw_image_path", "raw_image_url"),
        ("overlay_image_path", "overlay_image_url"),
        ("data_url_artifact_id", "data_url"),
    ):
        if compacted.get(path_key) and isinstance(compacted.get(url_key), str):
            compacted[url_key] = ""
    if drop_unbacked_inline:
        for key, item in list(compacted.items()):
            if key.endswith("_data_url") or key == "data_url":
                if isinstance(item, str) and item.startswith("data:image/"):
                    compacted[key] = ""
    return compacted
