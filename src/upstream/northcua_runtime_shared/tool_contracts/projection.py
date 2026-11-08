"""Projection helpers for NorthCUA tool contracts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from northcua_protocol.capabilities import JsonObject, ToolDescriptor

if TYPE_CHECKING:
    from northcua_runtime_shared.tool_contracts.contracts import ToolContract


_PROVIDER_UNSUPPORTED_SCHEMA_KEYS = {
    "$defs",
    "allOf",
    "anyOf",
    "dependentRequired",
    "not",
    "oneOf",
    "x-northcuaGreaterThan",
}

_JSON_SCHEMA_UNION_KEYS = {"anyOf", "oneOf"}


@dataclass(frozen=True, slots=True)
class ProviderSchemaProjection:
    """Provider-safe schema plus an explicit lossy-projection manifest."""

    schema: JsonObject
    lossiness_manifest: tuple[str, ...] = ()

    @property
    def lossy(self) -> bool:
        """Return whether provider projection removed schema semantics."""

        return bool(self.lossiness_manifest)


def empty_parameters_schema() -> JsonObject:
    """Return the stable empty object schema used for no-argument tools."""

    return {"type": "object", "additionalProperties": False, "properties": {}}


def schema_from_model(params_model: type[BaseModel] | None) -> JsonObject:
    """Return a JSON schema for a Pydantic model or an empty object schema."""

    if params_model is None:
        return empty_parameters_schema()
    return dict(params_model.model_json_schema())


def provider_safe_parameters_schema(
    schema: Mapping[str, object],
    *,
    preserve_unions: bool = False,
) -> JsonObject:
    """Return a provider-safe schema projection.

    Some provider adapters reject top-level JSON Schema combinators. NorthCUA
    keeps the full protocol schema for controller/runtime validation and uses
    this explicit projection for model-facing surfaces.
    """

    return provider_schema_projection(schema, preserve_unions=preserve_unions).schema


def provider_schema_projection(
    schema: Mapping[str, object],
    *,
    preserve_unions: bool = False,
) -> ProviderSchemaProjection:
    """Return provider projection with a manifest of removed semantics."""

    raw = deepcopy(dict(schema))
    unsupported_keys = _PROVIDER_UNSUPPORTED_SCHEMA_KEYS
    if preserve_unions:
        unsupported_keys = unsupported_keys - _JSON_SCHEMA_UNION_KEYS
    manifest = tuple(sorted(_lossiness_manifest(raw, unsupported_keys=unsupported_keys)))
    defs = raw.get("$defs") if isinstance(raw.get("$defs"), dict) else {}
    inlined = _inline_local_refs(raw, defs=cast(Mapping[str, object], defs))
    projected = cast(
        JsonObject,
        _strip_provider_unsupported_keys(inlined, unsupported_keys=unsupported_keys),
    )
    if not projected:
        return ProviderSchemaProjection(schema=empty_parameters_schema(), lossiness_manifest=manifest)
    projected.setdefault("type", "object")
    projected.setdefault("properties", {})
    return ProviderSchemaProjection(schema=projected, lossiness_manifest=manifest)


def _lossiness_manifest(
    value: object,
    *,
    unsupported_keys: set[str],
    path: str = "$",
) -> set[str]:
    manifest: set[str] = set()
    if isinstance(value, list):
        for index, item in enumerate(value):
            manifest.update(_lossiness_manifest(item, unsupported_keys=unsupported_keys, path=f"{path}[{index}]"))
        return manifest
    if not isinstance(value, dict):
        return manifest
    for key, child in value.items():
        key_str = str(key)
        child_path = f"{path}.{key_str}"
        if key_str in unsupported_keys:
            manifest.add(child_path)
        manifest.update(_lossiness_manifest(child, unsupported_keys=unsupported_keys, path=child_path))
    return manifest


def _strip_provider_unsupported_keys(value: object, *, unsupported_keys: set[str]) -> object:
    if isinstance(value, list):
        return [_strip_provider_unsupported_keys(item, unsupported_keys=unsupported_keys) for item in value]
    if not isinstance(value, dict):
        return value
    collapsed = _collapse_provider_union(value)
    if collapsed is not None:
        value = collapsed
    return {
        str(key): _strip_provider_unsupported_keys(child, unsupported_keys=unsupported_keys)
        for key, child in value.items()
        if str(key) not in unsupported_keys
    }


def _inline_local_refs(value: object, *, defs: Mapping[str, object]) -> object:
    if isinstance(value, list):
        return [_inline_local_refs(item, defs=defs) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        resolved = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(resolved, dict):
            merged = deepcopy(dict(resolved))
            for key, child in value.items():
                if key != "$ref":
                    merged[str(key)] = child
            return _inline_local_refs(merged, defs=defs)
    return {str(key): _inline_local_refs(child, defs=defs) for key, child in value.items()}


def _collapse_provider_union(value: Mapping[object, object]) -> dict[str, object] | None:
    for union_key in ("anyOf", "oneOf"):
        union_value = value.get(union_key)
        if not isinstance(union_value, list):
            continue
        collapsed = _collapse_typed_union(union_value)
        if collapsed is None:
            return None
        result = {str(key): child for key, child in value.items() if str(key) != union_key}
        result.update(collapsed)
        return result
    return None


def _collapse_typed_union(options: list[object]) -> dict[str, object] | None:
    option_dicts = [option for option in options if isinstance(option, dict)]
    if len(option_dicts) != len(options) or not option_dicts:
        return None

    types: list[object] = []
    non_null_options: list[dict[object, object]] = []
    for option in option_dicts:
        option_type = option.get("type")
        if option_type is None:
            return None
        option_types = option_type if isinstance(option_type, list) else [option_type]
        types.extend(option_types)
        if "null" not in option_types:
            non_null_options.append(option)

    if len(non_null_options) > 1 and any("properties" in option or "required" in option for option in non_null_options):
        return None

    collapsed: dict[str, object] = {}
    if len(non_null_options) == 1:
        collapsed.update(
            {
                str(key): deepcopy(child)
                for key, child in non_null_options[0].items()
                if str(key) not in {"description", "title", "type"}
            }
        )

    unique_types: list[object] = []
    for item in types:
        if item not in unique_types:
            unique_types.append(item)
    collapsed["type"] = unique_types[0] if len(unique_types) == 1 else unique_types
    return collapsed


def tool_descriptor_from_contract(contract: "ToolContract") -> ToolDescriptor:
    """Project a ``ToolContract`` to a protocol ``ToolDescriptor``."""

    return ToolDescriptor(
        tool_id=contract.tool_id,
        capability=contract.capability,
        schema=contract.parameters_schema,
        timeout_seconds=contract.timeout_seconds,
        side_effect=contract.side_effect,
        requires_focus=contract.requires_focus,
        risk_level=contract.risk_level,
        max_payload_bytes=contract.max_payload_bytes,
    )


__all__ = [
    "empty_parameters_schema",
    "ProviderSchemaProjection",
    "provider_schema_projection",
    "provider_safe_parameters_schema",
    "schema_from_model",
    "tool_descriptor_from_contract",
]
