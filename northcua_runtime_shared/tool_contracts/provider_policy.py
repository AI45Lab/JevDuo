"""Provider-boundary policies for model-visible tool schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import cast

from northcua_protocol.capabilities import JsonObject


def apply_openai_union_tool_policy(tools: Sequence[Mapping[str, object]]) -> list[JsonObject]:
    """Mark structural-union tools non-strict without mutating callers.

    OpenAI strict function schemas do not reliably accept top-level
    ``oneOf``/``anyOf``. Contracts that deliberately preserve one of those
    combinators must therefore use non-strict function calling while runtime
    validation remains authoritative.

    Args:
        tools: Provider-shaped function tools in Responses or Chat form.

    Returns:
        Deep-copied tool definitions with structural unions marked non-strict.
    """

    projected: list[JsonObject] = []
    for raw_tool in tools:
        tool = cast(JsonObject, deepcopy(dict(raw_tool)))
        function = tool.get("function")
        target = cast(JsonObject, function) if isinstance(function, dict) else tool
        schema = target.get("parameters")
        if not isinstance(schema, Mapping):
            schema = target.get("input_schema")
        if isinstance(schema, Mapping) and _has_structural_union(schema):
            target["strict"] = False
        projected.append(tool)
    return projected


def _has_structural_union(schema: Mapping[str, object]) -> bool:
    return isinstance(schema.get("oneOf"), list) or isinstance(schema.get("anyOf"), list)


__all__ = ["apply_openai_union_tool_policy"]
