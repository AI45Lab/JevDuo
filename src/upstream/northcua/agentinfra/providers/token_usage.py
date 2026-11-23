"""Durable per-provider-call token receipts; absent fields are never estimated."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4


def normalize_usage(raw):
    usage = raw if isinstance(raw, dict) else {}

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    details = usage.get("prompt_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or {}
    cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
    alternate = count(usage.get("prompt_cache_hit_tokens"))
    conflict = cached is not None and alternate is not None and cached != alternate
    if cached is None:
        cached = alternate
    prompt, output = count(usage.get("prompt_tokens")), count(usage.get("completion_tokens"))
    if prompt is None:
        prompt = count(usage.get("input_tokens"))
    if output is None:
        output = count(usage.get("output_tokens"))
    if conflict or (prompt is not None and cached is not None and cached > prompt):
        cached = None
        conflict = True
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "cache_input_tokens": cached,
        "uncached_input_tokens": prompt - cached if prompt is not None and cached is not None else None,
        "total_tokens": count(usage.get("total_tokens")),
        "reasoning_tokens": count(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict) else None,
        "cache_fields_conflict": conflict,
    }


class ModelUsageLedger:
    def __init__(self, path: Path, metadata: dict):
        self.path, self.metadata = path, metadata

    def _write(self, event, call_id, **payload):
        from .chat_response_capture import _append_receipt

        _append_receipt(self.path, {
            **self.metadata, "schema_version": 1, "event": event,
            "call_id": call_id, "at": time.time(), **payload,
        })

    def start(self, model, **payload):
        call_id = uuid4().hex
        self._write("request", call_id, model=model, **payload)
        return call_id

    def response(self, call_id, response, **payload):
        raw = response.get("usage") if isinstance(response, dict) else None
        self._write("response", call_id, response_id=response.get("id") if isinstance(response, dict) else None,
                    response_model=response.get("model") if isinstance(response, dict) else None,
                    raw_usage=raw, tokens=normalize_usage(raw), **payload)

    def failure(self, call_id, error):
        self._write("failure", call_id, error_type=type(error).__name__)
