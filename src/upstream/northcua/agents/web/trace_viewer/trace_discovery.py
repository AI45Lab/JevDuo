"""Dependency-free trace source discovery for viewer and CI validation."""

from __future__ import annotations

from pathlib import Path

from .domain import TraceKind, TraceSource

TRACE_FILE_NAME = "llm_trace.jsonl"
CONTEXT_SUMMARY_TRACE_FILE_NAME = "context_summary_llm_trace.jsonl"


def discover_trace_sources(artifact_root: Path, manual_traces: list[str]) -> list[TraceSource]:
    sources: list[TraceSource] = []
    if artifact_root.exists():
        _append_if_present(sources, artifact_root, TRACE_FILE_NAME, "main", TraceKind.MAIN)
        _append_if_present(
            sources,
            artifact_root,
            CONTEXT_SUMMARY_TRACE_FILE_NAME,
            "context summary",
            TraceKind.CONTEXT_SUMMARY,
        )
        for trace_path in sorted(artifact_root.glob(f"evaluations/*/round_*/{TRACE_FILE_NAME}")):
            sources.append(_source(trace_path, artifact_root, "evaluator", TraceKind.EVALUATOR))
        for trace_path in sorted(artifact_root.glob(f"evaluations/*/round_*/{CONTEXT_SUMMARY_TRACE_FILE_NAME}")):
            sources.append(_source(trace_path, artifact_root, "evaluator context summary", TraceKind.CONTEXT_SUMMARY))
    for raw in manual_traces:
        path = Path(raw).expanduser()
        if path.exists():
            sources.append(TraceSource(label=f"manual · {path.name}", path=path, kind=TraceKind.MANUAL))
    return sources


def _append_if_present(
    sources: list[TraceSource],
    root: Path,
    filename: str,
    label: str,
    kind: TraceKind,
) -> None:
    path = root / filename
    if path.exists():
        sources.append(_source(path, root, label, kind))


def _source(path: Path, root: Path, label: str, kind: TraceKind) -> TraceSource:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return TraceSource(label=f"{label} · {relative}", path=path, kind=kind, artifact_root=root)
