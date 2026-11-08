from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from PIL import Image, UnidentifiedImageError

from northcua.agents.web.prompt.history import WebHistoryToolResultProjector

from northcua.agents.web.trace_viewer.domain import JsonObject, TraceEntry, TraceKind, TraceSource
from northcua.agents.web.trace_viewer.trace_data import *  # noqa: F403 - compatibility re-export for tests and viewer helpers


def decode_data_image(raw: str) -> Image.Image | None:
    match = DATA_IMAGE_RE.match(raw)
    if match is None:
        return None
    try:
        data = base64.b64decode(match.group("data"), validate=False)
        with Image.open(BytesIO(data)) as image:
            return image.convert("RGBA")
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError):
        return None


def resolve_image_path(raw: str, trace_path: Path) -> Path | None:
    if raw.startswith("data:image/"):
        return None
    if raw.startswith("file://"):
        candidate = Path(raw[7:])
        if candidate.exists():
            return candidate
    candidate = Path(raw).expanduser()
    if candidate.exists():
        return candidate
    base = trace_path.parent
    for item in (base / raw, base / candidate.name, base / "screenshots" / candidate.name):
        if item.exists():
            return item
    return None


def render_image_ref(raw: str, trace_path: Path, *, caption: str = "") -> None:
    if raw.startswith("data:image/"):
        image = decode_data_image(raw)
        if image is None:
            st.warning("Could not decode inline image.")
            return
        st.image(image, caption=caption or f"inline image · {image.size[0]}x{image.size[1]}", width="stretch")
        return
    resolved = resolve_image_path(raw, trace_path)
    if resolved is None:
        st.warning(f"Image not found: {compact_text(raw, 160)}")
        return
    st.image(str(resolved), caption=caption or str(resolved), width="stretch")


def render_content(content: Any, trace_path: Path, *, image_limit: int, image_counter: list[int]) -> None:
    if content is None:
        st.caption("(empty)")
        return
    if isinstance(content, str):
        if content.startswith("data:image/"):
            if image_counter[0] < image_limit:
                image_counter[0] += 1
                render_image_ref(content, trace_path, caption=f"image {image_counter[0]}")
            else:
                st.caption("[image omitted by viewer limit]")
            return
        st.markdown(content)
        return
    if isinstance(content, list):
        for item in content:
            render_content(item, trace_path, image_limit=image_limit, image_counter=image_counter)
        return
    if not isinstance(content, dict):
        st.code(redacted_json(content), language="json")
        return
    item_type = str(content.get("type") or "")
    image_refs = image_ref_items_from_content(content)
    if item_type in {"image", "image_url", "input_image"} or image_refs:
        for item in image_refs:
            if image_counter[0] >= image_limit:
                st.caption("[image omitted by viewer limit]")
                break
            image_counter[0] += 1
            render_image_ref(
                item.raw, trace_path, caption=f"image {image_counter[0]} · {item.role or item_type or 'image'}"
            )
        return
    texts = text_from_content(content)
    if texts:
        for text in texts:
            st.markdown(text)
        return
    st.code(redacted_json(content), language="json")


def render_turn_header(role: str, index: int, extra: str = "") -> None:
    label, background = ROLE_STYLES.get(role, (role.title() or "Message", "#f8fafc"))
    suffix = f" | {extra}" if extra else ""
    st.markdown(
        f"""
        <div style="
            border: 1px solid rgba(49, 51, 63, 0.16);
            border-radius: 8px;
            padding: 10px 12px;
            margin: 10px 0 8px 0;
            background: {background};
            font-weight: 600;
        ">{index}. {label}{suffix}</div>
        """,
        unsafe_allow_html=True,
    )


def render_message_tool_calls(tool_calls: Any) -> None:
    if not isinstance(tool_calls, list) or not tool_calls:
        return
    with st.expander(f"tool_calls ({len(tool_calls)})", expanded=False):
        for idx, call in enumerate(tool_calls, 1):
            st.markdown(f"**Call {idx}**")
            if isinstance(call, dict):
                st.code(redacted_json(call), language="json")
            else:
                st.code(str(call))


def render_trace_message(message: JsonObject, index: int, trace_path: Path, image_limit: int) -> None:
    role = str(message.get("role") or "unknown")
    extra_parts: list[str] = []
    tool_call_id = message.get("tool_call_id")
    if role == "tool" and isinstance(tool_call_id, str) and tool_call_id:
        extra_parts.append(tool_call_id)
    render_turn_header(role, index, " | ".join(extra_parts))
    if role == "assistant":
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            with st.expander("reasoning_content", expanded=False):
                st.markdown(reasoning)
    if role == "tool":
        payload = parsed_function_call_output_payload(
            {"type": "function_call_output", "output": message.get("content")}
        )
        if payload is not None:
            render_tool_result_main(
                payload, trace_path, image_caption=f"tool {index}", image_source=message.get("content")
            )
        else:
            render_content(message.get("content"), trace_path, image_limit=image_limit, image_counter=[0])
        return
    render_content(message.get("content"), trace_path, image_limit=image_limit, image_counter=[0])
    if role == "assistant":
        render_message_tool_calls(message.get("tool_calls"))


def render_messages_panel(messages: list[JsonObject], trace_path: Path, image_limit: int) -> None:
    if not messages:
        st.info("No messages in this row.")
        return
    for idx, message in enumerate(messages, 1):
        render_trace_message(message, idx, trace_path, image_limit)


def message_rows(messages: list[JsonObject]) -> pd.DataFrame:
    rows: list[JsonObject] = []
    for idx, message in enumerate(messages, 1):
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        rows.append(
            {
                "idx": idx,
                "role": message.get("role", ""),
                "text": compact_text("\n".join(text_from_content(content))),
                "images": len(output_image_refs(content)),
                "tool_calls": len(tool_calls) if isinstance(tool_calls, list) else 0,
                "reasoning": bool(message.get("reasoning_content")),
            }
        )
    return pd.DataFrame(rows)


def is_user_observation_message(message: JsonObject) -> bool:
    if message.get("role") != "user":
        return False
    text = "\n".join(text_from_content(message.get("content")))
    return (
        "Current page:" in text
        or "Initial page observation:" in text
        or "Latest page observation:" in text
        or "Post-action page observation:" in text
        or "Page observation:" in text
        or "Visible marked refs (" in text
        or "Snapshot refs (" in text
        or "Current actionable refs (" in text
        or "Current refs (" in text
        or "Actionable refs (" in text
        or "Observation refs (" in text
    )


def request_message_label(message: JsonObject, idx: int) -> str:
    del idx
    item_type = str(message.get("type") or "")
    role = str(message.get("role") or "")
    if item_type == "function_call_output":
        call_id = message.get("call_id") if isinstance(message.get("call_id"), str) else ""
        return f"history function_call_output · {call_id or 'no-call-id'}"
    if item_type == "function_call":
        name = function_call_name(message)
        call_id = function_call_id(message)
        suffix = " · ".join(part for part in (name, call_id) if part)
        return f"history function_call{f' · {suffix}' if suffix else ''}"
    if role == "assistant" or item_type == "message" and role == "assistant":
        return "assistant message"
    if role == "user" and is_user_observation_message(message):
        return "user observation"
    if role == "user":
        return "user message"
    return role or item_type or "input"


def render_function_call_output_message(
    message: JsonObject, trace_path: Path, image_limit: int, image_counter: list[int]
) -> None:
    call_id = message.get("call_id")
    if isinstance(call_id, str) and call_id:
        st.caption(f"call_id: {call_id}")
    payload = parsed_function_call_output_payload(message)
    if payload is not None:
        st.caption("Parsed model-visible tool result")
        if isinstance(payload.get("text"), str):
            st.markdown(payload["text"])
        images = output_image_ref_items(message)
        if images:
            st.markdown("**function_call_output image(s)**")
            image_cols = st.columns(min(2, len(images)))
            for image_index, item in enumerate(images, 1):
                with image_cols[(image_index - 1) % len(image_cols)]:
                    if image_counter[0] >= image_limit:
                        st.caption("[image omitted by viewer limit]")
                        break
                    image_counter[0] += 1
                    render_image_ref(
                        item.raw,
                        trace_path,
                        caption=f"function_call_output {item.role} image {image_index}",
                    )
        if has_refs_or_marks(payload):
            with st.expander("Observation refs / SoM marks", expanded=False):
                render_refs_marks_debug(payload)
        with st.expander("parsed function_call_output JSON", expanded=False):
            st.code(redacted_json(payload), language="json")
    else:
        render_content(message.get("output", message), trace_path, image_limit=image_limit, image_counter=image_counter)


def render_request_messages(messages: list[Any], trace_path: Path, image_limit: int) -> None:
    if not messages:
        st.info("No request messages/input found.")
        return
    image_counter = [0]
    for idx, message in enumerate(messages, 1):
        if not isinstance(message, dict):
            with st.expander(f"{idx}. non-dict input", expanded=False):
                render_content(message, trace_path, image_limit=image_limit, image_counter=image_counter)
            continue
        label = request_message_label(message, idx)
        image_count = len(output_image_refs(message))
        expanded = idx == 1 or message.get("type") == "function_call_output"
        with st.expander(f"{idx}. {label} · images={image_count}", expanded=expanded):
            if message.get("type") == "function_call_output":
                render_function_call_output_message(message, trace_path, image_limit, image_counter)
                continue
            render_content(
                message.get("content", message), trace_path, image_limit=image_limit, image_counter=image_counter
            )


def render_response_items(items: list[Any], trace_path: Path, image_limit: int) -> None:
    if not items:
        st.info("No response output items found.")
        return
    image_counter = [0]
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            with st.expander(f"{idx}. output", expanded=False):
                render_content(item, trace_path, image_limit=image_limit, image_counter=image_counter)
            continue
        item_type = str(item.get("type") or item.get("finish_reason") or "output")
        label = item_type
        if item_type == "function_call":
            label = f"function_call · {item.get('name', '')}"
        elif item_type == "message":
            label = f"message · {item.get('role', '')}"
        with st.expander(f"{idx}. {label}", expanded=item_type in {"message", "function_call"}):
            if item_type == "function_call":
                st.caption(f"call_id: {function_call_id(item) or '-'}")
                st.code(redacted_json(item), language="json")
            elif item_type == "reasoning":
                summary = item.get("summary") or item.get("content")
                if summary:
                    render_content(summary, trace_path, image_limit=image_limit, image_counter=image_counter)
                encrypted = item.get("encrypted_content")
                if encrypted:
                    st.caption(f"encrypted_content chars={len(str(encrypted))}")
            else:
                render_content(
                    item.get("content", item), trace_path, image_limit=image_limit, image_counter=image_counter
                )


def render_tool_executions(executions: list[JsonObject], trace_path: Path) -> None:
    if not executions:
        st.info("No tool executions/checks captured in this row.")
        return
    for idx, execution in enumerate(executions, 1):
        name = tool_execution_name(execution) or f"tool {idx}"
        call_id = tool_execution_call_id(execution)
        status = execution.get("status") or execution.get("tool_status") or ""
        is_error = execution.get("is_error")
        error_suffix = " error" if is_error else ""
        with st.expander(f"{idx}. {name} · {call_id or 'no-call-id'} · {status}{error_suffix}", expanded=idx == 1):
            output = model_visible_tool_output(execution)
            runtime_output = runtime_tool_output(execution)
            st.caption("Input")
            st.code(redacted_json(execution.get("tool_input") or execution.get("input") or {}), language="json")
            st.caption("Result")
            render_tool_result_main(output, trace_path, image_caption=name, image_source=runtime_output)
            render_tool_debug_details(output, runtime_output=runtime_output)


def render_refs_marks_debug(output: Any) -> None:
    observation = observation_payload_from_tool_output(output)
    if observation is None:
        return
    refs, marks, annotation = refs_marks_from_observation(observation)
    if not refs and not marks:
        st.warning("Observation has no model-visible refs or SoM marks.")
        return
    matches = refs_marks_match(refs, marks)
    if matches:
        st.success(f"SoM/ref one-to-one: {len(refs)} refs == {len(marks)} marks")
    else:
        st.error(f"SoM/ref mismatch: {len(refs)} refs vs {len(marks)} marks")
    counts = annotation.get("omitted_reason_counts")
    st.json(
        {
            "marked_count": annotation.get("marked_count"),
            "visible_interactive_count": annotation.get("visible_interactive_count"),
            "omitted_count": annotation.get("omitted_count"),
            "omitted_reason_counts": counts if isinstance(counts, dict) else {},
            "truncated": annotation.get("truncated"),
            "max_marks": annotation.get("max_marks"),
            "max_box_lookup": annotation.get("max_box_lookup"),
        }
    )
    merged_rows: list[JsonObject] = []
    max_len = max(len(refs), len(marks))
    for index in range(max_len):
        ref = refs[index] if index < len(refs) else {}
        mark = marks[index] if index < len(marks) else {}
        merged_rows.append(
            {
                "idx": index + 1,
                "ref": ref.get("ref") or mark.get("ref"),
                "role": ref.get("role") or mark.get("role"),
                "label": compact_text(ref.get("label") or ref.get("name") or ref.get("text") or mark.get("name"), 120),
                "mark_label": mark.get("label"),
                "visible": mark.get("visible"),
                "enabled": mark.get("enabled"),
                "bbox_css": mark.get("bbox_css"),
            }
        )
    st.markdown("**Refs / SoM marks**")
    st.dataframe(pd.DataFrame(merged_rows), width="stretch", height=260)


def render_tool_result_main(
    output: Any, trace_path: Path, *, image_caption: str, image_source: Any | None = None
) -> None:
    rendered_text_or_image = False
    if isinstance(output, dict):
        text = WebHistoryToolResultProjector().render_web_tool_output(output)
        if text:
            st.markdown(text)
            rendered_text_or_image = True
    else:
        render_content(output, trace_path, image_limit=1, image_counter=[0])
        rendered_text_or_image = True
    images = output_image_ref_items(image_source if image_source is not None else output)
    if images:
        rendered_text_or_image = True
        st.markdown("**Image(s)**")
        image_cols = st.columns(min(2, len(images)))
        for image_index, item in enumerate(images, 1):
            with image_cols[(image_index - 1) % len(image_cols)]:
                render_image_ref(item.raw, trace_path, caption=f"{image_caption} {item.role} image {image_index}")
    if not rendered_text_or_image:
        st.caption("(tool result has no text or image; see Debug details)")
    if isinstance(output, dict) and has_refs_or_marks(output):
        with st.expander("Observation refs / SoM marks", expanded=False):
            render_refs_marks_debug(output)


def render_tool_debug_details(output: Any, *, runtime_output: Any | None = None) -> None:
    status = refs_marks_status(output)
    label = "Debug details"
    if status:
        label = f"{label} · SoM/ref {status}"
    with st.expander(label, expanded=False):
        st.caption("Default view shows the exact model-visible tool output. Runtime/raw output is for debugging only.")
        st.markdown("**model-visible tool output JSON**")
        st.code(redacted_json(output), language="json")
        if runtime_output is not None and not payloads_equal(output, runtime_output):
            st.markdown("**runtime/raw tool output JSON**")
            st.code(redacted_json(runtime_output), language="json")


def render_evaluator_tool_trace(root: Path) -> None:
    entries = evaluator_tool_trace_entries(root)
    if not entries:
        return
    final_report_entry = latest_final_report_tool_trace(root)
    if final_report_entry is not None:
        payload = final_report_entry.get("summary_payload")
        summary = payload.get("summary") if isinstance(payload, dict) else final_report_entry.get("summary")
        st.caption(
            f"Evaluator finalizer tool trace: final_report · {final_report_entry.get('status') or '-'} · {summary}"
        )
    with st.expander(f"Evaluator tool trace ({len(entries)} records)", expanded=False):
        table_rows = [
            {
                "line": entry.get("line_no"),
                "name": entry.get("name"),
                "status": entry.get("status"),
                "summary": compact_text(entry.get("summary"), 220),
            }
            for entry in entries
        ]
        st.dataframe(pd.DataFrame(table_rows), width="stretch", height=260)
        selected_line = st.number_input(
            "Tool trace line",
            min_value=1,
            max_value=max(1, len(entries)),
            value=len(entries) if final_report_entry is not None else 1,
            step=1,
            key=f"evaluator_tool_trace_line_{root}",
        )
        selected = entries[int(selected_line) - 1]
        st.markdown(f"**{selected.get('name') or 'tool'} · line {selected.get('line_no')}**")
        payload = selected.get("summary_payload")
        if isinstance(payload, dict):
            st.code(redacted_json(payload), language="json")
        else:
            st.code(str(selected.get("summary") or ""), language="text")


def render_function_calls_with_results(
    calls: list[JsonObject],
    executions: list[JsonObject],
    trace_path: Path,
    *,
    artifact_root: Path | None = None,
    trace_kind: str = "",
) -> None:
    if not calls and not executions:
        st.info("No function calls or tool results captured in this row.")
        return
    if trace_kind == TraceKind.EVALUATOR.value and calls and not executions:
        st.info(
            "This evaluator row contains model function_call item(s). Their accepted tool outputs are persisted in "
            "`evaluations/web_round_evaluator/round_0001/tool_trace.jsonl` and in the next request history, matching "
            "the append-only evaluator loop."
        )
    executions_by_id = {
        tool_execution_call_id(execution): execution for execution in executions if tool_execution_call_id(execution)
    }
    shown_execution_ids: set[str] = set()
    for idx, call in enumerate(calls, 1):
        call_id = function_call_id(call)
        name = function_call_name(call) or f"call {idx}"
        execution = executions_by_id.get(call_id)
        with st.expander(f"{idx}. {name} result · {call_id or 'no-call-id'}", expanded=True):
            if execution is None:
                if name == "final_report":
                    finalizer = latest_final_report_tool_trace(artifact_root)
                    if finalizer is not None:
                        st.success("final_report was submitted and accepted by the evaluator finalizer.")
                        payload = finalizer.get("summary_payload")
                        if isinstance(payload, dict):
                            st.code(redacted_json(payload), language="json")
                    else:
                        st.warning("No final_report tool_trace record found for this artifact root.")
                else:
                    st.warning("No matching in-row tool execution found for this call_id.")
            if execution is not None:
                shown_execution_ids.add(tool_execution_call_id(execution))
                output = model_visible_tool_output(execution)
                runtime_output = runtime_tool_output(execution)
                render_tool_result_main(output, trace_path, image_caption=name, image_source=runtime_output)
                render_tool_debug_details(output, runtime_output=runtime_output)
    remaining = [execution for execution in executions if tool_execution_call_id(execution) not in shown_execution_ids]
    if remaining:
        st.subheader("Unmatched tool executions")
        render_tool_executions(remaining, trace_path)


def filter_entries(entries: list[TraceEntry], query: str) -> list[TraceEntry]:
    needle = compact_text(query, 1000).lower()
    if not needle:
        return entries
    return [entry for entry in entries if needle in entry.search_blob]


def render_artifact_summary(root: Path) -> None:
    if not root.exists():
        return
    evaluator_report_path = root / "evaluations" / "web_round_evaluator" / "round_0001" / "final_report.json"
    if evaluator_report_path.exists():
        st.caption(f"Evaluator report detected: `{evaluator_report_path.relative_to(root)}`")
    else:
        st.caption("Evaluator report: not found for this artifact root.")
    render_evaluator_final_report(root)
    render_evaluator_tool_trace(root)
    summary_paths = [
        ("result", root / "result.json"),
        ("trace QA", root / "trace_qa.json"),
        ("webgym verifier", root / "webgym_official_verifier.json"),
    ]
    with st.expander("Run summaries", expanded=False):
        for label, path in summary_paths:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                st.warning(f"{label}: failed to read {path}: {exc}")
                continue
            st.markdown(f"**{label}** · `{path.name}`")
            if label == "result":
                st.json(
                    {
                        "status": payload.get("status"),
                        "success": payload.get("success"),
                        "steps": len(payload.get("steps") or []),
                        "final_answer": payload.get("final_answer"),
                    }
                )
            elif label == "trace QA":
                st.json(
                    {
                        "status": payload.get("status"),
                        "findings": payload.get("findings"),
                        "llm_image_counts": (payload.get("llm_trace") or {}).get("image_counts"),
                    }
                )
            elif label == "webgym verifier":
                st.json(
                    {
                        "status": payload.get("status"),
                        "reward": payload.get("reward"),
                        "is_blocked": payload.get("is_blocked"),
                        "submitted_images": payload.get("submitted_images"),
                    }
                )


def render_evaluator_final_report(root: Path) -> None:
    report_path = root / "evaluations" / "web_round_evaluator" / "round_0001" / "final_report.json"
    if not report_path.exists():
        return
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        st.warning(f"web evaluator report: failed to read {report_path}: {exc}")
        return
    st.subheader("Evaluator Final Report")
    summary = evaluator_final_report_summary(payload)
    if summary:
        st.table(pd.DataFrame([{"field": key, "value": str(value)} for key, value in summary.items()]))
    criteria = [
        {
            "criterion_id": item.get("criterion_id"),
            "score": item.get("score"),
            "reason": item.get("reason"),
        }
        for item in payload.get("criteria", [])
        if isinstance(item, dict)
    ]
    if criteria:
        with st.expander(f"Evaluator rubric scores ({len(criteria)})", expanded=False):
            st.json(criteria)
    with st.expander("Raw final_report.json", expanded=False):
        st.code(redacted_json(payload), language="json")


def evaluator_final_report_summary(payload: JsonObject) -> JsonObject:
    """Return the viewer's compact top-level evaluator final-report summary."""

    summary = {
        "overall_verdict": payload.get("overall_verdict"),
        "outcome_label": payload.get("outcome_label"),
        "completion_rating": payload.get("completion_rating"),
        "confidence_rating": payload.get("confidence_rating"),
        "needs_more_evidence": payload.get("needs_more_evidence"),
        "web_blocker": payload.get("web_blocker"),
        "final_url": payload.get("final_url"),
        "final_title": payload.get("final_title"),
        "first_divergence_step": payload.get("first_divergence_step"),
        "first_divergence_reason": payload.get("first_divergence_reason"),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


def render_trace_timeline(
    entries: list[TraceEntry],
    *,
    selected_number: int,
    selected_key: str,
    sync_input_key: str,
    selection_scope: str,
    max_rows: int = 80,
) -> None:
    st.subheader(f"Trace timeline ({len(entries)} rows)")
    st.caption("Each row is one append-only llm_trace.jsonl request/response record. Click a row to inspect it below.")
    visible = entries[:max_rows]
    for display_index, entry in enumerate(visible, 1):
        is_selected = display_index == selected_number
        action_label = f"{'Selected' if is_selected else 'Open'} #{display_index}"
        row_cols = st.columns([0.9, 1.1, 1.2, 4.6, 2.2])
        with row_cols[0]:
            if st.button(action_label, key=f"timeline_select_{selection_scope}_{display_index}", width="stretch"):
                st.session_state[selected_key] = display_index
                st.session_state[sync_input_key] = True
                st.rerun()
        row_cols[1].markdown(f"`line {entry.line_no}`")
        row_cols[2].markdown(f"`round {entry.round_num or '-'} · step {entry.step or '-'}`")
        preview = entry.assistant_preview or entry.reasoning_preview or "(no assistant text in this row)"
        if is_selected:
            preview = f"**{compact_text(preview, 220)}**"
        else:
            preview = compact_text(preview, 220)
        row_cols[3].markdown(preview)
        tools = ", ".join(entry.tool_names[:3])
        if len(entry.tool_names) > 3:
            tools += f", +{len(entry.tool_names) - 3}"
        row_cols[4].markdown(f"`images {entry.image_count}` · `tools {entry.tool_count}`")
        row_cols[4].caption(tools or "no tool call")
    if len(entries) > max_rows:
        st.caption(f"Timeline truncated to first {max_rows} rows. Use Search or All Rows for the rest.")


def sync_selected_row_from_input(selected_key: str, input_key: str, row_count: int) -> None:
    value = int(st.session_state.get(input_key, st.session_state.get(selected_key, 1)))
    st.session_state[selected_key] = min(max(value, 1), row_count)


def render_model_input_panel(request: Any, trace_path: Path, image_limit: int) -> None:
    if isinstance(request, dict):
        instructions = request_instructions(request)
        if instructions:
            with st.expander("System / instructions", expanded=True):
                st.markdown(instructions)
        params = {k: v for k, v in request.items() if k not in {"input", "messages", "tools", "instructions"}}
        if params:
            with st.expander("Request parameters", expanded=False):
                st.code(redacted_json(params), language="json")
    st.subheader("Input messages")
    render_request_messages(request_messages(request), trace_path, image_limit)
    tools = request_tools(request)
    if tools:
        with st.expander(f"Tool definitions ({len(tools)})", expanded=False):
            for idx, tool in enumerate(tools, 1):
                st.markdown(f"**{idx}. {tool_name_from_definition(tool) or 'tool'}**")
                st.code(redacted_json(tool), language="json")


def render_model_output_panel(response: Any, trace_path: Path, image_limit: int) -> None:
    st.subheader("Response output items")
    render_response_items(response_items(response), trace_path, image_limit)


def render_context_summary_panel(row: JsonObject, trace_path: Path, image_limit: int) -> None:
    """Render internal summary calls without treating them as main-agent turns."""

    st.json(
        {
            key: row.get(key)
            for key in (
                "invocation_kind",
                "status",
                "model",
                "provider",
                "compaction_event_id",
                "checkpoint_id",
                "parent_main_invocation_id",
                "source_digest",
                "usage",
                "latency_ms",
                "error_type",
                "error_message",
            )
            if row.get(key) not in (None, "", {})
        }
    )
    request = row.get("request")
    calls = request.get("calls") if isinstance(request, dict) else None
    if isinstance(calls, list) and calls:
        st.subheader("Summary model calls")
        for index, call in enumerate(calls, 1):
            if not isinstance(call, dict):
                continue
            with st.expander(
                f"{index}. {call.get('purpose') or 'summary'} · call_index={call.get('call_index') or index}",
                expanded=index == len(calls),
            ):
                render_request_messages(
                    call.get("messages") if isinstance(call.get("messages"), list) else [],
                    trace_path,
                    image_limit,
                )
    response = row.get("response")
    summary = response.get("summary") if isinstance(response, dict) else None
    if isinstance(summary, dict):
        st.subheader("Validated checkpoint document")
        st.json(summary)


def main() -> None:
    args = parse_args(sys.argv[1:])
    st.set_page_config(page_title="NorthCUA Web LLM Trace Viewer", layout="wide")
    st.title("NorthCUA Web / Evaluator LLM Trace Viewer")

    recent_root = find_recent_artifact_root()
    default_root = args.artifact_root or (str(recent_root) if recent_root is not None else "logs")

    with st.sidebar:
        st.subheader("Trace source")
        artifact_root_str = st.text_input("Artifact root", value=default_root)
        artifact_root = Path(artifact_root_str).expanduser()
        manual_trace_text = st.text_area(
            "Extra trace paths",
            value="\n".join(args.trace),
            help="Optional one llm_trace.jsonl path per line.",
        )
        manual_traces = [line.strip() for line in manual_trace_text.splitlines() if line.strip()]
        sources = discover_trace_sources(artifact_root, manual_traces)
        if not sources:
            st.error("No llm_trace.jsonl found. Point Artifact root at a web run directory.")
            st.stop()
        selected_label = st.selectbox("Trace", [source.label for source in sources])
        source = next(item for item in sources if item.label == selected_label)
        sig = path_signature(source.path)
        loaded = load_trace_entries(sig[0], sig[1], sig[2], source.label, source.kind.value)
        entries: list[TraceEntry] = loaded["entries"]
        if loaded["errors"]:
            with st.expander(f"Parse errors ({len(loaded['errors'])})", expanded=False):
                st.code(redacted_json(loaded["errors"][:20]), language="json")
        query = st.text_input("Search", value="", placeholder="text / tool / session / URL / ref")
        filtered = filter_entries(entries, query)
        image_limit = int(st.number_input("Max images per selected row", min_value=1, max_value=50, value=10, step=1))
        st.divider()
        st.metric("Rows", len(entries))
        st.metric("Filtered", len(filtered))
        st.metric("Images in filtered rows", sum(entry.image_count for entry in filtered))
        st.caption(str(source.path))

    if source.artifact_root:
        render_artifact_summary(source.artifact_root)

    if not filtered:
        st.warning("No matching trace rows.")
        st.stop()

    selection_scope = re.sub(r"[^a-zA-Z0-9_]+", "_", f"{source.path}_{query}_{len(filtered)}")[-160:]
    selected_key = f"selected_row_{selection_scope}"
    if selected_key not in st.session_state:
        st.session_state[selected_key] = len(filtered) if source.kind == TraceKind.EVALUATOR else 1
    st.session_state[selected_key] = min(max(int(st.session_state[selected_key]), 1), len(filtered))
    step_input_key = f"step_input_{selection_scope}"
    sync_step_input_key = f"sync_step_input_{selection_scope}"
    if step_input_key not in st.session_state or st.session_state.get(sync_step_input_key):
        st.session_state[step_input_key] = int(st.session_state[selected_key])
        st.session_state[sync_step_input_key] = False
    selected_number = int(st.session_state[selected_key])

    with st.sidebar:
        st.divider()
        st.subheader("Navigate")
        st.caption(f"Current {selected_number} / {len(filtered)}")
        prev_col, next_col = st.columns(2)
        if prev_col.button("Previous", disabled=selected_number <= 1, key=f"prev_{selection_scope}", width="stretch"):
            st.session_state[selected_key] = max(1, selected_number - 1)
            st.session_state[sync_step_input_key] = True
            st.rerun()
        if next_col.button(
            "Next", disabled=selected_number >= len(filtered), key=f"next_{selection_scope}", width="stretch"
        ):
            st.session_state[selected_key] = min(len(filtered), selected_number + 1)
            st.session_state[sync_step_input_key] = True
            st.rerun()
        st.number_input(
            "Step / row",
            min_value=1,
            max_value=len(filtered),
            step=1,
            key=step_input_key,
            on_change=sync_selected_row_from_input,
            args=(selected_key, step_input_key, len(filtered)),
        )
        selected_number = int(st.session_state[selected_key])
        line_numbers = [entry.line_no for entry in filtered]
        min_line = min(line_numbers)
        max_line = max(line_numbers)
        jump_line = int(
            st.number_input(
                "JSONL line",
                min_value=min_line,
                max_value=max_line,
                value=filtered[selected_number - 1].line_no,
                step=1,
                key=f"jump_line_{selection_scope}",
            )
        )
        if st.button("Jump", key=f"jump_button_{selection_scope}", width="stretch"):
            st.session_state[selected_key] = next(
                (index + 1 for index, candidate in enumerate(filtered) if candidate.line_no >= jump_line),
                len(filtered),
            )
            st.session_state[sync_step_input_key] = True
            st.rerun()
    selected_number = int(st.session_state[selected_key])
    selected_line = filtered[selected_number - 1].line_no
    st.caption(f"Selected step/row {selected_number} / {len(filtered)} · JSONL line {selected_line}")

    with st.expander(f"Trace timeline ({len(filtered)} rows)", expanded=False):
        render_trace_timeline(
            filtered,
            selected_number=selected_number,
            selected_key=selected_key,
            sync_input_key=sync_step_input_key,
            selection_scope=selection_scope,
        )
    table = dataframe_for_entries(filtered)
    with st.expander("Row table", expanded=False):
        st.dataframe(table, width="stretch", height=260)
    entry = filtered[selected_number - 1]
    row = load_jsonl_row(str(source.path), entry.offset)
    trace = normalize_trace(row)
    request = trace_request(trace)
    response = trace_response(trace)
    messages = display_messages(request, response)
    calls = function_calls_from_trace(trace, response)
    executions = tool_executions(trace, row)

    cols = st.columns(6)
    cols[0].metric("row", entry.index + 1)
    cols[1].metric("round", entry.round_num or "-")
    cols[2].metric("step", entry.step or "-")
    cols[3].metric("images", entry.image_count)
    cols[4].metric("tools", entry.tool_count)
    cols[5].metric("model", entry.model or "-")
    st.caption(
        f"line={entry.line_no} · kind={entry.trace_kind} · phase={entry.phase or '-'} · "
        f"stop={entry.stop_reason or '-'} · usage={entry.usage_summary or '-'}"
    )
    with st.expander("Noise metrics", expanded=False):
        metrics = trace_noise_metrics(row)
        metric_cols = st.columns(7)
        for col, (key, value) in zip(metric_cols, metrics.items(), strict=False):
            col.metric(key.replace("_", " "), value)

    context_summary_offset = 1 if source.kind == TraceKind.CONTEXT_SUMMARY else 0
    labels = ["Model Input", "Model Output", "Tool Results", "Images", "Raw JSON"]
    if context_summary_offset:
        labels.insert(0, "Context Summary")
    tabs = st.tabs(labels)
    if context_summary_offset:
        with tabs[0]:
            render_context_summary_panel(row, source.path, image_limit)
    with tabs[context_summary_offset]:
        render_model_input_panel(request, source.path, image_limit)
    with tabs[context_summary_offset + 1]:
        render_model_output_panel(response, source.path, image_limit)
    with tabs[context_summary_offset + 2]:
        render_function_calls_with_results(
            calls,
            executions,
            source.path,
            artifact_root=source.artifact_root,
            trace_kind=source.kind.value,
        )
    with tabs[context_summary_offset + 3]:
        if entry.image_count:
            st.subheader("Images")
            seen_images: set[str] = set()
            for message in messages:
                for raw in output_image_refs(message.get("content")):
                    if raw in seen_images:
                        continue
                    seen_images.add(raw)
                    render_image_ref(raw, source.path, caption=f"image {len(seen_images)}")
            for raw in tool_execution_image_refs(executions):
                if raw in seen_images:
                    continue
                seen_images.add(raw)
                render_image_ref(raw, source.path, caption=f"tool result image {len(seen_images)}")
        else:
            st.info("No images in this row.")
    with tabs[context_summary_offset + 4]:
        st.code(redacted_json(row), language="json")


if __name__ == "__main__":
    main()
