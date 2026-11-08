"""Schema drift helpers for registered MCP servers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from fastmcp import FastMCP

from northcua_runtime_shared.tool_contracts import fingerprint_json


@dataclass(frozen=True, slots=True)
class ToolSchemaDrift:
    """One registered tool whose contract and live schema fingerprints differ."""

    tool_name: str
    contract_hash: str
    runtime_hash: str
    reason: str = "schema_mismatch"


async def compare_contracts_to_fastmcp(
    *,
    contract_schemas: Mapping[str, Mapping[str, object]],
    server: FastMCP,
) -> list[ToolSchemaDrift]:
    """Compare contract schemas with a live FastMCP server schema surface.

    FastMCP derives input metadata from Python function signatures and does not
    currently accept a full input-schema override. The audit therefore compares
    the compatible runtime metadata shape after removing cosmetic Pydantic
    fields and top-level strictness keywords that are enforced separately by
    contract validators at dispatch time.
    """

    drifts: list[ToolSchemaDrift] = []
    live_tools = {str(tool.name): dict(getattr(tool, "parameters", {}) or {}) for tool in await server.list_tools()}

    contract_names = set(contract_schemas)
    live_names = set(live_tools)
    for tool_name in sorted(contract_names - live_names):
        drifts.append(
            ToolSchemaDrift(
                tool_name=tool_name,
                contract_hash=fingerprint_json(_normalize_schema(contract_schemas[tool_name])),
                runtime_hash="",
                reason="missing_runtime_tool",
            )
        )
    for tool_name in sorted(live_names - contract_names):
        drifts.append(
            ToolSchemaDrift(
                tool_name=tool_name,
                contract_hash="",
                runtime_hash=fingerprint_json(_normalize_schema(live_tools[tool_name])),
                reason="extra_runtime_tool",
            )
        )

    for tool_name in sorted(contract_names & live_names):
        schema = contract_schemas[tool_name]
        contract_hash = fingerprint_json(_normalize_schema(schema))
        runtime_hash = fingerprint_json(_normalize_schema(live_tools[tool_name]))
        if contract_hash != runtime_hash:
            drifts.append(
                ToolSchemaDrift(
                    tool_name=tool_name,
                    contract_hash=contract_hash,
                    runtime_hash=runtime_hash,
                )
            )
    return drifts


def _normalize_schema(schema: Mapping[str, object]) -> dict[str, object]:
    raw = dict(schema)
    defs = raw.get("$defs") if isinstance(raw.get("$defs"), dict) else {}
    normalized = cast(dict[str, object], _normalize_schema_value(raw, defs=cast(dict[str, object], defs)))
    normalized.pop("$defs", None)
    normalized.pop("oneOf", None)
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    return normalized


def _normalize_schema_value(value: object, *, defs: Mapping[str, object]) -> object:
    if isinstance(value, list):
        return [_normalize_schema_value(item, defs=defs) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        resolved = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(resolved, dict):
            return _normalize_schema_value(resolved, defs=defs)
    stripped: dict[str, object] = {}
    for key, child in value.items():
        key_str = str(key)
        if key_str in {
            "additionalProperties",
            "default",
            "dependentRequired",
            "maximum",
            "maxItems",
            "maxLength",
            "minimum",
            "minItems",
            "minLength",
            "pattern",
            "title",
            "description",
        }:
            continue
        if key_str in {"anyOf", "oneOf"}:
            collapsed = _collapse_typed_union(child)
            if collapsed is None:
                continue
            collapsed_value = _normalize_schema_value(collapsed, defs=defs)
            if isinstance(collapsed_value, dict):
                stripped.update(cast(dict[str, object], collapsed_value))
            continue
        stripped[key_str] = _normalize_schema_value(child, defs=defs)
    return stripped


def _collapse_typed_union(value: object) -> dict[str, object] | None:
    if not isinstance(value, list) or not value:
        return None
    option_dicts = [option for option in value if isinstance(option, dict)]
    if len(option_dicts) != len(value):
        return None
    types: list[object] = []
    template: dict[str, object] = {}
    for option in option_dicts:
        if "properties" in option or "required" in option:
            return None
        option_type = option.get("type")
        if option_type is None:
            return None
        option_types = option_type if isinstance(option_type, list) else [option_type]
        types.extend(option_types)
        if "null" not in option_types and not template:
            template = {
                str(key): child for key, child in option.items() if str(key) not in {"description", "title", "type"}
            }
    unique_types: list[object] = []
    for item in types:
        if item not in unique_types:
            unique_types.append(item)
    template["type"] = unique_types[0] if len(unique_types) == 1 else unique_types
    return template


__all__ = ["ToolSchemaDrift", "compare_contracts_to_fastmcp"]
