"""Provider capability strategy registry."""

from __future__ import annotations

from northcua.agentinfra.providers.provider_profile_support import apply_provider_profile_overrides
from northcua.agentinfra.providers.provider_profiles import (
    ImageTransportPolicy,
    ProviderCapabilityProfile,
    ProviderFamily,
    ProviderProfileOverrides,
)


class ProviderCapabilityStrategy:
    """Capability strategy for one provider family."""

    provider_family: ProviderFamily = ProviderFamily.UNKNOWN

    def default_profile(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Return provider-family defaults before explicit overrides.

        Args:
            overrides: Explicit config/environment overrides.

        Returns:
            Default capability profile for this strategy.
        """

        return ProviderCapabilityProfile(
            provider_family=self.provider_family,
            gateway_name=overrides.gateway_name,
            supports_images=False,
            supports_tool_calls=False,
            supports_structured_output=False,
            preferred_for_multimodal=False,
            supports_responses_api=False,
            image_transport=ImageTransportPolicy.UNSUPPORTED,
        )

    def resolve(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Resolve the final profile for this strategy.

        Args:
            overrides: Explicit config/environment overrides.

        Returns:
            Capability profile after defaults and overrides are combined.
        """

        return apply_provider_profile_overrides(
            self.default_profile(overrides),
            image_transport=overrides.image_transport,
            transcode_images_to_jpeg=overrides.transcode_images_to_jpeg,
            supports_images=overrides.supports_images,
            supports_tool_calls=overrides.supports_tool_calls,
            supports_structured_output=overrides.supports_structured_output,
            preferred_for_multimodal=overrides.preferred_for_multimodal,
            supports_responses_api=overrides.supports_responses_api,
        )


class OpenAIResponsesCapabilityStrategy(ProviderCapabilityStrategy):
    """Capability defaults for OpenAI Responses-compatible providers."""

    provider_family = ProviderFamily.OPENAI_RESPONSES

    def default_profile(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Return OpenAI Responses defaults.

        Args:
            overrides: Explicit config/environment overrides that provide gateway metadata.

        Returns:
            Default OpenAI Responses capability profile before explicit override fields are applied.
        """

        return ProviderCapabilityProfile(
            provider_family=self.provider_family,
            gateway_name=overrides.gateway_name,
            supports_images=True,
            supports_tool_calls=True,
            supports_structured_output=True,
            preferred_for_multimodal=False,
            supports_responses_api=True,
            image_transport=ImageTransportPolicy.OPENAI_DATA_URL,
        )


class OpenAIChatCapabilityStrategy(ProviderCapabilityStrategy):
    """Capability defaults for OpenAI chat-completions-compatible providers."""

    provider_family = ProviderFamily.OPENAI_CHAT

    def default_profile(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Return OpenAI chat-completion defaults.

        Args:
            overrides: Explicit config/environment overrides that provide gateway metadata.

        Returns:
            Default OpenAI chat-completions capability profile before explicit override fields are applied.
        """

        return ProviderCapabilityProfile(
            provider_family=self.provider_family,
            gateway_name=overrides.gateway_name,
            supports_images=True,
            supports_tool_calls=True,
            supports_structured_output=True,
            preferred_for_multimodal=False,
            supports_responses_api=False,
            image_transport=ImageTransportPolicy.OPENAI_DATA_URL,
        )


class AnthropicMessagesCapabilityStrategy(ProviderCapabilityStrategy):
    """Capability defaults for Anthropic Messages-compatible providers."""

    provider_family = ProviderFamily.ANTHROPIC_MESSAGES

    def default_profile(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Return Anthropic Messages defaults.

        Args:
            overrides: Explicit config/environment overrides that provide gateway metadata.

        Returns:
            Default Anthropic Messages capability profile before explicit override fields are applied.
        """

        return ProviderCapabilityProfile(
            provider_family=self.provider_family,
            gateway_name=overrides.gateway_name,
            supports_images=True,
            supports_tool_calls=True,
            supports_structured_output=False,
            preferred_for_multimodal=True,
            supports_responses_api=False,
            image_transport=ImageTransportPolicy.ANTHROPIC_BASE64,
            transcode_images_to_jpeg=True,
            notes=("Anthropic-compatible multimodal payloads are normalized to JPEG base64 image blocks.",),
        )


class GeminiRestCapabilityStrategy(ProviderCapabilityStrategy):
    """Capability defaults for Gemini REST-compatible providers."""

    provider_family = ProviderFamily.GEMINI_REST

    def default_profile(self, overrides: ProviderProfileOverrides) -> ProviderCapabilityProfile:
        """Return Gemini REST defaults.

        Args:
            overrides: Explicit config/environment overrides that provide gateway metadata.

        Returns:
            Default Gemini REST capability profile before explicit override fields are applied.
        """

        return ProviderCapabilityProfile(
            provider_family=self.provider_family,
            gateway_name=overrides.gateway_name,
            supports_images=True,
            supports_tool_calls=True,
            supports_structured_output=False,
            preferred_for_multimodal=True,
            supports_responses_api=False,
            image_transport=ImageTransportPolicy.UNSUPPORTED,
        )


class UnknownProviderCapabilityStrategy(ProviderCapabilityStrategy):
    """Conservative defaults for unsupported provider families."""

    provider_family = ProviderFamily.UNKNOWN


class ProviderStrategyRegistry:
    """Registry resolving provider-family strategies."""

    def __init__(self) -> None:
        """Initialize an empty provider strategy registry."""

        self._capability_strategies: dict[ProviderFamily, ProviderCapabilityStrategy] = {}

    def register_capability_strategy(self, strategy: ProviderCapabilityStrategy) -> None:
        """Register or replace one capability strategy.

        Args:
            strategy: Strategy keyed by its provider family.
        """

        self._capability_strategies[strategy.provider_family] = strategy

    def capability_strategy_for(self, provider_family: ProviderFamily) -> ProviderCapabilityStrategy:
        """Return the matching capability strategy or the unknown fallback.

        Args:
            provider_family: Provider family to resolve.

        Returns:
            Matching strategy or the unknown-provider fallback.
        """

        return (
            self._capability_strategies.get(provider_family)
            or self._capability_strategies.get(ProviderFamily.UNKNOWN)
            or UnknownProviderCapabilityStrategy()
        )


def default_provider_strategy_registry() -> ProviderStrategyRegistry:
    """Create the default provider strategy registry.

    Returns:
        Registry with built-in provider capability strategies.
    """

    registry = ProviderStrategyRegistry()
    registry.register_capability_strategy(OpenAIResponsesCapabilityStrategy())
    registry.register_capability_strategy(OpenAIChatCapabilityStrategy())
    registry.register_capability_strategy(AnthropicMessagesCapabilityStrategy())
    registry.register_capability_strategy(GeminiRestCapabilityStrategy())
    registry.register_capability_strategy(UnknownProviderCapabilityStrategy())
    return registry
