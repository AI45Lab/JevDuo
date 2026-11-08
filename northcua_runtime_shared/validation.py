"""Shared validation helpers for descriptor-backed runtime commands."""

from __future__ import annotations

import json
import re

from northcua_protocol.capabilities import JsonObject, ToolDescriptor


class ToolParameterValidationError(ValueError):
    """Raised when model-supplied tool parameters violate a descriptor schema."""


def validate_command_against_tool(
    *,
    tool_id: str,
    parameters: JsonObject,
    descriptor: ToolDescriptor,
    max_payload_bytes: int | None = None,
) -> None:
    """Validate command parameters against one advertised tool descriptor.

    Args:
        tool_id: Protocol tool identifier from the command request.
        parameters: JSON object passed to the runtime adapter.
        descriptor: Registered descriptor for the target tool.
        max_payload_bytes: Optional envelope/controller payload byte limit.

    Raises:
        ValueError: If the command targets another tool, exceeds payload
            budgets, or violates the descriptor's lightweight JSON schema.
        ToolParameterValidationError: If parameters violate the descriptor's
            lightweight JSON schema.
    """

    if descriptor.tool_id != tool_id:
        raise ValueError(f"command tool_id does not match descriptor: {tool_id}")
    payload_bytes = len(json.dumps(parameters, ensure_ascii=False).encode("utf-8"))
    if max_payload_bytes is not None and payload_bytes > max_payload_bytes:
        raise ValueError("command payload exceeds controller max_envelope_bytes")
    if payload_bytes > descriptor.max_payload_bytes:
        raise ValueError("command payload exceeds tool max_payload_bytes")
    try:
        _validate_schema(parameters, descriptor.parameters_schema)
    except ValueError as exc:
        raise ToolParameterValidationError(str(exc)) from exc


def _validate_schema(parameters: JsonObject, schema: JsonObject) -> None:
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    _validate_value(parameters, schema, path="parameters", defs=defs)


def _validate_value(value: object, schema: JsonObject, *, path: str, defs: object) -> None:
    schema = _resolve_schema_ref(schema, defs=defs)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        _validate_any_of(value, any_of, path=path, defs=defs)

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        match_count = 0
        matched_indices: list[int] = []
        errors: list[str] = []
        for index, option in enumerate(one_of):
            if not isinstance(option, dict):
                continue
            try:
                _validate_value(value, option, path=path, defs=defs)
                match_count += 1
                matched_indices.append(index)
            except ValueError as exc:
                errors.append(str(exc))
        if match_count != 1:
            match_detail = f" matched_indices={matched_indices}" if matched_indices else ""
            raise ValueError(
                f"command parameter {path!r} must match exactly one schema; "
                f"matched {match_count}{match_detail}: {'; '.join(errors)}"
            )

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for option in all_of:
            if isinstance(option, dict):
                _validate_value(value, option, path=path, defs=defs)

    expected_type = schema.get("type")
    if expected_type is not None and not _json_type_matches_any(value, expected_type):
        raise ValueError(f"command parameter {path!r} does not match schema type {expected_type!r}")

    expected_enum = schema.get("enum")
    if isinstance(expected_enum, list) and value not in expected_enum:
        raise ValueError(f"command parameter {path!r} is not in the allowed enum values")

    if isinstance(value, dict):
        _validate_object(value, schema, path=path, defs=defs)
        return
    if isinstance(value, list):
        _validate_array(value, schema, path=path, defs=defs)
        return
    _validate_scalar_constraints(value, schema, path=path)


def _validate_any_of(value: object, options: list[object], *, path: str, defs: object) -> None:
    errors: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        try:
            _validate_value(value, option, path=path, defs=defs)
            return
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(f"command parameter {path!r} does not match any allowed schema: {'; '.join(errors)}")


def _validate_object(value: dict[object, object], schema: JsonObject, *, path: str, defs: object) -> None:
    required = schema.get("required")
    if isinstance(required, list):
        missing = [str(name) for name in required if str(name) not in value]
        if missing:
            raise ValueError(f"command missing required parameters at {path}: {', '.join(missing)}")
    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, dict):
        for field, dependencies in dependent_required.items():
            if str(field) not in value or not isinstance(dependencies, list):
                continue
            missing = [str(name) for name in dependencies if str(name) not in value]
            if missing:
                raise ValueError(
                    f"command missing dependent parameters at {path} when {field!r} is present: {', '.join(missing)}"
                )
    properties = schema.get("properties")
    if isinstance(schema.get("additionalProperties"), bool) and not schema["additionalProperties"]:
        allowed = set(properties) if isinstance(properties, dict) else set()
        extra = sorted(str(name) for name in set(value).difference(allowed))
        if extra:
            raise ValueError(f"command has unexpected parameters at {path}: {', '.join(extra)}")
    if not isinstance(properties, dict):
        return
    for name, spec in properties.items():
        if name not in value or not isinstance(spec, dict):
            continue
        _validate_value(value[name], spec, path=f"{path}.{name}", defs=defs)
    _validate_object_cross_field_constraints(value, properties, path=path)


def _validate_array(value: list[object], schema: JsonObject, *, path: str, defs: object) -> None:
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        raise ValueError(f"command parameter {path!r} must contain at least {min_items} item(s)")
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        raise ValueError(f"command parameter {path!r} must contain at most {max_items} item(s)")
    items = schema.get("items")
    if not isinstance(items, dict):
        return
    for index, item in enumerate(value):
        _validate_value(item, items, path=f"{path}[{index}]", defs=defs)


def _validate_object_cross_field_constraints(
    value: dict[object, object],
    properties: dict[object, object],
    *,
    path: str,
) -> None:
    for name, spec in properties.items():
        if not isinstance(name, str) or not isinstance(spec, dict) or name not in value:
            continue
        greater_than = spec.get("x-northcuaGreaterThan")
        if not isinstance(greater_than, str) or greater_than not in value:
            continue
        left = value[name]
        right = value[greater_than]
        if (
            isinstance(left, int | float)
            and not isinstance(left, bool)
            and isinstance(right, int | float)
            and not isinstance(right, bool)
            and left <= right
        ):
            raise ValueError(f"command parameter {path}.{name!s} must be > {path}.{greater_than}")


def _validate_scalar_constraints(value: object, schema: JsonObject, *, path: str) -> None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, int | float) and value < minimum:
            raise ValueError(f"command parameter {path!r} must be >= {minimum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, int | float) and value <= exclusive_minimum:
            raise ValueError(f"command parameter {path!r} must be > {exclusive_minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, int | float) and value > maximum:
            raise ValueError(f"command parameter {path!r} must be <= {maximum}")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(exclusive_maximum, int | float) and value >= exclusive_maximum:
            raise ValueError(f"command parameter {path!r} must be < {exclusive_maximum}")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ValueError(f"command parameter {path!r} must be at least {min_length} character(s)")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            raise ValueError(f"command parameter {path!r} must be at most {max_length} character(s)")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ValueError(f"command parameter {path!r} does not match required pattern")


def _json_type_matches_any(value: object, expected_type: object) -> bool:
    if isinstance(expected_type, list):
        return any(isinstance(item, str) and _json_type_matches(value, item) for item in expected_type)
    if isinstance(expected_type, str):
        return _json_type_matches(value, expected_type)
    return True


def _json_type_matches(value: object, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "null":
        return value is None
    return True


def _resolve_schema_ref(schema: JsonObject, *, defs: object) -> JsonObject:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/") or not isinstance(defs, dict):
        return schema
    resolved = defs.get(ref.removeprefix("#/$defs/"))
    if not isinstance(resolved, dict):
        return schema
    merged = dict(resolved)
    for key, value in schema.items():
        if key != "$ref":
            merged[str(key)] = value
    return merged
