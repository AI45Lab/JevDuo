"""Tool-call and message normalization helpers for Web trace rows."""

from __future__ import annotations

import json
from typing import Any

from northcua.agents.web.prompt.history import WebHistoryToolResultProjector

from .domain import JsonObject
from .trace_payloads import (
    index_text_from_content,
    output_image_refs,
    request_instructions,
    request_messages,
    response_items,
    text_from_content,
)


def response_text(response: Any) -> str:
    texts: list[str] = []
    for item in response_items(response):
        if isinstance(item, dict):
            texts.extend(text_from_content(item.get("content")))
            message = item.get("message")
            if isinstance(message, dict):
                texts.extend(text_from_content(message.get("content")))
    if not texts and isinstance(response, dict):
        text_payload = response.get("text")
        if isinstance(text_payload, dict):
            texts.extend(text_from_content(text_payload))
        elif isinstance(text_payload, str):
            texts.append(text_payload)
    return "\n".join(texts)


def reasoning_text(trace: JsonObject, response: Any) -> str:
    direct = trace.get("reasoning") or trace.get("thinking")
    if isinstance(direct, str) and direct.strip():
        return direct
    texts: list[str] = []
    for item in response_items(response):
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        texts.extend(text_from_content(item.get("summary")))
        texts.extend(text_from_content(item.get("content")))
    return "\n".join(texts)


def tool_name_from_definition(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    if isinstance(tool.get("name"), str):
        return tool["name"]
    return ""


def _normalized_call_from_tool_call(record: JsonObject) -> JsonObject:
    return {
        "type": "function_call",
        "call_id": record.get("id") or record.get("call_id") or record.get("tool_call_id"),
        "name": record.get("name") or record.get("tool_name"),
        "arguments": record.get("arguments") or record.get("tool_input") or record.get("input"),
        "source": "assistant_tool_calls",
    }


def function_calls_from_trace(trace: JsonObject, response: Any) -> list[JsonObject]:
    calls: list[JsonObject] = []
    seen: set[str] = set()

    def add_call(call: JsonObject) -> None:
        call_id = function_call_id(call)
        if call_id and call_id in seen:
            return
        if call_id:
            seen.add(call_id)
        calls.append(call)

    raw_trace_calls = trace.get("assistant_tool_calls")
    if isinstance(raw_trace_calls, list):
        for call in raw_trace_calls:
            if isinstance(call, dict):
                add_call(_normalized_call_from_tool_call(call))
    for item in response_items(response):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            add_call(item)
        message = item.get("message")
        if isinstance(message, dict):
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if isinstance(call, dict):
                        add_call(call)
    return calls


def function_calls_from_response(response: Any) -> list[JsonObject]:
    return function_calls_from_trace({}, response)


def function_call_id(call: JsonObject) -> str:
    value = call.get("call_id") or call.get("id")
    if isinstance(value, str):
        return value
    return ""


def function_call_name(call: JsonObject) -> str:
    name = call.get("name")
    if isinstance(name, str):
        return name
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def function_call_arguments(call: JsonObject) -> Any:
    raw = call.get("arguments")
    if raw is None and isinstance(call.get("function"), dict):
        raw = call["function"].get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def display_tool_call(call: JsonObject) -> JsonObject:
    return {
        key: value
        for key, value in {
            "type": call.get("type") or "function_call",
            "call_id": function_call_id(call) or None,
            "name": function_call_name(call) or None,
            "arguments": function_call_arguments(call),
            "status": call.get("status"),
        }.items()
        if value is not None
    }


def tool_executions(trace: JsonObject, row: JsonObject) -> list[JsonObject]:
    values: list[JsonObject] = []
    for source in (trace.get("tool_executions"), row.get("tool_executions")):
        if isinstance(source, list):
            values.extend(item for item in source if isinstance(item, dict))
    return values


def tool_execution_call_id(execution: JsonObject) -> str:
    value = execution.get("tool_call_id") or execution.get("call_id") or execution.get("id")
    if isinstance(value, str):
        return value
    return ""


def tool_execution_name(execution: JsonObject) -> str:
    value = execution.get("tool_name") or execution.get("name")
    if isinstance(value, str):
        return value
    return ""


def model_visible_tool_output(execution: JsonObject) -> Any:
    for key in ("llm_tool_output", "model_visible_output", "tool_output", "output"):
        if key in execution and execution.get(key) is not None:
            return execution.get(key)
    return execution


def runtime_tool_output(execution: JsonObject) -> Any:
    for key in ("tool_output", "output"):
        if key in execution and execution.get(key) is not None:
            return execution.get(key)
    return execution


def payloads_equal(left: Any, right: Any) -> bool:
    return left == right


def display_messages(request: Any, response: Any) -> list[JsonObject]:
    messages: list[JsonObject] = []
    instructions = request_instructions(request)
    if instructions:
        messages.append({"role": "system", "content": instructions})
    for item in request_messages(request):
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        item_type = str(item.get("type") or "")
        role = item.get("role")
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output"),
                }
            )
            continue
        if item_type == "function_call":
            messages.append({"role": "assistant", "content": [], "tool_calls": [display_tool_call(item)]})
            continue
        if item_type == "reasoning":
            reasoning = "\n".join(text_from_content(item.get("summary") or item.get("content")))
            messages.append({"role": "assistant", "content": [], "reasoning_content": reasoning})
            continue
        if isinstance(role, str):
            messages.append({"role": role, "content": item.get("content", item)})
            continue
        messages.append({"role": "user", "content": item})
    for item in response_items(response):
        if not isinstance(item, dict):
            messages.append({"role": "assistant", "content": item})
            continue
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            reasoning = "\n".join(text_from_content(item.get("summary") or item.get("content")))
            messages.append({"role": "assistant", "content": [], "reasoning_content": reasoning})
            continue
        if item_type == "function_call":
            messages.append({"role": "assistant", "content": [], "tool_calls": [display_tool_call(item)]})
            continue
        if item_type == "message":
            messages.append({"role": str(item.get("role") or "assistant"), "content": item.get("content", item)})
            continue
        messages.append({"role": "assistant", "content": item})
    return messages
