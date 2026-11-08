"""Helpers for normalizing FastMCP tool results into NorthCUA runtime payloads."""

from __future__ import annotations

import base64
import os

from fastmcp.tools.base import ToolResult

from northcua.runtime.mcp.types import MCPToolResultPayload


def normalize_tool_result(tool_result: object) -> MCPToolResultPayload | str:
    """Convert a FastMCP result into a websocket/persistence-friendly payload.

    Args:
        tool_result: Raw result returned by FastMCP or a direct in-memory tool.

    Returns:
        A JSON-style dictionary or plain string. FastMCP structured content of
        the form ``{"result": ...}`` is unwrapped for compatibility with
        existing data-URL and action-result callers. Dictionary payload keys are
        copied without renaming or filtering.
    """

    if isinstance(tool_result, ToolResult):
        if tool_result.structured_content is not None:
            structured = tool_result.structured_content
            structured_payload = _dict_payload(structured)
            if structured_payload is not None and set(structured_payload.keys()) == {"result"}:
                unwrapped = structured_payload["result"]
                unwrapped_payload = _dict_payload(unwrapped)
                if unwrapped_payload is not None:
                    return unwrapped_payload
                return "" if unwrapped is None else str(unwrapped)
            return structured_payload if structured_payload is not None else str(structured)
        text_parts: list[str] = []
        for block in tool_result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        return "\n".join(text_parts)
    payload = _dict_payload(tool_result)
    if payload is not None:
        return payload
    return "" if tool_result is None else str(tool_result)


def _dict_payload(value: object) -> MCPToolResultPayload | None:
    """Return a copy of a dict payload without changing its runtime shape.

    Args:
        value: Candidate JSON-style dictionary.

    Returns:
        A shallow copy when ``value`` is a dictionary, otherwise ``None``.
    """

    if not isinstance(value, dict):
        return None
    return dict(value)


def encode_image_path_to_data_url(path: str, *, mime_type: str = "image/png") -> str:
    """Convert a local image path into a data URL for multimodal resume/prompt use.

    Args:
        path: Local image path to read.
        mime_type: MIME type to embed in the returned data URL.

    Returns:
        Base64 data URL, or an empty string if ``path`` is missing or does not
        exist.

    Raises:
        OSError: If the file exists but cannot be read.
    """

    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
