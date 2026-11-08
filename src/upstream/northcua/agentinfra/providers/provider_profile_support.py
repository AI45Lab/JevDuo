"""Shared helpers for provider capability profile strategies."""

from __future__ import annotations

from northcua.agentinfra.providers.provider_profiles import (
    ImageTransportPolicy,
    ProviderCapabilityProfile,
)


def apply_provider_profile_overrides(
    profile: ProviderCapabilityProfile,
    *,
    image_transport: ImageTransportPolicy | None,
    transcode_images_to_jpeg: bool | None,
    supports_images: bool | None,
    supports_tool_calls: bool | None,
    supports_structured_output: bool | None,
    preferred_for_multimodal: bool | None,
    supports_responses_api: bool | None,
) -> ProviderCapabilityProfile:
    """Apply optional explicit overrides to a provider capability profile.

    Args:
        profile: Provider-family default profile.
        image_transport: Optional image transport override.
        transcode_images_to_jpeg: Optional image transcoding override.
        supports_images: Optional image support override.
        supports_tool_calls: Optional tool-call support override.
        supports_structured_output: Optional structured output support override.
        preferred_for_multimodal: Optional multimodal preference override.
        supports_responses_api: Optional OpenAI Responses API support override.

    Returns:
        Capability profile with explicit overrides applied.
    """

    return ProviderCapabilityProfile(
        provider_family=profile.provider_family,
        gateway_name=profile.gateway_name,
        supports_images=profile.supports_images if supports_images is None else supports_images,
        supports_tool_calls=profile.supports_tool_calls if supports_tool_calls is None else supports_tool_calls,
        supports_structured_output=(
            profile.supports_structured_output if supports_structured_output is None else supports_structured_output
        ),
        preferred_for_multimodal=(
            profile.preferred_for_multimodal if preferred_for_multimodal is None else preferred_for_multimodal
        ),
        supports_responses_api=(
            profile.supports_responses_api if supports_responses_api is None else supports_responses_api
        ),
        image_transport=profile.image_transport if image_transport is None else image_transport,
        transcode_images_to_jpeg=(
            profile.transcode_images_to_jpeg if transcode_images_to_jpeg is None else transcode_images_to_jpeg
        ),
        notes=profile.notes,
    )
