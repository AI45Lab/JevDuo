"""Per-client OpenAI Responses compatibility at the final send boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from northcua.core.json_types import JsonObject

_PROXY_MARKER = "__northcua_legacy_openai_responses_client_proxy__"


def normalize_legacy_openai_responses_items(items: list[JsonObject]) -> list[JsonObject]:
    """Drop empty reasoning while retaining summaries and encrypted replay state."""

    normalized: list[JsonObject] = []
    for item in items:
        if item.get("type") != "reasoning":
            normalized.append(item)
            continue
        summary = item.get("summary")
        has_summary_text = isinstance(summary, list) and any(
            isinstance(entry, Mapping) and bool(str(entry.get("text") or "")) for entry in summary
        )
        if item.get("encrypted_content"):
            if not has_summary_text:
                item = dict(item)
                item["summary"] = []
            normalized.append(item)
        elif has_summary_text:
            normalized.append(item)
    return normalized


def wrap_openai_responses_client(client: object | None) -> object | None:
    """Return a delegating client that normalizes only Responses ``input``."""

    if client is None or getattr(client, _PROXY_MARKER, False):
        return client
    responses = getattr(client, "responses", None)
    if responses is None:
        return client
    return _OpenAIResponsesClientProxy(client, responses)


class _OpenAIResponsesClientProxy:
    """Delegate one sync or async OpenAI client with a wrapped resource."""

    __northcua_legacy_openai_responses_client_proxy__ = True

    def __init__(self, client: object, responses: object) -> None:
        self._client = client
        self.responses = _OpenAIResponsesResourceProxy(responses)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _OpenAIResponsesResourceProxy:
    """Normalize request input immediately before every SDK send boundary."""

    def __init__(self, responses: object) -> None:
        self._responses = responses

    def create(self, *args: object, **kwargs: object) -> object:
        return self._responses.create(*args, **self._normalize_request(kwargs))  # type: ignore[attr-defined]

    def stream(self, *args: object, **kwargs: object) -> object:
        return self._responses.stream(*args, **self._normalize_request(kwargs))  # type: ignore[attr-defined]

    @staticmethod
    def _normalize_request(kwargs: Mapping[str, object]) -> dict[str, object]:
        request = dict(kwargs)
        input_items = request.get("input")
        if isinstance(input_items, list) and all(isinstance(item, Mapping) for item in input_items):
            request["input"] = normalize_legacy_openai_responses_items([dict(item) for item in input_items])
        return request

    def __getattr__(self, name: str) -> Any:
        return getattr(self._responses, name)


__all__ = ["normalize_legacy_openai_responses_items", "wrap_openai_responses_client"]
