"""Fallback provider request payload builders for trace capture."""

from __future__ import annotations

from typing import Protocol, cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.tool.tool import structured_tool_definition_to_openai
from nexau.core.messages import Message
from nexau.core.serializers.openai_responses import normalize_openai_responses_api_tools

from northcua.agentinfra.nexau_compat.adapters.providers import NexAUProviderAdapter
from northcua.agentinfra.providers.tool_image_policy import tool_image_policy_from_config
from northcua.agentkernel.json_types import JsonObject, ProviderToolSpecs

from .state import TraceCaptureState

_NEXAU_PROVIDER_ADAPTER = NexAUProviderAdapter()


class _LegacyOpenAIChatMessage(Protocol):
    def _as_legacy_openai_chat_dict(self) -> JsonObject:
        """Return the private-pin chat-shaped representation."""

        ...


def build_openai_responses_fallback_payload(
    *,
    llm_config: LLMConfig,
    messages: list[object],
    state: TraceCaptureState,
    tool_defs: ProviderToolSpecs,
) -> JsonObject:
    """Build the legacy OpenAI Responses-shaped trace payload as a safe fallback.

    Args:
        llm_config: LLM configuration for the captured model call.
        messages: Provider-prepared or legacy-compatible messages.
        state: Mutable trace state containing provider tool overrides.
        tool_defs: Eager tool definitions collected from the registry.

    Returns:
        JSON payload that preserves historical trace shape when the
        provider-specific builder cannot prepare a richer snapshot.
    """

    tool_image_policy = tool_image_policy_from_config(llm_config)
    if all(hasattr(message, "role") and hasattr(message, "content") for message in messages):
        chat_messages = _NEXAU_PROVIDER_ADAPTER.serialize_openai_chat(
            cast(list[Message], messages),
            tool_image_policy=tool_image_policy.value,
        )
    else:
        chat_messages = [cast(_LegacyOpenAIChatMessage, message)._as_legacy_openai_chat_dict() for message in messages]
    prepared_input = _NEXAU_PROVIDER_ADAPTER.prepare_openai_responses_input(chat_messages)
    input_items = prepared_input.items
    instructions = prepared_input.instructions
    request_payload: JsonObject = llm_config.to_openai_params()
    for key in (
        "provider_profile",
        "gateway_profile",
        "provider_gateway",
        "image_transport",
        "transcode_images_to_jpeg",
        "supports_images",
        "supports_tool_calls",
        "supports_structured_output",
        "preferred_for_multimodal",
        "supports_responses_api",
        "supports_reasoning_content_replay",
        "reasoning_replay_policy",
        "tool_image_policy",
    ):
        request_payload.pop(key, None)
    request_payload.pop("stream", None)
    request_payload.pop("store", None)
    max_tokens = request_payload.pop("max_tokens", None)
    if max_tokens is not None:
        request_payload.setdefault("max_output_tokens", max_tokens)
    request_payload.setdefault("input", input_items)
    if instructions:
        request_payload["instructions"] = instructions
    tools = state.provider_tools
    if not tools:
        tools = normalize_openai_responses_api_tools([structured_tool_definition_to_openai(defn) for defn in tool_defs])
    if tools:
        request_payload["tools"] = tools
    if state.provider_tool_choice is not None:
        request_payload["tool_choice"] = state.provider_tool_choice
    request_payload.setdefault("parallel_tool_calls", True)
    return request_payload


__all__ = ["build_openai_responses_fallback_payload"]
