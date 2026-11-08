"""Shared provider payload normalization used by payload strategies and facades."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence, cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.tool_call_modes import resolve_structured_provider_target
from nexau.archs.tool.tool import (
    StructuredToolDefinitionLike,
    normalize_structured_tool_definition,
    structured_tool_definition_to_openai,
)
from nexau.core.adapters.legacy import messages_from_legacy_openai_chat
from nexau.core.messages import ImageBlock, Message
from nexau.core.serializers.openai_responses import normalize_openai_responses_api_tools

from northcua.agentinfra.nexau_compat.adapters.providers import NexAUProviderAdapter
from northcua.agentinfra.providers.provider_images import ProviderImageInspector, ProviderImageTranscoder
from northcua.agentinfra.providers.provider_profiles import ProviderCapabilityProfile, resolve_provider_profile
from northcua.agentinfra.providers.reasoning_replay import (
    ReasoningReplayProjector,
    reasoning_replay_policy_from_config,
)
from northcua.agentinfra.providers.tool_image_policy import (
    apply_tool_image_policy_metadata,
    install_nexau_tool_image_policy_patch,
    tool_image_policy_from_config,
)
from northcua.core.json_types import JsonObject
from northcua_runtime_shared.tool_contracts import apply_openai_union_tool_policy

# pyright: reportMissingImports=false


if TYPE_CHECKING:
    from northcua.agentinfra.providers.provider_payloads import ProviderPayloadBuildRequest

logger = logging.getLogger(__name__)

_DEFAULT_REASONING_REPLAY_PROJECTOR = ReasoningReplayProjector()
_DEFAULT_NEXAU_PROVIDER_ADAPTER = NexAUProviderAdapter()


@dataclass(slots=True, frozen=True)
class ProviderPayloadInputs:
    """Provider-neutral inputs prepared once before strategy-specific serialization.

    Attributes:
        profile: Resolved provider capability profile.
        ump_messages: UMP messages after provider-specific image rewrites.
        openai_chat_messages: OpenAI-chat-shaped trace projection.
        tools: Provider-native tool schemas.
        resolved_tool_choice: Provider-native tool-choice value.
        api_params: Provider API params with NorthCUA-only flags removed.
    """

    profile: ProviderCapabilityProfile
    ump_messages: list[Message]
    openai_chat_messages: list[JsonObject]
    tools: list[JsonObject]
    resolved_tool_choice: JsonObject | str | None
    api_params: JsonObject


class StructuredToolSchemaAdapter:
    """Adapt neutral structured tool definitions into provider-native schemas."""

    def adapt(
        self,
        *,
        tool_definitions: list[StructuredToolDefinitionLike],
        api_type: str,
    ) -> list[JsonObject]:
        """Convert neutral structured tool definitions into provider-facing schemas.

        Args:
            tool_definitions: Tool definitions accepted by NexAU's structured
                tool normalizer.
            api_type: Provider API type used to select OpenAI, Anthropic, or
                generic schema projection.

        Returns:
            Provider-native tool schema dictionaries.
        """

        if not tool_definitions:
            return []

        provider_target = resolve_structured_provider_target(api_type)
        adapted: list[JsonObject] = []
        for definition in tool_definitions:
            normalized = normalize_structured_tool_definition(definition)
            if provider_target == "openai":
                adapted.append(cast(JsonObject, structured_tool_definition_to_openai(normalized)))
                continue
            if provider_target == "anthropic":
                adapted.append(_DEFAULT_NEXAU_PROVIDER_ADAPTER.serialize_anthropic_tool(normalized))
                continue
            adapted.append(dict(normalized))

        if provider_target == "openai":
            adapted = apply_openai_union_tool_policy(adapted)

        if api_type == "openai_responses":
            return cast(list[JsonObject], normalize_openai_responses_api_tools(adapted))
        return adapted

    def default_tool_choice(self, api_type: str, *, tools_present: bool) -> JsonObject | str | None:
        """Return the provider-native default tool_choice shape for one api_type.

        Args:
            api_type: Provider API type from ``LLMConfig``.
            tools_present: Whether any tool schemas will be sent.

        Returns:
            Provider-native automatic tool-choice value, or ``None`` when the
            request should omit the field.
        """

        if not tools_present:
            return None
        if api_type == "openai_responses":
            return "auto"
        if api_type == "anthropic_chat_completion":
            return {"type": "auto"}
        if api_type == "openai_chat_completion":
            return "auto"
        return None


class ProviderMessagePreparer:
    """Coerce and rewrite messages according to provider capability profiles."""

    def coerce_to_ump(self, messages: list[Message] | list[JsonObject]) -> list[Message]:
        """Coerce UMP or legacy chat messages into UMP messages.

        Args:
            messages: UMP messages or legacy OpenAI-chat-shaped dictionaries.

        Returns:
            UMP message objects ready for provider serialization.

        Raises:
            TypeError: If an item cannot be interpreted as UMP or legacy chat.
        """

        if not messages:
            return []
        if all(isinstance(message, Message) for message in messages):
            return cast(list[Message], messages)

        legacy_messages: list[JsonObject] = []
        for message in messages:
            if isinstance(message, Message):
                legacy_dict = getattr(message, "_as_legacy_openai_chat_dict", None)
                if callable(legacy_dict):
                    legacy_messages.append(cast(JsonObject, legacy_dict()))
                    continue
                raise TypeError(f"Unsupported UMP message object for provider payload shaping: {type(message)!r}")
            if isinstance(message, Mapping):
                legacy_messages.append(cast(JsonObject, dict(message)))
                continue
            legacy_dict = getattr(message, "_as_legacy_openai_chat_dict", None)
            if callable(legacy_dict):
                legacy_messages.append(cast(JsonObject, legacy_dict()))
                continue
            raise TypeError(f"Unsupported message type for provider payload shaping: {type(message)!r}")
        return messages_from_legacy_openai_chat(legacy_messages)

    def prepare(self, *, llm_config: LLMConfig, messages: list[Message] | list[JsonObject]) -> list[Message]:
        """Apply provider-specific runtime message rewrites.

        Args:
            llm_config: NexAU LLM configuration with provider profile flags.
            messages: UMP messages or legacy OpenAI-chat-shaped dictionaries.

        Returns:
            UMP messages after image transcoding or other provider rewrites.
        """

        ump_messages = self.coerce_to_ump(messages)
        profile = resolve_provider_profile(llm_config)
        if profile.transcode_images_to_jpeg:
            ump_messages = ProviderImageTranscoder().transcode_messages(ump_messages)
        ump_messages = _DEFAULT_REASONING_REPLAY_PROJECTOR.project_messages(
            ump_messages,
            api_type=llm_config.api_type,
            policy=reasoning_replay_policy_from_config(llm_config),
        )
        tool_image_policy = tool_image_policy_from_config(llm_config)
        install_nexau_tool_image_policy_patch()
        return apply_tool_image_policy_metadata(ump_messages, policy=tool_image_policy)


class ProviderAPIParamsProjector:
    """Project NexAU LLMConfig fields into provider request params."""

    northcua_only_keys = (
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
    )

    def project(self, llm_config: LLMConfig) -> JsonObject:
        """Return provider request params without NorthCUA-only capability flags.

        Args:
            llm_config: NexAU LLM configuration to project.

        Returns:
            Provider API parameters safe to merge into the outgoing request.
        """

        params = dict(llm_config.to_openai_params())
        for key in self.northcua_only_keys:
            params.pop(key, None)
        return params


class ProviderPayloadInputBuilder:
    """Prepare shared payload inputs before provider strategy serialization."""

    def __init__(
        self,
        *,
        message_preparer: ProviderMessagePreparer | None = None,
        tool_adapter: StructuredToolSchemaAdapter | None = None,
        image_inspector: ProviderImageInspector | None = None,
        api_params_projector: ProviderAPIParamsProjector | None = None,
        reasoning_replay_projector: ReasoningReplayProjector | None = None,
    ) -> None:
        self.message_preparer = message_preparer or ProviderMessagePreparer()
        self.tool_adapter = tool_adapter or StructuredToolSchemaAdapter()
        self.image_inspector = image_inspector or ProviderImageInspector()
        self.api_params_projector = api_params_projector or ProviderAPIParamsProjector()
        self.reasoning_replay_projector = reasoning_replay_projector or ReasoningReplayProjector()

    def build(self, request: ProviderPayloadBuildRequest) -> ProviderPayloadInputs:
        """Prepare shared payload inputs before provider strategy serialization.

        Args:
            request: Full provider payload build request from a strategy.

        Returns:
            Provider-neutral payload inputs shared by all strategy serializers.

        Raises:
            ValueError: If the selected provider profile does not allow images
                present in the request.
            TypeError: If legacy messages cannot be coerced into UMP messages.
        """

        profile = resolve_provider_profile(request.llm_config)
        replay_policy = reasoning_replay_policy_from_config(request.llm_config)
        ump_messages = self.message_preparer.prepare(
            llm_config=request.llm_config,
            messages=request.messages,
        )
        ump_messages = self.reasoning_replay_projector.project_messages(
            ump_messages,
            api_type=request.llm_config.api_type,
            policy=replay_policy,
        )
        tool_image_policy = tool_image_policy_from_config(request.llm_config)
        openai_chat_messages = _DEFAULT_NEXAU_PROVIDER_ADAPTER.serialize_openai_chat(
            ump_messages,
            tool_image_policy=tool_image_policy.value,
        )
        if request.llm_config.api_type == "openai_chat_completion":
            openai_chat_messages = self.reasoning_replay_projector.sanitize_openai_chat_messages(
                openai_chat_messages,
                policy=replay_policy,
            )
        if self.image_inspector.contains_images(openai_chat_messages) and not profile.supports_images:
            raise ValueError(
                f"Gateway profile {profile.gateway_name or 'current'} does not support image input for {request.llm_config.api_type}.",
            )

        tools = list(request.provider_tools)
        if not tools and request.tool_definitions:
            tools = self.tool_adapter.adapt(
                tool_definitions=request.tool_definitions,
                api_type=request.llm_config.api_type,
            )
        resolved_tool_choice = (
            request.tool_choice
            if request.tool_choice is not None
            else self.tool_adapter.default_tool_choice(
                request.llm_config.api_type,
                tools_present=bool(tools),
            )
        )

        api_params = self.api_params_projector.project(request.llm_config)
        api_params.pop("stream", None)
        return ProviderPayloadInputs(
            profile=profile,
            ump_messages=ump_messages,
            openai_chat_messages=openai_chat_messages,
            tools=tools,
            resolved_tool_choice=resolved_tool_choice,
            api_params=api_params,
        )


_DEFAULT_PAYLOAD_INPUT_BUILDER = ProviderPayloadInputBuilder()
_DEFAULT_TOOL_ADAPTER = StructuredToolSchemaAdapter()
_DEFAULT_MESSAGE_PREPARER = ProviderMessagePreparer()
_DEFAULT_IMAGE_INSPECTOR = ProviderImageInspector()
_DEFAULT_IMAGE_TRANSCODER = ProviderImageTranscoder()
_DEFAULT_API_PARAMS_PROJECTOR = ProviderAPIParamsProjector()


def build_payload_inputs(request: ProviderPayloadBuildRequest) -> ProviderPayloadInputs:
    """Prepare shared payload inputs before provider strategy serialization.

    Args:
        request: Provider payload build request.

    Returns:
        Shared provider payload inputs.

    Raises:
        ValueError: If the provider profile rejects required image input.
        TypeError: If legacy messages cannot be coerced into UMP messages.
    """

    return _DEFAULT_PAYLOAD_INPUT_BUILDER.build(request)


def adapt_structured_tools_for_api_type(
    *,
    tool_definitions: list[StructuredToolDefinitionLike],
    api_type: str,
) -> list[JsonObject]:
    """Convert neutral structured tool definitions into provider-facing schemas.

    Args:
        tool_definitions: Neutral structured tool definitions.
        api_type: NexAU provider API type.

    Returns:
        Provider-facing tool schema dictionaries.
    """

    return _DEFAULT_TOOL_ADAPTER.adapt(tool_definitions=tool_definitions, api_type=api_type)


def default_tool_choice_for_api_type(api_type: str, *, tools_present: bool) -> JsonObject | str | None:
    """Return the provider-native default tool_choice shape for one api_type.

    Args:
        api_type: NexAU provider API type.
        tools_present: Whether the request includes tools.

    Returns:
        Provider-native default tool choice, or None when no default should be sent.
    """

    return _DEFAULT_TOOL_ADAPTER.default_tool_choice(api_type, tools_present=tools_present)


def coerce_to_ump_messages(messages: list[Message] | list[JsonObject]) -> list[Message]:
    """Coerce UMP or legacy chat messages into UMP messages.

    Args:
        messages: UMP messages or legacy OpenAI-chat-style dictionaries.

    Returns:
        UMP messages.

    Raises:
        TypeError: If one message cannot be coerced.
    """

    return _DEFAULT_MESSAGE_PREPARER.coerce_to_ump(messages)


def prepare_messages_for_provider(
    *, llm_config: LLMConfig, messages: list[Message] | list[JsonObject]
) -> list[Message]:
    """Apply provider-specific runtime message rewrites.

    Args:
        llm_config: NexAU LLM config with NorthCUA provider extensions.
        messages: UMP messages or legacy OpenAI-chat-style dictionaries.

    Returns:
        Provider-adjusted UMP messages.

    Raises:
        TypeError: If legacy messages cannot be coerced into UMP messages.
    """

    return _DEFAULT_MESSAGE_PREPARER.prepare(llm_config=llm_config, messages=messages)


def provider_api_params(llm_config: LLMConfig) -> JsonObject:
    """Return provider request params without NorthCUA-only capability flags.

    Args:
        llm_config: NexAU LLM config.

    Returns:
        Provider params ready to merge into request payloads.
    """

    return _DEFAULT_API_PARAMS_PROJECTOR.project(llm_config)


def payload_contains_images(openai_chat_messages: list[JsonObject]) -> bool:
    """Return whether OpenAI-chat-shaped messages contain images.

    Args:
        openai_chat_messages: OpenAI-chat-shaped messages to inspect.

    Returns:
        True when at least one image block is present.
    """

    return _DEFAULT_IMAGE_INSPECTOR.contains_images(openai_chat_messages)


def count_openai_chat_images(messages: list[JsonObject]) -> int:
    """Count image URL blocks in OpenAI-chat-shaped messages.

    Args:
        messages: OpenAI-chat-shaped messages.

    Returns:
        Number of image blocks.
    """

    return _DEFAULT_IMAGE_INSPECTOR.count_openai_chat_images(messages)


def transcode_messages_to_jpeg(messages: list[Message]) -> list[Message]:
    """Transcode inline image blocks to JPEG for gateway paths that require it.

    Args:
        messages: UMP messages.

    Returns:
        UMP messages with inline image blocks rewritten when possible.
    """

    return _DEFAULT_IMAGE_TRANSCODER.transcode_messages(messages)


def transcode_blocks_to_jpeg(blocks: Sequence[object]) -> list[object]:
    """Transcode image blocks nested inside one message content list.

    Args:
        blocks: UMP message content blocks.

    Returns:
        Content blocks with inline image blocks converted when possible.
    """

    return _DEFAULT_IMAGE_TRANSCODER.transcode_blocks(blocks)


def transcode_image_block_to_jpeg(block: ImageBlock) -> ImageBlock:
    """Best-effort convert one inline image block to JPEG base64.

    Args:
        block: UMP image block.

    Returns:
        JPEG image block when conversion succeeds, otherwise the original block.
    """

    return _DEFAULT_IMAGE_TRANSCODER.transcode_image_block(block)


def image_block_bytes(block: ImageBlock) -> bytes | None:
    """Decode bytes from a base64-backed image block.

    Args:
        block: UMP image block containing either base64 data or a data URL.

    Returns:
        Decoded image bytes, or None when the block cannot be decoded locally.
    """

    return _DEFAULT_IMAGE_TRANSCODER.image_block_bytes(block)
