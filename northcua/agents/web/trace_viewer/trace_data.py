"""Trace parsing and data projection helpers for the Web LLM trace viewer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from .domain import JsonObject, TraceEntry, TraceKind, TraceSource
from .trace_calls import *  # noqa: F403 - compatibility re-export for viewer helpers
from .trace_discovery import CONTEXT_SUMMARY_TRACE_FILE_NAME, discover_trace_sources
from .trace_payloads import *  # noqa: F403 - compatibility re-export for viewer helpers

DEFAULT_ARTIFACT_ROOT = os.environ.get("WEB_LLM_TRACE_VIEWER_ROOT", "")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--trace", action="append", default=[])
    return parser.parse_known_args(argv)[0]


def path_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return str(path), 0, 0


def find_recent_artifact_root() -> Path | None:
    logs = Path("logs")
    if not logs.exists():
        return None
    candidates = [path.parent for path in logs.rglob(TRACE_FILE_NAME) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path / TRACE_FILE_NAME).stat().st_mtime_ns)


def tool_names(trace: JsonObject, row: JsonObject, request: Any, response: Any) -> tuple[str, ...]:
    names: list[str] = []
    for call in function_calls_from_trace(trace, response):
        name = function_call_name(call)
        if isinstance(name, str) and name:
            names.append(name)
    for execution in tool_executions(trace, row):
        name = tool_execution_name(execution)
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def usage_summary(trace: JsonObject, row: JsonObject) -> str:
    usage = trace.get("usage") or row.get("usage")
    if not isinstance(usage, dict):
        return ""
    parts = []
    for key in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
        if key in usage:
            parts.append(f"{key}={usage[key]}")
    return ", ".join(parts)


def build_entry(
    index: int, line_no: int, offset: int, row: JsonObject, source: TraceSource, raw_text: str = ""
) -> TraceEntry:
    trace = normalize_trace(row)
    request = trace_request(trace)
    response = trace_response(trace)
    model = str(row.get("model") or "")
    if isinstance(request, dict):
        model = str(request.get("model") or "")
    if not model and isinstance(response, dict):
        model = str(response.get("model") or "")
    assistant = trace.get("assistant_text") if isinstance(trace.get("assistant_text"), str) else response_text(response)
    reasoning = reasoning_text(trace, response)
    executions = tool_executions(trace, row)
    names = tool_names(trace, row, request, response)
    request_text = " ".join([request_instructions(request), index_text_from_content(request_messages(request))])
    search_blob = " ".join(
        [
            compact_text(request_text, 4000),
            compact_text(assistant, 2000),
            compact_text(reasoning, 2000),
            " ".join(names),
            row.get("source", ""),
            row.get("phase", ""),
            str(row.get("session_id", "")),
        ]
    ).lower()
    step = row.get("round_step", row.get("session_step", row.get("evaluator_step")))
    return TraceEntry(
        index=index,
        line_no=line_no,
        offset=offset,
        row=row,
        trace=trace,
        source_label=source.label,
        trace_kind=source.kind.value,
        model=model,
        session_id=str(
            row.get("session_id")
            or ((row.get("owner") or {}).get("session_id") if isinstance(row.get("owner"), dict) else "")
            or ""
        ),
        round_num=row.get("round_num") if isinstance(row.get("round_num"), int) else None,
        step=step if isinstance(step, int) else None,
        phase=str(row.get("phase") or ""),
        stop_reason=str(trace.get("stop_reason") or row.get("stop_reason") or ""),
        status=str(row.get("status") or (response.get("status") if isinstance(response, dict) else "") or ""),
        image_count=image_count_from_raw_json(raw_text),
        tool_count=len(names),
        tool_names=names,
        assistant_preview=compact_text(assistant),
        reasoning_preview=compact_text(reasoning),
        usage_summary=usage_summary(trace, row),
        search_blob=search_blob,
    )


@st.cache_data(show_spinner="Loading trace...")
def load_trace_entries(path_str: str, mtime_ns: int, size: int, label: str, kind: str) -> dict[str, Any]:
    path = Path(path_str)
    source = TraceSource(label=label, path=path, kind=TraceKind(kind))
    entries: list[TraceEntry] = []
    errors: list[JsonObject] = []
    with path.open("rb") as handle:
        line_no = 0
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            line_no += 1
            if not raw.strip():
                continue
            try:
                raw_text = raw.decode("utf-8")
                row = json.loads(raw_text)
            except Exception as exc:
                errors.append({"line_no": line_no, "offset": offset, "error": str(exc)})
                continue
            if not isinstance(row, dict):
                row = {"value": row}
            entries.append(build_entry(len(entries), line_no, offset, row, source, raw_text=raw_text))
    return {"entries": entries, "errors": errors}


@st.cache_data(show_spinner=False)
def load_jsonl_row(path_str: str, offset: int) -> JsonObject:
    with Path(path_str).open("rb") as handle:
        handle.seek(offset)
        raw = handle.readline()
    row = json.loads(raw.decode("utf-8"))
    return row if isinstance(row, dict) else {"value": row}


def evaluator_tool_trace_path(root: Path) -> Path:
    return root / "evaluations" / "web_round_evaluator" / "round_0001" / "tool_trace.jsonl"


@st.cache_data(show_spinner=False)
def load_evaluator_tool_trace(path_str: str, mtime_ns: int, size: int) -> list[JsonObject]:
    del mtime_ns, size
    path = Path(path_str)
    entries: list[JsonObject] = []
    if not path.exists():
        return entries
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception as exc:
            entries.append({"line_no": line_no, "name": "parse_error", "status": "error", "summary": str(exc)})
            continue
        if not isinstance(row, dict):
            row = {"value": row}
        summary = row.get("summary")
        parsed_summary = json_object_from_text(summary) if isinstance(summary, str) else None
        entries.append(
            {
                "line_no": line_no,
                "name": row.get("name") or row.get("tool_name") or "",
                "status": row.get("status") or (parsed_summary or {}).get("status") or "",
                "summary": summary if isinstance(summary, str) else "",
                "summary_payload": parsed_summary,
                "evidence_refs": row.get("evidence_refs") if isinstance(row.get("evidence_refs"), list) else [],
                "raw": row,
            }
        )
    return entries


def evaluator_tool_trace_entries(root: Path | None) -> list[JsonObject]:
    if root is None:
        return []
    path = evaluator_tool_trace_path(root)
    if not path.exists():
        return []
    stat = path.stat()
    return load_evaluator_tool_trace(str(path), stat.st_mtime_ns, stat.st_size)


def latest_final_report_tool_trace(root: Path | None) -> JsonObject | None:
    for entry in reversed(evaluator_tool_trace_entries(root)):
        if entry.get("name") == "final_report":
            return entry
    return None


def dataframe_for_entries(entries: list[TraceEntry]) -> pd.DataFrame:
    rows: list[JsonObject] = []
    for entry in entries[:MAX_TABLE_ROWS]:
        rows.append(
            {
                "idx": entry.index + 1,
                "line": entry.line_no,
                "kind": entry.trace_kind,
                "round": entry.round_num,
                "step": entry.step,
                "phase": entry.phase,
                "status": entry.status,
                "stop": entry.stop_reason,
                "images": entry.image_count,
                "tools": ", ".join(entry.tool_names[:4]),
                "assistant": entry.assistant_preview,
            }
        )
    return pd.DataFrame(rows)
