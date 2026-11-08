"""Provider-agnostic NorthCUA prompt envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

from nexau.core.messages import (
    DiscriminatedBlock,
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolResultContentBlock,
    ToolUseBlock,
)

from northcua.agentkernel.context.domain.presentation import ContextPresentationNotice
from northcua.agentkernel.json_types import JsonObject, JsonValue, ProviderToolChoice, ProviderToolSpecs

type PromptMessageBlock = ImageBlock | TextBlock | ToolResultBlock | ToolUseBlock
type ToolResultContent = str | list[ToolResultContentBlock]


@dataclass(slots=True)
class HistoryTurn:
    """One completed CUA step captured for replay.

    Attributes:
        assistant_text: Assistant text emitted for the step.
        reasoning: Plain reasoning summary emitted for the step.
        reasoning_encrypted_content: Encrypted Responses reasoning state used for replay.
        assistant_tool_calls: Serialized assistant tool calls from the step.
        tool_results: Serialized tool results paired with the tool calls.
    """

    assistant_text: str = ""
    reasoning: str = ""
    reasoning_encrypted_content: str = ""
    assistant_tool_calls: ProviderToolSpecs = field(default_factory=list)
    tool_results: ProviderToolSpecs = field(default_factory=list)

    def to_messages(self) -> list[Message]:
        """Convert replay state into UMP messages.

        Returns:
            Assistant and tool messages representing this history turn.
        """

        messages: list[Message] = []
        assistant_blocks: list[PromptMessageBlock] = []
        if self.assistant_text:
            assistant_blocks.append(TextBlock(text=self.assistant_text))
        for tool_call in self.assistant_tool_calls:
            tool_name, tool_input, raw_input = self._extract_tool_use(tool_call)
            assistant_blocks.append(
                ToolUseBlock(
                    id=str(tool_call.get("id", "")),
                    name=tool_name,
                    input=tool_input,
                    raw_input=raw_input,
                )
            )
        if assistant_blocks:
            metadata: dict[str, object] = {}
            reasoning_items = self._reasoning_items()
            if reasoning_items:
                metadata["reasoning"] = reasoning_items
            messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=cast(list[DiscriminatedBlock], assistant_blocks),
                    metadata=metadata,
                )
            )

        for result in self.tool_results:
            content_blocks = self._tool_result_content(self._tool_result_content_with_omission_notice(result))
            raw_image_url = "" if self._is_error_result(result) else result.get("image_url", "")
            image_url = raw_image_url if isinstance(raw_image_url, str) else ""
            if image_url:
                content_blocks = [TextBlock(text=str(result.get("content", ""))), ImageBlock(url=image_url)]
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=cast(
                        list[DiscriminatedBlock],
                        [
                            ToolResultBlock(
                                tool_use_id=str(result.get("tool_call_id", "")),
                                content=content_blocks,
                                is_error=bool(result.get("is_error", False)),
                            )
                        ],
                    ),
                )
            )

        return messages

    @staticmethod
    def _tool_result_content(value: JsonValue | None) -> ToolResultContent:
        """Normalize serialized tool-result content for UMP replay.

        Args:
            value: Content captured in persisted tool-result JSON.

        Returns:
            A string payload accepted by ``ToolResultBlock``. Rich image
            content is rebuilt by ``to_messages`` when an ``image_url`` is
            present.
        """

        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False) if value is not None else ""

    @classmethod
    def _tool_result_content_with_omission_notice(cls, result: JsonObject) -> JsonValue:
        """Append a prompt-visible note when an available tool image was pruned."""

        content = result.get("content", "")
        if not cls._prompt_image_was_omitted(result):
            return content
        notice = ContextPresentationNotice.DESKTOP_HISTORY_IMAGE_OMITTED.value
        text = cls._tool_result_content(content)
        return f"{text}\n\n{notice}" if text else notice

    @staticmethod
    def _prompt_image_was_omitted(result: JsonObject) -> bool:
        if HistoryTurn._is_error_result(result):
            return False
        if result.get("image_url"):
            return False
        if result.get("image_path"):
            return True
        for path_key, url_key in (
            ("raw_image_path", "raw_image_url"),
            ("overlay_image_path", "overlay_image_url"),
        ):
            if result.get(path_key) or result.get(url_key):
                return True
        return False

    @staticmethod
    def _is_error_result(result: JsonObject) -> bool:
        return bool(result.get("is_error", False)) or result.get("status") == "error"

    def _reasoning_items(self) -> list[JsonObject]:
        """Return Responses-compatible reasoning items for history replay.

        OpenAI Responses can continue prior hidden reasoning only when the
        previous ``reasoning`` item, especially its encrypted payload, is
        supplied back as input. The plain summary is useful for trace/debug
        parity, while ``encrypted_content`` is what the provider can use.
        """

        reasoning = self.reasoning.strip()
        encrypted = self.reasoning_encrypted_content.strip()
        if not reasoning and not encrypted:
            return []

        item: JsonObject = {"type": "reasoning"}
        if reasoning:
            item["summary"] = [{"type": "summary_text", "text": reasoning}]
        if encrypted:
            item["encrypted_content"] = encrypted
            item.setdefault("summary", [])
        return [item]

    @staticmethod
    def _extract_tool_use(tool_call: JsonObject) -> tuple[str, JsonObject, str | None]:
        """Normalize OpenAI function calls and Anthropic tool_use calls.

        OpenAI-style traces store tool metadata under ``function`` with a JSON
        string ``arguments`` value. Anthropic traces store ``name`` and
        ``arguments`` at the top level. Replay must preserve both the parsed
        input object used by Anthropic Messages and the raw JSON string used by
        OpenAI Responses.
        """

        raw_function = tool_call.get("function", {})
        function = raw_function if isinstance(raw_function, dict) else {}
        tool_name = str(function.get("name") or tool_call.get("name") or "")
        raw_arguments = (
            function.get("arguments")
            if "arguments" in function
            else tool_call.get("input", tool_call.get("arguments", {}))
        )

        if isinstance(raw_arguments, dict):
            return tool_name, cast(JsonObject, raw_arguments), None

        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return tool_name, {}, raw_arguments
            return tool_name, cast(JsonObject, parsed) if isinstance(parsed, dict) else {}, raw_arguments

        return tool_name, {}, None


@dataclass(slots=True)
class PromptEnvelope:
    """Full provider-agnostic prompt payload.

    Attributes:
        system_prompt: Rendered system prompt text.
        persistent_user_text: Long-lived task prompt text.
        observation_user_text: Current observation prompt text.
        observation_images: Current observation image URLs.
        history_messages: Replay history already converted to messages.
        tools: Provider tool definitions attached to the request.
        tool_choice: Provider tool-choice setting attached to the request.
    """

    system_prompt: str
    persistent_user_text: str
    observation_user_text: str
    observation_images: list[str]
    history_messages: list[Message]
    tools: ProviderToolSpecs
    tool_choice: ProviderToolChoice

    def to_messages(self) -> list[Message]:
        """Serialize the envelope into UMP messages.

        Returns:
            Ordered UMP messages for model execution.
        """

        combined_user_text = self.persistent_user_text
        if self.observation_user_text:
            combined_user_text = "\n\n".join(
                part for part in [self.persistent_user_text, self.observation_user_text] if part
            )
        user_content: list[ImageBlock | TextBlock] = [TextBlock(text=combined_user_text)]
        for image_url in self.observation_images:
            user_content.append(ImageBlock(url=image_url))
        return [
            Message(
                role=Role.SYSTEM,
                content=cast(list[DiscriminatedBlock], [TextBlock(text=self.system_prompt)]),
            ),
            Message(role=Role.USER, content=cast(list[DiscriminatedBlock], user_content)),
            *self.history_messages,
        ]
