"""Normalize public NexAU response additions back to private-pin contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from nexau.archs.main_sub.execution.model_response import ModelResponse


def normalize_legacy_model_response(
    api_type: str,
    response: ModelResponse | None,
    *,
    stream: bool = False,
) -> ModelResponse | None:
    """Remove only response fields the private pin would not have produced."""

    if response is None:
        return None
    # Private parsers sometimes retained an empty reasoning string internally,
    # but their UMP conversion used truthiness and never persisted an empty
    # ReasoningBlock. Public NexAU switched that boundary to ``is not None``.
    # Normalize before history/session conversion for every provider.
    if response.reasoning_content == "":
        response.reasoning_content = None
    if api_type == "openai_chat_completion" and _has_bare_openai_reasoning(response.raw_message):
        response.reasoning_content = None
    if api_type == "openai_responses":
        response.response_items = [_legacy_responses_item(item) for item in response.response_items]
    if api_type == "anthropic_chat_completion" and _has_empty_anthropic_signature(response.raw_message):
        response.reasoning_signature = ""
    if api_type == "anthropic_chat_completion" and stream:
        # Private streaming retained final output usage but failed to forward
        # input/cache usage from message_start. Limit this compatibility
        # projection to streams; non-stream accounting remains untouched.
        response.usage = replace(
            response.usage,
            input_tokens=0,
            completion_tokens=response.usage.completion_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            total_tokens=response.usage.completion_tokens + response.usage.reasoning_tokens,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            input_tokens_uncached=0,
        )
    return response


def _legacy_responses_item(item: dict[str, object]) -> dict[str, object]:
    """Remove fields added by public SDK stream aggregation.

    Private ``58d3fa4`` stored the same semantic output items without lifecycle
    status or newly introduced nullable members. Keeping those additions would
    alter resumed Responses input and serialized artifacts after a dependency-
    only migration.
    """

    normalized = dict(item)
    normalized.pop("status", None)
    for key in ("content", "namespace", "phase"):
        if normalized.get(key) is None:
            normalized.pop(key, None)
    return normalized


def _has_bare_openai_reasoning(raw_message: object) -> bool:
    if isinstance(raw_message, Mapping):
        return "reasoning" in raw_message and "reasoning_content" not in raw_message
    return hasattr(raw_message, "reasoning") and not hasattr(raw_message, "reasoning_content")


def _has_empty_anthropic_signature(raw_message: object) -> bool:
    if isinstance(raw_message, Mapping):
        content = raw_message.get("content")
    else:
        content = getattr(raw_message, "content", None)
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return False
    for block in content:
        if isinstance(block, Mapping):
            block_type = block.get("type")
            signature = block.get("signature")
        else:
            block_type = getattr(block, "type", None)
            signature = getattr(block, "signature", None)
        if block_type == "thinking" and signature == "":
            return True
    return False


__all__ = ["normalize_legacy_model_response"]
