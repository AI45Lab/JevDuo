"""Provider capability and gateway profile helpers.

Provider profiles describe coarse feature support for a configured NexAU
``api_type`` plus optional NorthCUA overrides supplied through extra params or
environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from nexau.archs.llm.llm_config import LLMConfig

from northcua.core.json_types import JsonObject

# pyright: reportMissingImports=false


if TYPE_CHECKING:
    from northcua.agentinfra.providers.provider_profile_strategies import ProviderStrategyRegistry


class ProviderFamily(str, Enum):
    """Top-level provider wire family used by NorthCUA traces and probes.

    Attributes:
        OPENAI_RESPONSES: OpenAI Responses API payload shape.
        OPENAI_CHAT: OpenAI-compatible chat completions payload shape.
        ANTHROPIC_MESSAGES: Anthropic Messages API payload shape.
        GEMINI_REST: Gemini REST payload shape.
        UNKNOWN: Unsupported or unrecognized provider payload shape.
    """

    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat_completion"
    ANTHROPIC_MESSAGES = "anthropic_chat_completion"
    GEMINI_REST = "gemini_rest"
    UNKNOWN = "unknown"


class ImageTransportPolicy(str, Enum):
    """How multimodal image inputs should be transported for one provider path.

    Attributes:
        OPENAI_DATA_URL: Send image inputs as OpenAI-compatible data URLs.
        ANTHROPIC_BASE64: Send image inputs as Anthropic base64 blocks.
        UNSUPPORTED: Treat image input as unsupported for this profile.
    """

    OPENAI_DATA_URL = "openai_data_url"
    ANTHROPIC_BASE64 = "anthropic_base64"
    UNSUPPORTED = "unsupported"


GatewayName = str | None


@dataclass(slots=True, frozen=True)
class ProviderCapabilityProfile:
    """Resolved provider/gateway capability profile for one configured LLM path.

    Attributes:
        provider_family: Provider wire family selected from the configured api_type.
        gateway_name: Optional explicit gateway/profile name.
        supports_images: Whether image input can be sent to the provider.
        supports_tool_calls: Whether tool calls are supported.
        supports_structured_output: Whether strict structured output is supported.
        preferred_for_multimodal: Whether this path is preferred for multimodal work.
        supports_responses_api: Whether the Responses API shape is supported.
        image_transport: Image transport policy for multimodal payloads.
        transcode_images_to_jpeg: Whether inline images should be rewritten to JPEG.
        notes: Human-readable capability notes.
    """

    provider_family: ProviderFamily
    gateway_name: GatewayName = None
    supports_images: bool = True
    supports_tool_calls: bool = True
    supports_structured_output: bool = True
    preferred_for_multimodal: bool = False
    supports_responses_api: bool = True
    image_transport: ImageTransportPolicy = ImageTransportPolicy.OPENAI_DATA_URL
    transcode_images_to_jpeg: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class ProviderProfileOverrides:
    """Explicit provider capability overrides from config/environment.

    Attributes:
        gateway_name: Optional gateway/profile label.
        image_transport: Optional image transport override.
        transcode_images_to_jpeg: Optional image transcoding override.
        supports_images: Optional image-support override.
        supports_tool_calls: Optional tool-call support override.
        supports_structured_output: Optional structured-output support override.
        preferred_for_multimodal: Optional multimodal preference override.
        supports_responses_api: Optional Responses API support override.
    """

    gateway_name: GatewayName = None
    image_transport: ImageTransportPolicy | None = None
    transcode_images_to_jpeg: bool | None = None
    supports_images: bool | None = None
    supports_tool_calls: bool | None = None
    supports_structured_output: bool | None = None
    preferred_for_multimodal: bool | None = None
    supports_responses_api: bool | None = None


def default_provider_strategy_registry() -> ProviderStrategyRegistry:
    """Create the default provider strategy registry.

    Returns:
        Registry with built-in provider capability strategies.
    """

    from northcua.agentinfra.providers.provider_profile_strategies import default_provider_strategy_registry as _default

    return _default()


def provider_family_from_api_type(api_type: str | None) -> ProviderFamily:
    """Map NexAU api_type to a coarse provider family.

    Args:
        api_type: NexAU provider API type.

    Returns:
        Matching provider family, or UNKNOWN for unsupported values.
    """

    normalized = (api_type or "").strip()
    if normalized == "openai_responses":
        return ProviderFamily.OPENAI_RESPONSES
    if normalized == "openai_chat_completion":
        return ProviderFamily.OPENAI_CHAT
    if normalized == "anthropic_chat_completion":
        return ProviderFamily.ANTHROPIC_MESSAGES
    if normalized == "gemini_rest":
        return ProviderFamily.GEMINI_REST
    return ProviderFamily.UNKNOWN


def detect_gateway_name(base_url: str | None = None, *, explicit_profile: str | None = None) -> GatewayName:
    """Resolve an explicitly configured provider/gateway profile name.

    NorthCUA intentionally does not infer private gateway names from URLs. Teams
    that need a gateway-specific capability profile should opt in through
    ``llm_config.provider_profile`` or ``NORTHCUA_PROVIDER_PROFILE``.

    Args:
        base_url: Provider base URL, intentionally ignored for profile inference.
        explicit_profile: Explicit provider or gateway profile label.

    Returns:
        Normalized gateway/profile name, or None when unset.
    """

    _ = base_url
    normalized = (explicit_profile or "").strip()
    return normalized or None


def resolve_provider_profile(llm_config: LLMConfig) -> ProviderCapabilityProfile:
    """Resolve provider capabilities for the current model/gateway path.

    Args:
        llm_config: NexAU LLM config with optional NorthCUA provider extensions.

    Returns:
        Resolved provider capability profile.
    """

    provider_family = provider_family_from_api_type(getattr(llm_config, "api_type", ""))
    strategy = default_provider_strategy_registry().capability_strategy_for(provider_family)
    return strategy.resolve(_profile_overrides_from_config(llm_config))


def _profile_overrides_from_config(llm_config: LLMConfig) -> ProviderProfileOverrides:
    extra_params = _extra_params(llm_config)
    return ProviderProfileOverrides(
        gateway_name=detect_gateway_name(
            explicit_profile=_optional_str(
                extra_params.get("provider_profile")
                or extra_params.get("gateway_profile")
                or extra_params.get("provider_gateway")
                or os.getenv("NORTHCUA_PROVIDER_PROFILE")
            )
        ),
        image_transport=_image_transport_policy(extra_params.get("image_transport")),
        transcode_images_to_jpeg=_optional_bool(extra_params.get("transcode_images_to_jpeg")),
        supports_images=_optional_bool(extra_params.get("supports_images")),
        supports_tool_calls=_optional_bool(extra_params.get("supports_tool_calls")),
        supports_structured_output=_optional_bool(extra_params.get("supports_structured_output")),
        preferred_for_multimodal=_optional_bool(extra_params.get("preferred_for_multimodal")),
        supports_responses_api=_optional_bool(extra_params.get("supports_responses_api")),
    )


def _extra_params(llm_config: LLMConfig) -> JsonObject:
    extra = getattr(llm_config, "extra_params", None)
    return dict(extra) if isinstance(extra, dict) else {}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _image_transport_policy(value: object) -> ImageTransportPolicy | None:
    text = _optional_str(value)
    if text is None:
        return None
    try:
        return ImageTransportPolicy(text)
    except ValueError:
        return None
