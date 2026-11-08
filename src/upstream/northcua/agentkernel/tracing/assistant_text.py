"""Assistant text extraction for provider-specific response shapes."""

from __future__ import annotations

from .state import ProviderResponsePayload


def _extract_assistant_text(
    *,
    content: object | None,
    original_response: object | None,
    response_payload: ProviderResponsePayload,
) -> str:
    """Extract assistant text across provider response payload shapes.

    Args:
        content: NexAU-normalized model response content, when populated.
        original_response: Hook-provided original response text.
        response_payload: Raw provider response payload.

    Returns:
        Assistant-visible text, or an empty string when unavailable.
    """

    for candidate in (content, original_response):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    for extractor in (
        _extract_openai_responses_text,
        _extract_openai_chat_text,
        _extract_anthropic_text,
        _extract_generic_text,
    ):
        text = extractor(response_payload)
        if text.strip():
            return text
    return ""


def _extract_openai_responses_text(payload: ProviderResponsePayload) -> str:
    """Extract text from OpenAI Responses-style output items."""

    output_items = payload.get("output")
    if not isinstance(output_items, list):
        return ""
    text_parts: list[str] = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str) and item.get("type") in {"output_text", "text"}:
            text_parts.append(str(item["text"]))
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str) and content_item.get("type") in {"output_text", "text"}:
                text_parts.append(text)
    return "\n".join(part for part in text_parts if part)


def _extract_openai_chat_text(payload: ProviderResponsePayload) -> str:
    """Extract text from OpenAI Chat Completions-style choices."""

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    text_parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        text_parts.extend(_text_parts_from_content(message.get("content")))
    return "\n".join(part for part in text_parts if part)


def _extract_anthropic_text(payload: ProviderResponsePayload) -> str:
    """Extract text from Anthropic Messages-style content blocks."""

    return "\n".join(_text_parts_from_content(payload.get("content")))


def _extract_generic_text(payload: ProviderResponsePayload) -> str:
    """Extract text from common fallback payload keys."""

    for key in ("assistant_text", "output_text", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _text_parts_from_content(content: object) -> list[str]:
    """Return text fragments from provider content fields."""

    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    text_parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and item.get("type") in {None, "text", "output_text"}:
                text_parts.append(text)
    return text_parts


__all__ = [
    "_extract_anthropic_text",
    "_extract_assistant_text",
    "_extract_generic_text",
    "_extract_openai_chat_text",
    "_extract_openai_responses_text",
    "_text_parts_from_content",
]
