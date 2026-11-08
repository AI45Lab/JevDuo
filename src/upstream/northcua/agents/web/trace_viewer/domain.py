"""Typed trace-viewer domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


class TraceKind(StrEnum):
    """Supported LLM trace source families."""

    MAIN = "main"
    EVALUATOR = "evaluator"
    CONTEXT_SUMMARY = "context_summary"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class TraceSource:
    """One resolved JSONL trace source."""

    label: str
    path: Path
    kind: TraceKind
    artifact_root: Path | None = None


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One append-only LLM trace row with search/index metadata."""

    index: int
    line_no: int
    offset: int
    row: JsonObject
    trace: JsonObject
    source_label: str
    trace_kind: str
    model: str
    session_id: str
    round_num: int | None
    step: int | None
    phase: str
    stop_reason: str
    status: str
    image_count: int
    tool_count: int
    tool_names: tuple[str, ...]
    assistant_preview: str
    reasoning_preview: str
    usage_summary: str
    search_blob: str
