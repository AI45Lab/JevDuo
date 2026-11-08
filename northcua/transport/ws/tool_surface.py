"""Structured tool-surface helpers for websocket transport execution."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from nexau.archs.main_sub.execution.parse_structures import ParsedResponse, ToolCall

from northcua_runtime_shared.tool_contracts import tool_route_server_name


def pending_tool_calls_from_parsed_response(
    parsed_response: ParsedResponse | None,
    *,
    route_by_tool_name: Mapping[str, str] | None = None,
    allowed_tool_names: Collection[str] | None = None,
) -> list[dict[str, object]]:
    """Normalize one parsed response into externalizable pending tool calls.

    Args:
        parsed_response: NexAU parsed response that may contain structured tool calls.

    Returns:
        JSON-style pending calls with stable ``id``, ``name``, and ``input`` fields.
    """

    if parsed_response is None or not parsed_response.tool_calls:
        return []
    pending: list[dict[str, object]] = []
    routes = route_by_tool_name or {}
    for index, tool_call in enumerate(parsed_response.tool_calls):
        call = tool_call if isinstance(tool_call, ToolCall) else None
        if call is None:
            continue
        if allowed_tool_names is not None and call.tool_name not in allowed_tool_names:
            raise ValueError(f"model returned tool outside the active provider surface: {call.tool_name}")
        item: dict[str, object] = {
            "id": call.tool_call_id or f"tool_call_{index}",
            "name": call.tool_name,
            "input": dict(call.parameters),
        }
        server_name = routes.get(call.tool_name)
        if server_name:
            item["server_name"] = server_name
        pending.append(item)
    return pending


def route_by_tool_name_from_runtime_tools(tools: Iterable[object]) -> dict[str, str]:
    """Return route metadata for direct tools that carry a ToolContract server."""

    routes: dict[str, str] = {}
    seen_tool_routes: dict[str, str] = {}
    for tool in tools:
        tool_name = str(getattr(tool, "name", "") or "")
        if not tool_name:
            continue
        server_name = _tool_server_name(tool)
        existing = seen_tool_routes.get(tool_name)
        if existing is not None:
            if existing and server_name and existing != server_name:
                raise ValueError(
                    f"Conflicting direct-tool route for provider tool {tool_name!r}: {existing!r} vs {server_name!r}"
                )
            if existing and server_name:
                raise ValueError(f"Duplicate direct-tool provider name {tool_name!r} on routed server {server_name!r}")
            if existing or server_name:
                routed = existing or server_name
                raise ValueError(
                    f"Ambiguous direct-tool provider name {tool_name!r}: routed server {routed!r} "
                    "collides with an unrouted direct tool"
                )
            raise ValueError(f"Duplicate direct-tool provider name {tool_name!r}")
        seen_tool_routes[tool_name] = server_name
        if not server_name:
            continue
        routes[tool_name] = server_name
    return routes


def _tool_server_name(tool: object) -> str:
    return tool_route_server_name(tool)
