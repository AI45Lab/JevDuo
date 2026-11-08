"""Shared helpers for converting websocket/device results into NexAU blocks."""

from __future__ import annotations

from dataclasses import dataclass, field

from nexau.core.messages import ImageBlock, TextBlock, ToolResultBlock

from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord, ToolExecutionRecord
from northcua.runtime.mcp.result_adapter import encode_image_path_to_data_url


@dataclass(frozen=True, slots=True)
class RemoteToolDispatchResult:
    """Provider tool-result blocks plus authoritative websocket command rows.

    NexAU continuation messages only need ``ToolResultBlock`` objects, while
    NorthCUA step persistence needs the raw command rows to build
    ``ToolExecutionRecord`` entries. Keeping the two streams in an explicit
    value object avoids hidden list-subclass state and makes remote/local trace
    replay use the same typed contract.

    Attributes:
        tool_blocks: Provider-facing tool-result blocks returned to NexAU.
        command_results: Raw websocket command results keyed by tool call id.
    """

    tool_blocks: list[ToolResultBlock] = field(default_factory=list)
    command_results: list[CommandExecutionResultRecord] = field(default_factory=list)


def coerce_remote_tool_dispatch_result(
    value: RemoteToolDispatchResult | list[ToolResultBlock],
) -> RemoteToolDispatchResult:
    """Normalize legacy provider-block lists into the explicit dispatch result.

    Args:
        value: Result returned by a remote dispatch callback. New code should
            return ``RemoteToolDispatchResult``; plain lists are accepted only
            as a defensive compatibility boundary for older tests/extensions.

    Returns:
        Explicit remote dispatch result.
    """

    if isinstance(value, RemoteToolDispatchResult):
        return value
    return RemoteToolDispatchResult(tool_blocks=list(value), command_results=[])


def command_result_to_tool_result_block(result: CommandExecutionResultRecord) -> ToolResultBlock:
    """Convert one command result into a NexAU ToolResultBlock.

    Args:
        result: Websocket/device command result to convert.

    Returns:
        Provider-facing NexAU tool result block preserving text/image content.
    """

    is_error = result.status == ResultStatus.FAILURE
    payload = result.result
    if is_error:
        return ToolResultBlock(
            tool_use_id=result.call_id,
            content=_command_error_text(result),
            is_error=True,
        )
    if isinstance(payload, dict):
        blocks = []
        text = payload.get("text")
        if text is not None:
            blocks.append(TextBlock(text=str(text)))
        image_url = payload.get("screenshot_url")
        if not image_url:
            image_url = encode_image_path_to_data_url(str(payload.get("screenshot_path") or ""))
        if isinstance(image_url, str) and image_url:
            blocks.append(ImageBlock(url=image_url))
        if blocks:
            return ToolResultBlock(
                tool_use_id=result.call_id,
                content=blocks,
                is_error=False,
            )
    if isinstance(payload, str) and payload.startswith("data:image/"):
        return ToolResultBlock(
            tool_use_id=result.call_id,
            content=[ImageBlock(url=payload)],
            is_error=False,
        )
    return ToolResultBlock(
        tool_use_id=result.call_id,
        content="" if payload is None else str(payload),
        is_error=False,
    )


def command_results_to_tool_execution_records(
    pending_tool_calls: list[JsonObject],
    results: list[CommandExecutionResultRecord],
) -> list[ToolExecutionRecord]:
    """Convert executed command results into ToolExecutionRecord rows.

    Args:
        pending_tool_calls: JSON-shaped model tool calls awaiting execution.
        results: Device command results keyed by call identifier.

    Returns:
        Trace rows that preserve the original tool call and command payloads.
    """

    result_by_call_id = {result.call_id: result for result in results}
    executions: list[ToolExecutionRecord] = []
    for call in pending_tool_calls:
        call_id = str(call.get("id") or "")
        if not call_id:
            continue
        result = result_by_call_id.get(call_id)
        if result is None:
            continue
        tool_output: JsonObject | str | None = result.result
        if result.status == ResultStatus.FAILURE and tool_output is None:
            tool_output = {
                "status": "error",
                "error": result.error or "Tool execution failed before returning a result.",
            }
        # Trace and provider payloads intentionally share the same raw result
        # shape here; downstream persistence relies on these field names and
        # does its own rendering-specific normalization later.
        executions.append(
            ToolExecutionRecord(
                tool_call_id=call_id,
                tool_name=str(call.get("name") or ""),
                tool_input=_json_object(call.get("input")),
                tool_output=tool_output,
                llm_tool_output=tool_output,
                is_error=result.status == ResultStatus.FAILURE,
            )
        )
    return executions


def _json_object(value: object) -> JsonObject:
    """Return ``value`` as a string-keyed JSON object when possible."""

    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _command_error_text(result: CommandExecutionResultRecord) -> str:
    """Return a prompt-safe text-only error for a failed command result."""

    payload = result.result
    if isinstance(payload, dict):
        for key in ("error", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(result.error, str) and result.error.strip():
        return result.error
    if isinstance(payload, str) and not payload.startswith("data:image/") and payload.strip():
        return payload
    return "Command failed without an error message."
