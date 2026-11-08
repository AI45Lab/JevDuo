"""Shared prompt-history replay services for AgentKit step loops."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from nexau.core.messages import (
    DiscriminatedBlock,
    ImageBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolResultContentBlock,
    ToolUseBlock,
)

from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.core.models import AssistantToolCallRecord, ToolExecutionRecord


class PromptHistoryMessageInspector:
    """Small inspector for provider-neutral prompt history messages."""

    def has_image(self, message: Message) -> bool:
        """Return whether a message contains at least one image block.

        Args:
            message: NexAU message to inspect.

        Returns:
            True when any content block is an image block.
        """

        return any(getattr(block, "type", None) == "image" for block in message.content)


@dataclass(slots=True)
class StepHistoryTrimmer:
    """Trim conversation history while preserving fixed preamble messages.

    Agent steps can be represented by observation, assistant tool-use, and one
    or more tool-result messages. The service intentionally uses a message-count
    budget derived from the step window so low-code agents get stable behavior
    without needing agent-specific trimming logic.

    Attributes:
        history_window_steps: Number of logical prior steps to keep.
        fixed_prefix_count: Leading messages that must never be trimmed.
        max_messages_per_step: Message budget used to approximate one step.
    """

    history_window_steps: int
    fixed_prefix_count: int = 1
    max_messages_per_step: int = 4

    def trim(self, messages: Sequence[Message]) -> list[Message]:
        """Return a trimmed copy of ``messages``.

        Args:
            messages: Existing prompt history in provider order.

        Returns:
            Trimmed message list preserving the fixed prefix and newest tail.
        """

        if not messages:
            return []
        fixed_count = min(max(self.fixed_prefix_count, 0), len(messages))
        fixed = list(messages[:fixed_count])
        remaining = list(messages[fixed_count:])
        if self.history_window_steps <= 0:
            return fixed
        max_tail_messages = max(1, self.history_window_steps) * max(1, self.max_messages_per_step)
        tail_start = max(0, len(remaining) - max_tail_messages)
        tail_start = self._tool_exchange_start(remaining, tail_start)
        return [*fixed, *remaining[tail_start:]]

    @staticmethod
    def _tool_exchange_start(messages: Sequence[Message], tail_start: int) -> int:
        """Expand a tail boundary so it never begins with an orphaned tool result."""

        if tail_start <= 0 or tail_start >= len(messages):
            return tail_start
        result_ids = {block.tool_use_id for block in messages[tail_start].content if isinstance(block, ToolResultBlock)}
        if not result_ids:
            return tail_start
        for index in range(tail_start - 1, -1, -1):
            use_ids = {block.id for block in messages[index].content if isinstance(block, ToolUseBlock)}
            if result_ids & use_ids:
                return index
        return tail_start


class ToolUseBlockFactory:
    """Convert captured provider tool calls into replayable tool-use blocks."""

    def build(self, tool_call: AssistantToolCallRecord) -> ToolUseBlock:
        """Build a structured assistant tool-use block.

        Args:
            tool_call: Captured assistant tool call from trace state.

        Returns:
            NexAU tool-use block with parsed JSON arguments when possible.
        """

        raw_arguments = tool_call.arguments
        parsed_arguments = self._json_object_from_string(raw_arguments)
        return ToolUseBlock(
            id=tool_call.id,
            name=tool_call.name,
            input=parsed_arguments,
            raw_input=raw_arguments if parsed_arguments == {} and raw_arguments else None,
        )

    def _json_object_from_string(self, value: str) -> dict[str, object]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return {str(key): item for key, item in parsed.items()} if isinstance(parsed, dict) else {}


class ToolResultContentProjector:
    """Project tool outputs into compact text/image content for prompt replay."""

    image_payload_keys = frozenset({"screenshot_url", "image_url", "data_url"})
    summary_text_keys = ("text", "answer", "error", "message")

    def build_content(self, execution: ToolExecutionRecord) -> str | list[ToolResultContentBlock]:
        """Build prompt-facing content for one tool result.

        Args:
            execution: Captured tool execution record.

        Returns:
            A string for text-only results, or a list of text/image blocks when
            the tool result includes a prompt-readable image.
        """

        value = execution.llm_tool_output if execution.llm_tool_output is not None else execution.tool_output
        if value is None:
            return ""

        image_url = self.image_url(value)
        if image_url:
            blocks: list[ToolResultContentBlock] = []
            summary = self._text_with_image(value)
            if summary:
                blocks.append(TextBlock(text=summary))
            blocks.append(ImageBlock(url=image_url))
            return blocks

        if self._should_preserve_full_json(value):
            return self.text(value)

        if isinstance(value, Mapping):
            summary = self.compact_summary_text(value)
            if summary:
                return summary
        return self.text(value)

    def image_url(self, value: object) -> str | None:
        """Return the first image data URL found in a tool result.

        Args:
            value: Raw tool result payload or nested result object.

        Returns:
            Image data URL, or ``None`` when no prompt-readable image exists.
        """

        if isinstance(value, str):
            return value if self._is_image_data_url(value) else None
        if not isinstance(value, Mapping):
            return None
        for key in self.image_payload_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and self._is_image_data_url(candidate):
                return candidate
        nested_result = value.get("result")
        if isinstance(nested_result, Mapping):
            return self.image_url(nested_result)
        return None

    def summary_text(self, value: object) -> str:
        """Return a compact human-readable summary without image payloads.

        Args:
            value: Raw tool result payload.

        Returns:
            Short text suitable to display before an image block.
        """

        if isinstance(value, str):
            return "" if self._is_image_data_url(value) else value
        if not isinstance(value, Mapping):
            return self.text(value)
        for key in self.summary_text_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        status = value.get("status")
        action = value.get("action")
        if isinstance(status, str) and isinstance(action, str):
            return f"{action} [{status}]"
        if isinstance(status, str):
            return f"Tool result [{status}]"
        if isinstance(action, str):
            return action
        return "Tool result includes an image."

    def compact_summary_text(self, value: Mapping[str, object]) -> str:
        """Return a compact text summary for non-image tool results, if explicit."""

        for key in self.summary_text_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                result = self._prompt_significant_result(value)
                if result is None:
                    return candidate
                return f"{candidate}\nresult: {self.text(result)}"
        status = value.get("status")
        action = value.get("action")
        if isinstance(status, str) and isinstance(action, str):
            return f"{action} [{status}]"
        if isinstance(status, str):
            return f"Tool result [{status}]"
        if isinstance(action, str):
            return action
        return ""

    def _text_with_image(self, value: object) -> str:
        if self._should_preserve_full_json(value):
            return self.text(value)
        return self.summary_text(value)

    def _should_preserve_full_json(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        if isinstance(value.get("observation"), Mapping):
            return True
        result = value.get("result")
        if not isinstance(result, Mapping):
            return False
        annotation = result.get("annotation")
        return isinstance(annotation, Mapping) and bool(annotation)

    def _prompt_significant_result(self, value: Mapping[str, object]) -> object | None:
        if "result" not in value:
            return None
        result = value.get("result")
        if result is None:
            return None
        if result == "" or result == [] or result == {}:
            return None
        return result

    def text(self, value: object) -> str:
        """Serialize a tool result while omitting large image data payloads.

        Args:
            value: Raw tool result payload.

        Returns:
            Text representation with large image payloads replaced by markers.
        """

        if isinstance(value, str):
            return value
        return json.dumps(self._strip_image_payloads(value), ensure_ascii=False)

    def _strip_image_payloads(self, value: object) -> object:
        if isinstance(value, Mapping):
            stripped: dict[str, object] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in self.image_payload_keys and isinstance(item, str) and self._is_image_data_url(item):
                    stripped[key_text] = "[image data omitted]"
                else:
                    stripped[key_text] = self._strip_image_payloads(item)
            return stripped
        if isinstance(value, list):
            return [self._strip_image_payloads(item) for item in value]
        if isinstance(value, str) and self._is_image_data_url(value):
            return "[image data omitted]"
        return value

    def _is_image_data_url(self, value: str) -> bool:
        return value.startswith("data:image/")


@dataclass(slots=True)
class PromptHistoryReplayer:
    """Build replayable assistant/tool history messages from trace state.

    Attributes:
        tool_use_factory: Converts captured assistant calls to tool-use blocks.
        tool_result_projector: Projects tool outputs into compact replay content.
    """

    tool_use_factory: ToolUseBlockFactory = field(default_factory=ToolUseBlockFactory)
    tool_result_projector: ToolResultContentProjector = field(default_factory=ToolResultContentProjector)

    def append_tool_step_history(
        self,
        *,
        messages: Sequence[Message],
        trace_state: TraceCaptureState,
        tool_executions: Sequence[ToolExecutionRecord] | None = None,
    ) -> list[Message]:
        """Append assistant tool-use and tool-result messages for one completed step.

        Args:
            messages: Existing history before the completed step.
            trace_state: Captured assistant text and provider tool calls.
            tool_executions: Optional normalized tool results overriding trace state.

        Returns:
            History with assistant and tool result messages appended.
        """

        history = list(messages)
        assistant_message = self.assistant_history_message(trace_state)
        if assistant_message is not None:
            history.append(assistant_message)
        history.extend(self.tool_result_history_messages(tool_executions or trace_state.tool_executions))
        return history

    def assistant_history_message(self, trace_state: TraceCaptureState) -> Message | None:
        """Build a structured assistant history message from captured model output.

        Args:
            trace_state: Capture state containing assistant text and tool calls.

        Returns:
            Assistant message, or ``None`` when there is no replayable content.
        """

        blocks: list[DiscriminatedBlock] = []
        reasoning = trace_state.reasoning.strip()
        encrypted_reasoning = trace_state.reasoning_encrypted_content.strip()
        if reasoning or encrypted_reasoning:
            blocks.append(
                ReasoningBlock(
                    text=reasoning,
                    redacted_data=encrypted_reasoning or None,
                )
            )
        assistant_text = trace_state.assistant_text.strip()
        if assistant_text:
            blocks.append(TextBlock(text=assistant_text))
        for tool_call in trace_state.assistant_tool_calls:
            blocks.append(self.tool_use_factory.build(tool_call))
        if not blocks:
            return None
        return Message(role=Role.ASSISTANT, content=blocks)

    def tool_result_history_messages(self, tool_executions: Sequence[ToolExecutionRecord]) -> list[Message]:
        """Build structured tool-result history messages from tool executions.

        Args:
            tool_executions: Tool execution records produced by dispatch.

        Returns:
            Tool-role messages that can be replayed before the next model step.
        """

        messages: list[Message] = []
        for execution in tool_executions:
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=[
                        ToolResultBlock(
                            tool_use_id=execution.tool_call_id,
                            content=self.tool_result_projector.build_content(execution),
                            is_error=execution.is_error,
                        )
                    ],
                )
            )
        return messages


_DEFAULT_INSPECTOR = PromptHistoryMessageInspector()
_DEFAULT_REPLAYER = PromptHistoryReplayer()


def message_has_image(message: Message) -> bool:
    """Return whether a message contains at least one image block.

    Args:
        message: NexAU message to inspect.

    Returns:
        True when any content block is an image block.
    """

    return _DEFAULT_INSPECTOR.has_image(message)


def trim_step_history(
    messages: Sequence[Message],
    *,
    history_window_steps: int,
    fixed_prefix_count: int = 1,
    max_messages_per_step: int = 4,
) -> list[Message]:
    """Trim conversation history while preserving fixed preamble messages.

    Agent steps can be represented by observation, assistant tool-use, and one
    or more tool-result messages. The helper intentionally uses a message-count
    budget derived from the step window so low-code agents get stable behavior
    without needing agent-specific trimming logic.

    Args:
        messages: Existing prompt history in provider order.
        history_window_steps: Number of logical prior steps to keep.
        fixed_prefix_count: Leading messages that must never be trimmed.
        max_messages_per_step: Message budget used to approximate one step.

    Returns:
        Trimmed message list preserving the fixed prefix and newest tail.
    """

    return StepHistoryTrimmer(
        history_window_steps=history_window_steps,
        fixed_prefix_count=fixed_prefix_count,
        max_messages_per_step=max_messages_per_step,
    ).trim(messages)


def append_tool_step_history(
    *,
    messages: Sequence[Message],
    trace_state: TraceCaptureState,
    tool_executions: Sequence[ToolExecutionRecord] | None = None,
) -> list[Message]:
    """Append assistant tool-use and tool-result messages for one completed step.

    Args:
        messages: Existing history before the completed step.
        trace_state: Captured assistant text and provider tool calls.
        tool_executions: Optional normalized tool results overriding trace state.

    Returns:
        History with assistant and tool result messages appended.
    """

    return _DEFAULT_REPLAYER.append_tool_step_history(
        messages=messages,
        trace_state=trace_state,
        tool_executions=tool_executions,
    )


def assistant_history_message(trace_state: TraceCaptureState) -> Message | None:
    """Build a structured assistant history message from captured model output.

    Args:
        trace_state: Capture state containing assistant text and tool calls.

    Returns:
        Assistant message, or ``None`` when there is no replayable content.
    """

    return _DEFAULT_REPLAYER.assistant_history_message(trace_state)


def tool_result_history_messages(tool_executions: Sequence[ToolExecutionRecord]) -> list[Message]:
    """Build structured tool-result history messages from tool executions.

    Args:
        tool_executions: Tool execution records produced by local or remote dispatch.

    Returns:
        Tool-role messages that can be replayed before the next model step.
    """

    return _DEFAULT_REPLAYER.tool_result_history_messages(tool_executions)
