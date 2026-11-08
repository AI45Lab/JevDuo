"""Payload, image, and ref extraction helpers for Web trace rows."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import JsonObject

PREVIEW_LIMIT = 240
MAX_TABLE_ROWS = 300
TRACE_FILE_NAME = "llm_trace.jsonl"
DATA_IMAGE_RE = re.compile(r"^data:image/(?P<mime>[^;]+);base64,(?P<data>.+)$", re.S)
ROLE_STYLES = {
    "system": ("System", "#f4f4f5"),
    "user": ("User", "#eef6ff"),
    "assistant": ("Assistant", "#f2f7f2"),
    "tool": ("Tool", "#fff7ed"),
}
IMAGE_VALUE_KEYS = frozenset(
    {
        "image",
        "image_url",
        "url",
        "path",
        "file_path",
        "screenshot_url",
        "data_url",
        "raw_image_url",
        "action_overlay_image_url",
        "overlay_image_url",
    }
)
IMAGE_PATH_VALUE_KEYS = (
    "screenshot_path",
    "raw_image_path",
    "action_overlay_image_path",
    "som_image_path",
    "selected_image_path",
    "source_screenshot_path",
    "overlay_screenshot_path",
    "post_action_image_path",
    "current_screenshot_path",
    "clean_screenshot_path",
)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
LOCAL_PATH_RE = re.compile(r"(/Users/|/tmp/|/var/folders/|[A-Za-z]:\\\\)")
PATH_KEY_RE = re.compile(r'"[^"]*path[^"]*"\s*:', re.I)
BBOX_KEY_RE = re.compile(r'"(?:bbox|bbox_css|bbox_image|bounds|geometry)"\s*:', re.I)


@dataclass(frozen=True, slots=True)
class ViewerImageRef:
    """One image reference with a best-effort semantic role for display."""

    raw: str
    role: str = "image"


def compact_text(value: Any, limit: int = PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def redacted_json(value: Any) -> str:
    return safe_json(redacted_for_json(value))


def normalize_trace(row: JsonObject) -> JsonObject:
    trace = row.get("llm_trace")
    if isinstance(trace, dict):
        return trace
    return row


def trace_request(trace: JsonObject) -> Any:
    return trace.get("request") if isinstance(trace, dict) else None


def trace_response(trace: JsonObject) -> Any:
    return trace.get("response") if isinstance(trace, dict) else None


def request_instructions(request: Any) -> str:
    if not isinstance(request, dict):
        return ""
    for key in ("instructions", "system", "system_prompt"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def request_messages(request: Any) -> list[Any]:
    if isinstance(request, dict):
        messages = request.get("messages")
        if isinstance(messages, list):
            return messages
        inputs = request.get("input")
        if isinstance(inputs, list):
            return inputs
        if isinstance(inputs, dict):
            return [inputs]
        return []
    if isinstance(request, list):
        return request
    return []


def request_tools(request: Any) -> list[Any]:
    if isinstance(request, dict) and isinstance(request.get("tools"), list):
        return request["tools"]
    return []


def response_items(response: Any) -> list[Any]:
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            return output
        choices = response.get("choices")
        if isinstance(choices, list):
            return choices
    if isinstance(response, list):
        return response
    return []


def content_blocks(content: Any) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, list):
        return content
    return [content]


def text_from_content(content: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(content, str):
        if content.strip():
            texts.append(content)
        return texts
    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
        nested = content.get("content")
        if isinstance(nested, list):
            texts.extend(text_from_content(nested))
        summary = content.get("summary")
        if isinstance(summary, list):
            texts.extend(text_from_content(summary))
        output = content.get("output")
        if isinstance(output, list):
            texts.extend(text_from_content(output))
        return texts
    if isinstance(content, list):
        for item in content:
            texts.extend(text_from_content(item))
    return texts


def image_ref_items_from_content(content: Any) -> list[ViewerImageRef]:
    refs: list[ViewerImageRef] = []

    def add_if_image_ref(raw: Any, *, allow_path: bool = False, role: str = "image") -> None:
        if not isinstance(raw, str) or not raw:
            return
        if raw.startswith("data:image/"):
            refs.append(ViewerImageRef(raw=raw, role=role))
            return
        if allow_path and _looks_like_image_path(raw):
            refs.append(ViewerImageRef(raw=raw, role=role))

    def visit(value: Any, *, image_context: bool = False) -> None:
        if isinstance(value, str):
            add_if_image_ref(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, image_context=image_context)
            return
        if not isinstance(value, dict):
            return
        item_type = str(value.get("type") or "")
        local_image_context = (
            image_context or item_type in {"image", "image_url", "input_image"} or "image_url" in value
        )
        if local_image_context:
            image_url = value.get("image_url")
            if isinstance(image_url, str) and image_url:
                add_if_image_ref(image_url, allow_path=True, role=_image_role_for_key("image_url"))
            elif isinstance(image_url, dict):
                raw = image_url.get("url") or image_url.get("path")
                add_if_image_ref(raw, allow_path=True, role=_image_role_for_key("image_url"))
            for key in IMAGE_VALUE_KEYS:
                add_if_image_ref(value.get(key), allow_path=True, role=_image_role_for_key(key))
        else:
            for key in IMAGE_VALUE_KEYS:
                add_if_image_ref(value.get(key), role=_image_role_for_key(key))
        for key in IMAGE_PATH_VALUE_KEYS:
            add_if_image_ref(value.get(key), allow_path=True, role=_image_role_for_key(key))
        for key in ("content", "input", "output", "result", "observation"):
            visit(value.get(key), image_context=local_image_context)

    visit(content)
    deduped: dict[tuple[str, str], ViewerImageRef] = {}
    for ref in refs:
        deduped.setdefault((ref.raw, ref.role), ref)
    return list(deduped.values())


def image_refs_from_content(content: Any) -> list[str]:
    return [item.raw for item in image_ref_items_from_content(content)]


def _image_role_for_key(key: object) -> str:
    text = str(key or "").lower()
    if "action_overlay" in text:
        return "action_overlay"
    if "som" in text or "overlay_screenshot" in text or text == "overlay_image_url":
        return "som"
    if "raw_image" in text or "source_screenshot" in text or "clean_screenshot" in text:
        return "clean"
    return "image"


def _looks_like_image_path(value: str) -> bool:
    try:
        path = Path(value)
    except (OSError, ValueError):
        return False
    return path.suffix.lower() in IMAGE_SUFFIXES


def request_image_refs(request: Any) -> list[str]:
    refs: list[str] = []
    for message in request_messages(request):
        if isinstance(message, dict):
            refs.extend(image_refs_from_content(message))
    return list(dict.fromkeys(refs))


def output_image_refs(value: Any) -> list[str]:
    return image_refs_from_content(value)


def output_image_ref_items(value: Any) -> list[ViewerImageRef]:
    return image_ref_items_from_content(value)


def image_count_from_raw_json(raw_text: str) -> int:
    """Return a cheap image count for trace table metadata.

    Evaluator traces intentionally preserve raw base64 image blocks for Qwen
    conversion parity. Recursively walking every request/message to count images
    makes source switching painfully slow, so the table index uses a raw-string
    count and the selected row still does the precise lazy extraction.
    """

    return raw_text.lower().count("data:image/")


def trace_noise_metrics(row: JsonObject) -> JsonObject:
    """Return cheap noise metrics for the selected trace row."""

    from .trace_calls import (
        function_call_arguments,
        function_calls_from_trace,
        model_visible_tool_output,
        tool_executions,
    )

    raw = json.dumps(row, ensure_ascii=False)
    trace = normalize_trace(row)
    executions = tool_executions(trace, row)
    model_visible_outputs = []
    tool_output_chars = 0
    for execution in executions:
        output = model_visible_tool_output(execution)
        model_visible_outputs.append(output)
        try:
            tool_output_chars += len(json.dumps(output, ensure_ascii=False))
        except TypeError:
            tool_output_chars += len(str(output))
    model_visible = json.dumps(
        {"request": trace_request(trace), "tool_outputs": model_visible_outputs},
        ensure_ascii=False,
    )
    calls = function_calls_from_trace(trace, trace_response(trace))
    full_raw_calls = 0
    for call in calls:
        args = function_call_arguments(call)
        args_text = json.dumps(args, ensure_ascii=False).lower() if not isinstance(args, str) else args.lower()
        if "detail_level" in args_text and "debug" in args_text:
            full_raw_calls += 1
        if (
            '"all"' in args_text
            or '"full"' in args_text
            or "section" in args_text
            and ("all" in args_text or "full" in args_text)
        ):
            full_raw_calls += 1
    return {
        "request_chars": len(json.dumps(trace_request(trace), ensure_ascii=False)),
        "image_count": image_count_from_raw_json(model_visible),
        "bbox_key_count": len(BBOX_KEY_RE.findall(model_visible)),
        "path_key_count": len(PATH_KEY_RE.findall(model_visible)),
        "local_path_count": len(LOCAL_PATH_RE.findall(model_visible)),
        "tool_output_chars": tool_output_chars,
        "full_or_debug_tool_calls": full_raw_calls,
        "raw_image_count": image_count_from_raw_json(raw),
        "raw_bbox_key_count": len(BBOX_KEY_RE.findall(raw)),
        "raw_path_key_count": len(PATH_KEY_RE.findall(raw)),
        "raw_local_path_count": len(LOCAL_PATH_RE.findall(raw)),
    }


def index_text_from_content(content: Any, *, budget: int = 4000) -> str:
    """Extract searchable text without traversing large image payloads."""

    pieces: list[str] = []
    remaining = budget

    def add(text: str) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        if text.startswith("data:image/"):
            return
        text = compact_text(text, remaining)
        if not text:
            return
        pieces.append(text)
        remaining -= len(text)

    def visit(value: Any) -> None:
        if remaining <= 0:
            return
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
                if remaining <= 0:
                    break
            return
        if not isinstance(value, dict):
            return
        item_type = str(value.get("type") or "")
        if item_type in {"image", "image_url", "input_image"} or "image_url" in value:
            return
        for key in ("text", "content", "summary", "output", "value"):
            if key in value:
                visit(value.get(key))
                if remaining <= 0:
                    break

    visit(content)
    return " ".join(pieces)


def tool_execution_image_refs(executions: list[JsonObject]) -> list[str]:
    from .trace_calls import model_visible_tool_output, runtime_tool_output

    refs: list[str] = []
    for execution in executions:
        refs.extend(output_image_refs(model_visible_tool_output(execution)))
        refs.extend(output_image_refs(runtime_tool_output(execution)))
    return list(dict.fromkeys(refs))


def json_object_from_text(text: str) -> JsonObject | None:
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def parsed_function_call_output_payload(message: JsonObject) -> JsonObject | None:
    if message.get("type") != "function_call_output":
        return None
    for text in text_from_content(message.get("output")):
        payload = json_object_from_text(text)
        if payload is None:
            continue
        if any(key in payload for key in ("status", "text", "action", "observation", "screenshot_url")):
            return payload
    return None


def observation_payload_from_tool_output(output: Any) -> JsonObject | None:
    if not isinstance(output, dict):
        return None
    observation = output.get("observation")
    return observation if isinstance(observation, dict) else None


def refs_marks_from_observation(observation: JsonObject) -> tuple[list[JsonObject], list[JsonObject], JsonObject]:
    snapshot = observation.get("snapshot")
    refs = snapshot.get("refs") if isinstance(snapshot, dict) else []
    screenshot = observation.get("screenshot")
    annotation = screenshot.get("annotation") if isinstance(screenshot, dict) else {}
    marks = annotation.get("marks") if isinstance(annotation, dict) else []
    return (
        [item for item in refs if isinstance(item, dict)] if isinstance(refs, list) else [],
        [item for item in marks if isinstance(item, dict)] if isinstance(marks, list) else [],
        annotation if isinstance(annotation, dict) else {},
    )


def refs_marks_match(refs: list[JsonObject], marks: list[JsonObject]) -> bool:
    return [str(item.get("ref") or "") for item in refs] == [str(item.get("ref") or "") for item in marks] and [
        str(item.get("label") or "") for item in marks
    ] == [str(item.get("ref") or "") for item in refs]


def tool_output_summary(output: Any) -> JsonObject:
    if not isinstance(output, dict):
        return {}
    summary = {
        key: output.get(key)
        for key in (
            "status",
            "text",
            "action",
            "params",
            "result",
            "observation_error",
            "observation_recovery",
        )
        if key in output
    }
    observation = output.get("observation")
    if isinstance(observation, dict):
        raw_page = observation.get("page")
        page = raw_page if isinstance(raw_page, dict) else {}
        summary["observation"] = {
            "url": page.get("url"),
            "title": page.get("title"),
            "settled": observation.get("settled"),
            "settle_reason": observation.get("settle_reason"),
        }
    return summary


def refs_marks_status(output: Any) -> str:
    observation = observation_payload_from_tool_output(output)
    if observation is None:
        return ""
    refs, marks, _annotation = refs_marks_from_observation(observation)
    if not refs and not marks:
        return "no refs/marks"
    state = "ok" if refs_marks_match(refs, marks) else "mismatch"
    return f"{state}: {len(refs)} refs / {len(marks)} marks"


def has_refs_or_marks(output: Any) -> bool:
    """Return true when a tool result carries model-visible refs or marks."""

    observation = observation_payload_from_tool_output(output)
    if observation is None:
        return False
    refs, marks, _annotation = refs_marks_from_observation(observation)
    return bool(refs or marks)


def redacted_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, child in value.items():
            if key in IMAGE_VALUE_KEYS and isinstance(child, str) and child.startswith("data:image/"):
                out[key] = f"[image data omitted; chars={len(child)}]"
            else:
                out[key] = redacted_for_json(child)
        return out
    if isinstance(value, list):
        return [redacted_for_json(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"[image data omitted; chars={len(value)}]"
    return value
