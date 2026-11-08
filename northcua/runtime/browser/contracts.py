"""Canonical contracts for the Playwright browser MCP surface."""

from __future__ import annotations

from northcua_protocol.capabilities import Capability, JsonObject, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import (
    McpBinding,
    ProviderSchemaMode,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
)

PLAYWRIGHT_BROWSER_SERVER_NAME = "PlaywrightBrowserExecutor"


def _object_schema(properties: JsonObject, *, required: tuple[str, ...] = ()) -> JsonObject:
    schema: JsonObject = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _nullable(schema: JsonObject, *, default: object = None) -> JsonObject:
    result: JsonObject = {
        "anyOf": [schema, {"type": "null"}],
        "default": default,
    }
    return result


def _nullable_string(description: str) -> JsonObject:
    schema = _nullable({"type": "string"})
    schema["description"] = description
    return schema


def _present_string_property(name: str) -> JsonObject:
    return {"properties": {name: {"type": "string", "minLength": 1}}, "required": [name]}


def _requires_ref_or_selector() -> JsonObject:
    return {"anyOf": [_present_string_property("ref"), _present_string_property("selector")]}


def _browser_tabs_schema() -> JsonObject:
    schema = _object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["list", "new", "close", "select"],
                "description": "One of `list`, `new`, `close`, or `select`.",
            },
            "index": _nullable({"type": "integer"}, default=None)
            | {"description": "Tab index for `close` or `select`."},
        },
        required=("action",),
    )
    schema["oneOf"] = [
        {"properties": {"action": {"enum": ["list", "new", "close"]}}},
        {
            "properties": {
                "action": {"enum": ["select"]},
                "index": {"type": "integer"},
            },
            "required": ["index"],
        },
    ]
    return schema


def _target_schema(properties: JsonObject, *, required: tuple[str, ...] = ()) -> JsonObject:
    schema = _object_schema(properties, required=required)
    schema.update(_requires_ref_or_selector())
    return schema


def _drag_schema() -> JsonObject:
    schema = _object_schema(
        {
            "startElement": {"type": "string", "description": "Source element description."},
            "endElement": {"type": "string", "description": "Target element description."},
            "startRef": _nullable_string("Source snapshot ref."),
            "endRef": _nullable_string("Target snapshot ref."),
            "startSelector": _nullable_string("Fallback source selector."),
            "endSelector": _nullable_string("Fallback target selector."),
        },
        required=("startElement", "endElement"),
    )
    schema["anyOf"] = [
        {
            "properties": {
                "startRef": {"type": "string", "minLength": 1},
                "endRef": {"type": "string", "minLength": 1},
            },
            "required": ["startRef", "endRef"],
        },
        {
            "properties": {
                "startRef": {"type": "string", "minLength": 1},
                "endSelector": {"type": "string", "minLength": 1},
            },
            "required": ["startRef", "endSelector"],
        },
        {
            "properties": {
                "startSelector": {"type": "string", "minLength": 1},
                "endRef": {"type": "string", "minLength": 1},
            },
            "required": ["startSelector", "endRef"],
        },
        {
            "properties": {
                "startSelector": {"type": "string", "minLength": 1},
                "endSelector": {"type": "string", "minLength": 1},
            },
            "required": ["startSelector", "endSelector"],
        },
    ]
    return schema


def _contract(
    tool_id: str,
    *,
    description: str,
    json_schema: JsonObject,
    kind: ToolContractKind = ToolContractKind.ACTION,
    side_effect: bool = True,
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
) -> ToolContract:
    return ToolContract(
        tool_id=tool_id,
        description=description,
        namespace="browser.playwright",
        agent_kind="browser",
        kind=kind,
        audience=ToolAudience.AGENT,
        placement=ToolPlacement.MCP_SERVER,
        capability=Capability.BROWSER_PAGE,
        json_schema=json_schema,
        provider_schema_mode=(
            ProviderSchemaMode.UNION
            if "oneOf" in json_schema or "anyOf" in json_schema
            else ProviderSchemaMode.COMPATIBLE
        ),
        timeout_seconds=10.0,
        side_effect=side_effect,
        readonly=not side_effect,
        risk_level=risk_level,
        binding=McpBinding(server_name=PLAYWRIGHT_BROWSER_SERVER_NAME),
        source="northcua.runtime.browser.mcp_tools",
        profiles=("agent.browser.default",),
    )


PLAYWRIGHT_BROWSER_TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    _contract(
        "browser_tabs",
        description=(
            "List, create, close, or select a browser tab. Returns a markdown tab list "
            "showing tab indexes, the active tab marker, titles, and URLs."
        ),
        json_schema=_browser_tabs_schema(),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_navigate",
        description="Navigate the current tab to a URL and return a plain-text confirmation with the final URL.",
        json_schema=_object_schema({"url": {"type": "string", "description": "Destination URL."}}, required=("url",)),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_navigate_back",
        description="Go back to the previous page in the current tab history.",
        json_schema=_object_schema({}),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_navigate_forward",
        description="Go forward to the next page in the current tab history.",
        json_schema=_object_schema({}),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_reload",
        description="Reload the current page in the active tab.",
        json_schema=_object_schema({}),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_snapshot",
        description=(
            "Capture a semantic accessibility-style snapshot of the current page and refresh the "
            "element refs used by later browser tools."
        ),
        json_schema=_object_schema(
            {
                "selector": _nullable_string("Optional root selector to snapshot."),
                "depth": _nullable({"type": "integer"}, default=None)
                | {"description": "Optional maximum traversal depth."},
            }
        ),
        kind=ToolContractKind.PROBE,
        side_effect=False,
    ),
    _contract(
        "browser_click",
        description=(
            "Click or double-click an element on the current page. Prefer using `ref` values from "
            "the latest `browser_snapshot`; if no ref is available, you may use `selector` instead."
        ),
        json_schema=_target_schema(
            {
                "ref": _nullable_string("Snapshot ref such as `e3`."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback CSS selector."),
                "doubleClick": _nullable({"type": "boolean"}, default=None)
                | {"description": "Set true to double-click."},
                "button": _nullable_string("Mouse button: left, right, or middle."),
                "modifiers": _nullable({"type": "array", "items": {"type": "string"}}, default=None)
                | {"description": "Optional modifier keys."},
            }
        ),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_drag",
        description="Drag from one element to another on the current page.",
        json_schema=_drag_schema(),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_hover",
        description="Hover over an element on the current page.",
        json_schema=_target_schema(
            {
                "ref": _nullable_string("Snapshot ref such as `e4`."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback selector."),
            }
        ),
        risk_level=ToolRiskLevel.LOW,
    ),
    _contract(
        "browser_select_option",
        description="Select one or more option values in a dropdown element.",
        json_schema=_target_schema(
            {
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Values to select.",
                },
                "ref": _nullable_string("Snapshot ref."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback selector."),
            },
            required=("values",),
        ),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_generate_locator",
        description="Resolve an element ref or selector into a concrete CSS locator string.",
        json_schema=_target_schema(
            {
                "ref": _nullable_string("Snapshot ref."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback selector."),
            }
        ),
        kind=ToolContractKind.PROBE,
        side_effect=False,
    ),
    _contract(
        "browser_check",
        description="Check a checkbox or radio button.",
        json_schema=_target_schema(
            {
                "ref": _nullable_string("Snapshot ref."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback selector."),
            }
        ),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
    _contract(
        "browser_uncheck",
        description="Uncheck a checkbox or radio button.",
        json_schema=_target_schema(
            {
                "ref": _nullable_string("Snapshot ref."),
                "element": _nullable_string("Human-readable element description."),
                "selector": _nullable_string("Fallback selector."),
            }
        ),
        risk_level=ToolRiskLevel.MEDIUM,
    ),
)


__all__ = ["PLAYWRIGHT_BROWSER_SERVER_NAME", "PLAYWRIGHT_BROWSER_TOOL_CONTRACTS"]
