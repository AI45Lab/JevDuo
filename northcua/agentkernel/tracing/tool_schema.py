"""Durable main-trace tool schema compatibility."""

from __future__ import annotations

from collections.abc import Mapping

from northcua.agentkernel.json_types import JsonObject, ProviderToolSpecs


def legacy_trace_tool_definitions(tools: ProviderToolSpecs) -> ProviderToolSpecs:
    """Normalize provider tool schemas to NorthCUA's stable trace shape."""

    definitions: ProviderToolSpecs = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        source = function if isinstance(function, Mapping) else tool
        name = source.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = source.get("description")
        schema = source.get("input_schema")
        if not isinstance(schema, Mapping):
            schema = source.get("parameters")
        definition: JsonObject = {
            "name": name,
            "description": str(description or ""),
            "input_schema": dict(schema) if isinstance(schema, Mapping) else {"type": "object"},
            "kind": "tool",
        }
        definitions.append(definition)
    return definitions


__all__ = ["legacy_trace_tool_definitions"]
