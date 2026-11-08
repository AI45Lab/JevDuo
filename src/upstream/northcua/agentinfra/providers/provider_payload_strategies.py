"""Provider payload strategy registry used by provider payload facades."""

from __future__ import annotations

from northcua.agentinfra.nexau_compat.adapters.providers import NexAUProviderAdapter
from northcua.agentinfra.providers.provider_payload_inputs import build_payload_inputs
from northcua.agentinfra.providers.provider_payloads import (
    PreparedProviderPayload,
    ProviderPayloadBuildRequest,
)
from northcua.agentinfra.providers.provider_profiles import ProviderFamily

# pyright: reportMissingImports=false

_NEXAU_PROVIDER_ADAPTER = NexAUProviderAdapter()


class ProviderPayloadStrategy:
    """Provider payload strategy for one wire family."""

    provider_family = ProviderFamily.UNKNOWN

    def build(self, request: ProviderPayloadBuildRequest) -> PreparedProviderPayload:
        """Build a provider payload for this strategy.

        Args:
            request: Payload build request.

        Returns:
            Prepared provider payload.
        """

        inputs = build_payload_inputs(request)
        return PreparedProviderPayload(
            provider_family=self.provider_family,
            request_payload=dict(inputs.api_params),
            trace_messages=inputs.openai_chat_messages,
        )


class OpenAIResponsesPayloadStrategy(ProviderPayloadStrategy):
    """Build OpenAI Responses API payloads."""

    provider_family = ProviderFamily.OPENAI_RESPONSES

    def build(self, request: ProviderPayloadBuildRequest) -> PreparedProviderPayload:
        """Build an OpenAI Responses payload.

        Args:
            request: Payload build request.

        Returns:
            Prepared provider payload.
        """

        inputs = build_payload_inputs(request)
        request_payload = dict(inputs.api_params)
        request_payload.pop("store", None)
        max_tokens = request_payload.pop("max_tokens", None)
        if max_tokens is not None:
            request_payload.setdefault("max_output_tokens", max_tokens)
        responses_input = _NEXAU_PROVIDER_ADAPTER.prepare_openai_responses_input(inputs.openai_chat_messages)
        request_payload["input"] = responses_input.items
        if responses_input.instructions:
            request_payload["instructions"] = responses_input.instructions
        if inputs.tools:
            request_payload["tools"] = inputs.tools
        if inputs.resolved_tool_choice is not None:
            request_payload["tool_choice"] = inputs.resolved_tool_choice
        request_payload.setdefault("parallel_tool_calls", request.parallel_tool_calls)
        return PreparedProviderPayload(
            provider_family=ProviderFamily.OPENAI_RESPONSES,
            request_payload=request_payload,
            trace_messages=inputs.openai_chat_messages,
        )


class OpenAIChatPayloadStrategy(ProviderPayloadStrategy):
    """Build OpenAI chat-completions-compatible payloads."""

    provider_family = ProviderFamily.OPENAI_CHAT

    def build(self, request: ProviderPayloadBuildRequest) -> PreparedProviderPayload:
        """Build an OpenAI chat-completions-compatible payload.

        Args:
            request: Payload build request.

        Returns:
            Prepared provider payload.
        """

        inputs = build_payload_inputs(request)
        request_payload = dict(inputs.api_params)
        request_payload["messages"] = inputs.openai_chat_messages
        if inputs.tools:
            request_payload["tools"] = inputs.tools
        if inputs.resolved_tool_choice is not None:
            request_payload["tool_choice"] = inputs.resolved_tool_choice
        return PreparedProviderPayload(
            provider_family=ProviderFamily.OPENAI_CHAT,
            request_payload=request_payload,
            trace_messages=inputs.openai_chat_messages,
        )


class AnthropicMessagesPayloadStrategy(ProviderPayloadStrategy):
    """Build Anthropic Messages-compatible payloads."""

    provider_family = ProviderFamily.ANTHROPIC_MESSAGES

    def build(self, request: ProviderPayloadBuildRequest) -> PreparedProviderPayload:
        """Build an Anthropic Messages-compatible payload.

        Args:
            request: Payload build request.

        Returns:
            Prepared provider payload.
        """

        inputs = build_payload_inputs(request)
        anthropic = _NEXAU_PROVIDER_ADAPTER.serialize_anthropic(inputs.ump_messages)
        system_blocks, convo = anthropic.system, anthropic.messages
        request_payload = dict(inputs.api_params)
        request_payload["messages"] = convo
        if system_blocks:
            request_payload["system"] = system_blocks
        if inputs.tools:
            request_payload["tools"] = inputs.tools
        if inputs.resolved_tool_choice is not None:
            request_payload["tool_choice"] = inputs.resolved_tool_choice
        return PreparedProviderPayload(
            provider_family=ProviderFamily.ANTHROPIC_MESSAGES,
            request_payload=request_payload,
            trace_messages={
                "system": system_blocks,
                "messages": convo,
            },
        )


class UnknownProviderPayloadStrategy(ProviderPayloadStrategy):
    """Conservative trace-only payload strategy for unknown providers."""


class GeminiRestPayloadStrategy(ProviderPayloadStrategy):
    """Conservative trace-only payload strategy for Gemini REST until runtime support lands."""

    provider_family = ProviderFamily.GEMINI_REST


class ProviderPayloadStrategyRegistry:
    """Registry resolving provider payload builders by provider family."""

    def __init__(self) -> None:
        """Initialize an empty payload strategy registry."""

        self._strategies: dict[ProviderFamily, ProviderPayloadStrategy] = {}

    def register(self, strategy: ProviderPayloadStrategy) -> None:
        """Register or replace one provider payload strategy.

        Args:
            strategy: Strategy instance keyed by its provider family.
        """

        self._strategies[strategy.provider_family] = strategy

    def strategy_for(self, provider_family: ProviderFamily) -> ProviderPayloadStrategy:
        """Return a payload strategy for the provider family.

        Args:
            provider_family: Provider family to resolve.

        Returns:
            Matching strategy, or the unknown-provider fallback.
        """

        return (
            self._strategies.get(provider_family)
            or self._strategies.get(ProviderFamily.UNKNOWN)
            or UnknownProviderPayloadStrategy()
        )


def default_provider_payload_strategy_registry() -> ProviderPayloadStrategyRegistry:
    """Create the default payload strategy registry.

    Returns:
        Registry with built-in provider payload strategies.
    """

    registry = ProviderPayloadStrategyRegistry()
    registry.register(OpenAIResponsesPayloadStrategy())
    registry.register(OpenAIChatPayloadStrategy())
    registry.register(AnthropicMessagesPayloadStrategy())
    registry.register(GeminiRestPayloadStrategy())
    registry.register(UnknownProviderPayloadStrategy())
    return registry
