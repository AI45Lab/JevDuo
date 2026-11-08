"""Stable fingerprints for tool-contract snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from northcua_protocol.capabilities import JsonObject
from northcua_runtime_shared.tool_contracts.contracts import ToolContract


def normalized_json(value: object) -> str:
    """Return canonical JSON suitable for stable fingerprints."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def fingerprint_json(value: object) -> str:
    """Return a SHA-256 fingerprint for JSON-compatible values."""

    return hashlib.sha256(normalized_json(value).encode("utf-8")).hexdigest()


def fingerprint_contracts(contracts: Iterable[ToolContract], *, include_tools: bool = False) -> JsonObject:
    """Return a stable hash for a contract collection.

    Full contract summaries are intentionally opt-in because they can include
    large schemas and execution metadata that should not appear in routine
    audit/debug logs.
    """

    summaries = sorted((contract.to_summary() for contract in contracts), key=lambda item: str(item["tool_id"]))
    result: JsonObject = {
        "hash": fingerprint_json(summaries),
        "tool_count": len(summaries),
    }
    if include_tools:
        result["tools"] = summaries
    return result


__all__ = ["fingerprint_contracts", "fingerprint_json", "normalized_json"]
