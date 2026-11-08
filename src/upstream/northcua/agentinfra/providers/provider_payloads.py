"""Provider-aware payload builders shared by traces and probes.

The functions here mirror NexAU runtime message shaping so NorthCUA traces,
provider probes, and gateway-specific image rewrites stay aligned with the
actual request body sent to the configured provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.tool.tool import (
    StructuredToolDefinitionLike,
)
from nexau.core.messages import Message

from northcua.core.json_types import JsonObject

from .provider_payload_inputs import (
    adapt_structured_tools_for_api_type,
    count_openai_chat_images,
    default_tool_choice_for_api_type,
    prepare_messages_for_provider,
)
from .provider_profiles import ProviderFamily, resolve_provider_profile

# pyright: reportMissingImports=false


if TYPE_CHECKING:
    from northcua.agentinfra.providers.provider_payload_strategies import ProviderPayloadStrategyRegistry


@dataclass(slots=True, frozen=True)
class PreparedProviderPayload:
    """Resolved provider-facing wire payload and trace conversation snapshot.

    Attributes:
        provider_family: Coarse provider family for request serialization.
        request_payload: Provider-native request body.
        trace_messages: Sanitizable trace view of the conversation payload.
    """

    provider_family: ProviderFamily
    request_payload: JsonObject
    trace_messages: list[JsonObject] | JsonObject


@dataclass(slots=True, frozen=True)
class ProviderPayloadBuildRequest:
    """Inputs required to build a provider-native payload.

    Attributes:
        llm_config: NexAU LLM config plus NorthCUA provider extensions.
        messages: UMP messages or legacy chat dictionaries.
        tool_definitions: Neutral structured tool definitions.
        provider_tools: Already adapted provider-native tool schemas.
        tool_choice: Explicit provider-native tool choice.
        parallel_tool_calls: OpenAI Responses parallel-tool-call flag.
    """

    llm_config: LLMConfig
    messages: list[Message] | list[JsonObject]
    tool_definitions: list[StructuredToolDefinitionLike]
    provider_tools: list[JsonObject]
    tool_choice: JsonObject | str | None
    parallel_tool_calls: bool


def default_provider_payload_strategy_registry() -> ProviderPayloadStrategyRegistry:
    """Create the default payload strategy registry.

    Returns:
        Registry with built-in provider payload strategies.
    """

    from northcua.agentinfra.providers.provider_payload_strategies import (
        default_provider_payload_strategy_registry as _default,
    )

    return _default()


def build_provider_request_payload(
    *,
    llm_config: LLMConfig,
    messages: list[Message] | list[JsonObject],
    tool_definitions: list[StructuredToolDefinitionLike] | None = None,
    provider_tools: list[JsonObject] | None = None,
    tool_choice: JsonObject | str | None = None,
    parallel_tool_calls: bool = True,
) -> PreparedProviderPayload:
    """Build the provider wire payload that matches the configured api_type.

    Args:
        llm_config: NexAU LLM config with NorthCUA provider extensions.
        messages: UMP messages or legacy OpenAI-chat-style message dictionaries.
        tool_definitions: Neutral structured tool definitions.
        provider_tools: Already adapted provider tool schemas.
        tool_choice: Explicit provider-native tool choice.
        parallel_tool_calls: Responses API parallel tool-call flag.

    Returns:
        Prepared provider payload and trace conversation snapshot.

    Raises:
        ValueError: If the configured provider profile does not support a required payload feature.
        TypeError: If a legacy message cannot be coerced into UMP messages.
    """

    request = ProviderPayloadBuildRequest(
        llm_config=llm_config,
        messages=messages,
        tool_definitions=list(tool_definitions or []),
        provider_tools=list(provider_tools or []),
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
    )
    profile = resolve_provider_profile(request.llm_config)
    if request.llm_config.api_type == "openai_responses" and not profile.supports_responses_api:
        raise ValueError(
            f"Gateway profile {profile.gateway_name or 'current'} does not support OpenAI Responses for {request.llm_config.model}.",
        )
    return default_provider_payload_strategy_registry().strategy_for(profile.provider_family).build(request)


def prepare_runtime_messages_for_provider(
    *,
    llm_config: LLMConfig,
    messages: list[Message] | list[JsonObject],
) -> list[Message]:
    """Apply NorthCUA provider-profile message rewrites before NexAU calls the model.

    Trace payload shaping must not diverge from the real provider request. For
    Anthropic-compatible paths normalize inline image data URLs into JPEG
    base64 image blocks before the NexAU Anthropic serializer sees them.

    Args:
        llm_config: NexAU LLM config with NorthCUA provider extensions.
        messages: UMP messages or legacy OpenAI-chat-style message dictionaries.

    Returns:
        Provider-adjusted UMP messages.

    Raises:
        TypeError: If a legacy message cannot be coerced into UMP messages.
    """

    return prepare_messages_for_provider(llm_config=llm_config, messages=messages)


def count_provider_images(request_payload: JsonObject, *, provider_family: ProviderFamily) -> int:
    """Count image inputs in one provider-facing request payload.

    Args:
        request_payload: Provider-native request body.
        provider_family: Provider wire family for the payload.

    Returns:
        Number of image input blocks in the payload.
    """

    if provider_family == ProviderFamily.OPENAI_RESPONSES:
        input_items = request_payload.get("input")
        if not isinstance(input_items, list):
            return 0
        count = 0
        for item in input_items:
            if isinstance(item, dict):
                if item.get("type") == "input_image":
                    count += 1
                    continue
                if item.get("type") == "function_call_output":
                    output = item.get("output")
                    if isinstance(output, list):
                        count += sum(
                            1 for block in output if isinstance(block, dict) and block.get("type") == "input_image"
                        )
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    count += sum(
                        1 for block in content if isinstance(block, dict) and block.get("type") == "input_image"
                    )
        return count

    if provider_family == ProviderFamily.OPENAI_CHAT:
        return count_openai_chat_images(cast(list[JsonObject], request_payload.get("messages") or []))

    if provider_family == ProviderFamily.ANTHROPIC_MESSAGES:
        count = 0
        system_blocks = request_payload.get("system")
        if isinstance(system_blocks, list):
            count += sum(1 for block in system_blocks if isinstance(block, dict) and block.get("type") == "image")
        convo = request_payload.get("messages")
        if isinstance(convo, list):
            for item in convo:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    count += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image")
        return count

    return 0
