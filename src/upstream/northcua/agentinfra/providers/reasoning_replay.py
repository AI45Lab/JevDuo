"""Reasoning replay policy helpers for provider-specific prompt history."""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping, cast

from nexau.core.messages import Message, ReasoningBlock, Role

from northcua.core.json_types import JsonObject

# pyright: reportMissingImports=false


class ReasoningReplayPolicy(StrEnum):
    """How prior reasoning should be replayed for chat-compatible providers."""

    OFF = "off"
    SUMMARY_ONLY = "summary_only"
    CHAT_REASONING_CONTENT = "chat_reasoning_content"
    PROVIDER_NATIVE_DETAILS = "provider_native_details"

    @classmethod
    def from_value(cls, value: object) -> "ReasoningReplayPolicy":
        """Return a normalized policy, defaulting to ``OFF`` for unknown values.

        Args:
            value: Raw config value to normalize.

        Returns:
            Matching replay policy, or ``OFF`` when the value is empty/unknown.
        """

        if isinstance(value, ReasoningReplayPolicy):
            return value
        text = str(value or "").strip()
        if not text:
            return cls.OFF
        try:
            return cls(text)
        except ValueError:
            return cls.OFF


def reasoning_replay_policy_from_config(llm_config: object) -> ReasoningReplayPolicy:
    """Resolve the reasoning replay policy from ``LLMConfig.extra_params``.

    Args:
        llm_config: NexAU-like LLM config object with optional ``extra_params``.

    Returns:
        Configured replay policy, defaulting to ``OFF``.
    """

    extra = getattr(llm_config, "extra_params", None)
    if not isinstance(extra, Mapping):
        return ReasoningReplayPolicy.OFF
    return ReasoningReplayPolicy.from_value(extra.get("reasoning_replay_policy"))


class ReasoningReplayProjector:
    """Project Responses-style reasoning history into chat-native fields."""

    def project_messages(
        self,
        messages: list[Message],
        *,
        api_type: str,
        policy: ReasoningReplayPolicy,
    ) -> list[Message]:
        """Apply replay policy to UMP messages before the runtime LLM call.

        Args:
            messages: Provider-neutral UMP messages.
            api_type: NexAU provider API type.
            policy: Replay policy selected for the configured provider.

        Returns:
            Messages with chat-compatible reasoning blocks added when enabled.
        """

        if api_type != "openai_chat_completion":
            return messages
        if policy not in {ReasoningReplayPolicy.SUMMARY_ONLY, ReasoningReplayPolicy.CHAT_REASONING_CONTENT}:
            return messages

        projected: list[Message] = []
        for message in messages:
            if message.role != Role.ASSISTANT:
                projected.append(message)
                continue
            if any(isinstance(block, ReasoningBlock) for block in message.content):
                projected.append(message)
                continue
            reasoning_text = self.reasoning_text_from_metadata(message.metadata)
            if not reasoning_text:
                projected.append(message)
                continue
            metadata = dict(message.metadata)
            metadata.pop("reasoning", None)
            projected.append(
                message.model_copy(
                    update={
                        "content": [ReasoningBlock(text=reasoning_text), *message.content],
                        "metadata": metadata,
                    }
                )
            )
        return projected

    def sanitize_openai_chat_messages(
        self,
        messages: list[JsonObject],
        *,
        policy: ReasoningReplayPolicy,
    ) -> list[JsonObject]:
        """Remove Responses-only artifacts and optionally project chat reasoning.

        Args:
            messages: OpenAI-chat-shaped message dictionaries.
            policy: Replay policy selected for the configured provider.

        Returns:
            Sanitized message dictionaries matching what NexAU sends.
        """

        sanitized: list[JsonObject] = []
        for message in messages:
            item = dict(message)
            reasoning_items = item.pop("reasoning", None)
            if policy in {ReasoningReplayPolicy.SUMMARY_ONLY, ReasoningReplayPolicy.CHAT_REASONING_CONTENT}:
                reasoning_text = self.reasoning_text_from_items(reasoning_items)
                if reasoning_text and not item.get("reasoning_content"):
                    item["reasoning_content"] = reasoning_text
            sanitized.append(item)
        return sanitized

    def reasoning_text_from_metadata(self, metadata: Mapping[str, object]) -> str:
        """Extract replayable plain reasoning text from message metadata.

        Args:
            metadata: Message metadata that may include Responses reasoning items.

        Returns:
            Plain text summary/content suitable for chat reasoning replay.
        """

        return self.reasoning_text_from_items(metadata.get("reasoning"))

    def reasoning_text_from_items(self, value: object) -> str:
        """Extract plain reasoning text from Responses-style reasoning items.

        Args:
            value: Raw Responses reasoning array.

        Returns:
            Newline-joined reasoning text, or an empty string when absent.
        """

        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for raw_item in cast(list[object], value):
            if not isinstance(raw_item, Mapping):
                continue
            item = cast(Mapping[str, object], raw_item)
            parts.extend(self._text_parts(item.get("summary"), keys=("text",)))
            parts.extend(self._text_parts(item.get("content"), keys=("text", "content")))
            raw_text = item.get("text")
            if isinstance(raw_text, str) and raw_text.strip():
                parts.append(raw_text.strip())
        return "\n".join(part for part in parts if part)

    def _text_parts(self, value: object, *, keys: tuple[str, ...]) -> list[str]:
        if not isinstance(value, list):
            return []
        parts: list[str] = []
        for raw_part in cast(list[object], value):
            if not isinstance(raw_part, Mapping):
                continue
            part = cast(Mapping[str, object], raw_part)
            for key in keys:
                text = part.get(key)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                    break
        return parts


__all__ = [
    "ReasoningReplayPolicy",
    "ReasoningReplayProjector",
    "reasoning_replay_policy_from_config",
]
