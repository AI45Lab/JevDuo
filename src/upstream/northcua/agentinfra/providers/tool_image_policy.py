"""Tool-result image policy helpers for OpenAI-compatible providers."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Mapping, cast

from nexau.core.messages import Message

# pyright: reportMissingImports=false
logger = logging.getLogger(__name__)


class ToolImagePolicy(StrEnum):
    """How tool-result images should be serialized for chat-compatible payloads."""

    INJECT_USER_MESSAGE = "inject_user_message"
    EMBED_IN_TOOL_MESSAGE = "embed_in_tool_message"

    @classmethod
    def from_value(cls, value: object, *, default: "ToolImagePolicy") -> "ToolImagePolicy":
        """Return a normalized policy, falling back for unknown values.

        Args:
            value: Raw policy value from config or metadata.
            default: Policy to use for empty or unknown values.

        Returns:
            Parsed policy value or ``default``.
        """

        if isinstance(value, ToolImagePolicy):
            return value
        text = str(value or "").strip()
        if not text:
            return default
        try:
            return cls(text)
        except ValueError:
            return default


TOOL_IMAGE_POLICY_METADATA_KEY = "__northcua_tool_image_policy"


def default_tool_image_policy_for_api_type(api_type: str) -> ToolImagePolicy:
    """Return the historical default policy for one NexAU API type.

    Args:
        api_type: NexAU/OpenAI provider API type.

    Returns:
        Default policy that preserves historical provider behavior.
    """

    if api_type == "openai_responses":
        return ToolImagePolicy.EMBED_IN_TOOL_MESSAGE
    return ToolImagePolicy.INJECT_USER_MESSAGE


def tool_image_policy_from_config(llm_config: object) -> ToolImagePolicy:
    """Resolve tool-result image serialization policy from LLMConfig extras.

    Args:
        llm_config: NexAU-like LLM config object with ``api_type`` and extras.

    Returns:
        Configured policy, or the API-type default.
    """

    default = default_tool_image_policy_for_api_type(str(getattr(llm_config, "api_type", "")))
    extra = getattr(llm_config, "extra_params", None)
    if not isinstance(extra, Mapping):
        return default
    return ToolImagePolicy.from_value(extra.get("tool_image_policy"), default=default)


def apply_tool_image_policy_metadata(messages: list[Message], *, policy: ToolImagePolicy) -> list[Message]:
    """Tag messages so NexAU runtime serialization can honor NorthCUA's policy.

    NexAU currently decides the tool-image policy from api_type alone. NorthCUA
    tags runtime messages and installs a small serializer shim so a chat model can
    opt into Responses-compatible tool messages without changing NexAU's public
    call signature.

    Args:
        messages: UMP messages to tag before provider serialization.
        policy: Serialization policy to attach to message metadata.

    Returns:
        Original messages when no tag is needed, otherwise shallow-copied
        messages carrying policy metadata.
    """

    if policy == ToolImagePolicy.INJECT_USER_MESSAGE:
        return messages

    tagged: list[Message] = []
    for message in messages:
        metadata = dict(message.metadata)
        if metadata.get(TOOL_IMAGE_POLICY_METADATA_KEY) == policy.value:
            tagged.append(message)
            continue
        metadata[TOOL_IMAGE_POLICY_METADATA_KEY] = policy.value
        tagged.append(message.model_copy(update={"metadata": metadata}))
    return tagged


def tool_image_policy_from_message_metadata(messages: object) -> ToolImagePolicy | None:
    """Return a policy tagged onto UMP messages, if present.

    Args:
        messages: Raw UMP message list passed into NexAU serialization.

    Returns:
        Tagged policy, or ``None`` when no message carries one.
    """

    if not isinstance(messages, list):
        return None
    for message in messages:
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        value = metadata.get(TOOL_IMAGE_POLICY_METADATA_KEY)
        if value:
            return ToolImagePolicy.from_value(value, default=ToolImagePolicy.INJECT_USER_MESSAGE)
    return None


def install_nexau_tool_image_policy_patch() -> None:
    """Patch NexAU call sites that imported the OpenAI chat serializer by name."""

    try:
        import nexau.archs.main_sub.execution.llm_caller as llm_caller
    except Exception:
        logger.debug("NexAU llm_caller unavailable for tool image policy patch", exc_info=True)
        return

    _patch_serializer_holder(llm_caller)

    try:
        import nexau.archs.main_sub.token_trace_session as token_trace_session
    except Exception:
        logger.debug("NexAU token_trace_session unavailable for tool image policy patch", exc_info=True)
        return
    _patch_serializer_holder(token_trace_session)


def _patch_serializer_holder(holder: object) -> None:
    current = getattr(holder, "serialize_ump_to_openai_chat_payload", None)
    if current is None or getattr(current, "__northcua_tool_image_policy_patch__", False):
        return

    original = current

    def serialize_with_policy_metadata(
        messages: list[Message],
        *args: object,
        tool_image_policy: str = ToolImagePolicy.INJECT_USER_MESSAGE.value,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        """Serialize UMP messages while honoring NorthCUA policy metadata.

        Args:
            messages: UMP messages passed to the NexAU serializer.
            *args: Positional arguments forwarded to the original serializer.
            tool_image_policy: Default policy argument supplied by NexAU.
            **kwargs: Keyword arguments forwarded to the original serializer.

        Returns:
            OpenAI-chat-shaped payload dictionaries.
        """

        tagged_policy = tool_image_policy_from_message_metadata(messages)
        if tagged_policy is not None:
            tool_image_policy = tagged_policy.value
        return cast(
            list[dict[str, object]],
            original(messages, *args, tool_image_policy=tool_image_policy, **kwargs),
        )

    serialize_with_policy_metadata.__northcua_tool_image_policy_patch__ = True  # type: ignore[attr-defined]
    serialize_with_policy_metadata.__northcua_original_serializer__ = original  # type: ignore[attr-defined]
    setattr(holder, "serialize_ump_to_openai_chat_payload", serialize_with_policy_metadata)


__all__ = [
    "TOOL_IMAGE_POLICY_METADATA_KEY",
    "ToolImagePolicy",
    "apply_tool_image_policy_metadata",
    "default_tool_image_policy_for_api_type",
    "install_nexau_tool_image_policy_patch",
    "tool_image_policy_from_config",
    "tool_image_policy_from_message_metadata",
]
