"""Structured artifact layout for AgentKit evaluator runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from northcua.agentkernel.json_types import JsonObject

_EVALUATOR_KIND_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


@dataclass(slots=True, frozen=True)
class EvaluatorRoundArtifactPaths:
    """Paths for one evaluator-kind/round output directory.

    Attributes:
        root: Round-level structured evaluator directory.
        bundle_path: Canonical input bundle JSON path.
        evidence_manifest_path: Selected evidence manifest JSON path.
        llm_trace_log_path: Evaluator model trace JSONL path.
        runtime_log_path: Evaluator runtime event JSONL path.
        tool_trace_log_path: Evaluator tool check JSONL path.
        final_report_path: Final typed report JSON path.
    """

    root: Path
    bundle_path: Path
    evidence_manifest_path: Path
    llm_trace_log_path: Path
    context_summary_llm_trace_log_path: Path
    context_compaction_log_path: Path
    runtime_log_path: Path
    tool_trace_log_path: Path
    final_report_path: Path


class EvaluatorArtifactLayout:
    """Resolve and write structured evaluator artifact paths."""

    def __init__(self, *, session_root: Path, evaluator_kind: str) -> None:
        if not _EVALUATOR_KIND_PATTERN.fullmatch(evaluator_kind):
            raise ValueError(f"Invalid evaluator kind for artifact layout: {evaluator_kind!r}")
        self.session_root = session_root.resolve()
        self.evaluator_kind = evaluator_kind

    def round_paths(self, round_num: int) -> EvaluatorRoundArtifactPaths:
        """Return structured paths for one round.

        Args:
            round_num: One-based evaluation round number.

        Returns:
            Resolved structured paths for the round.

        Raises:
            ValueError: If the resolved path escapes the session root.
        """

        root = (self.session_root / "evaluations" / self.evaluator_kind / f"round_{round_num:04d}").resolve()
        if not root.is_relative_to(self.session_root):
            raise ValueError("Evaluator artifact path escaped the session root.")
        return EvaluatorRoundArtifactPaths(
            root=root,
            bundle_path=root / "bundle.json",
            evidence_manifest_path=root / "evidence_manifest.json",
            llm_trace_log_path=root / "llm_trace.jsonl",
            context_summary_llm_trace_log_path=root / "context_summary_llm_trace.jsonl",
            context_compaction_log_path=root / "context_compaction.jsonl",
            runtime_log_path=root / "runtime.jsonl",
            tool_trace_log_path=root / "tool_trace.jsonl",
            final_report_path=root / "final_report.json",
        )


class EvaluatorArtifactWriter:
    """Small writer for structured evaluator artifacts."""

    def __init__(self, layout: EvaluatorArtifactLayout) -> None:
        self.layout = layout

    def write_json(self, *, round_num: int, name: str, payload: BaseModel | JsonObject) -> Path:
        """Write one typed JSON sidecar under the structured evaluator directory.

        Args:
            round_num: One-based evaluation round number.
            name: Path field prefix, such as `bundle` or `final_report`.
            payload: Pydantic model or JSON object to persist.

        Returns:
            Path that was written.
        """

        paths = self.layout.round_paths(round_num)
        paths.root.mkdir(parents=True, exist_ok=True)
        target = getattr(paths, f"{name}_path")
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def write_bundle(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Write the canonical evaluator input bundle.

        Args:
            round_num: One-based evaluation round number.
            payload: Bundle model or JSON object.

        Returns:
            Path that was written.
        """

        return self.write_json(round_num=round_num, name="bundle", payload=payload)

    def write_evidence_manifest(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Write the selected evidence manifest.

        Args:
            round_num: One-based evaluation round number.
            payload: Evidence manifest model or JSON object.

        Returns:
            Path that was written.
        """

        return self.write_json(round_num=round_num, name="evidence_manifest", payload=payload)

    def write_final_report(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Write the final schema-validated evaluator report.

        Args:
            round_num: One-based evaluation round number.
            payload: Final report model or JSON object.

        Returns:
            Path that was written.
        """

        return self.write_json(round_num=round_num, name="final_report", payload=payload)

    def append_jsonl(self, *, round_num: int, name: str, payload: BaseModel | JsonObject) -> Path:
        """Append one JSONL record under the structured evaluator directory.

        Args:
            round_num: One-based evaluation round number.
            name: Path field prefix ending in `_log`, such as `runtime_log`.
            payload: Pydantic model or JSON object to append.

        Returns:
            Path that was appended.
        """

        paths = self.layout.round_paths(round_num)
        paths.root.mkdir(parents=True, exist_ok=True)
        target = getattr(paths, f"{name}_path")
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")
        return target

    def append_llm_trace(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Append one model trace record.

        Args:
            round_num: One-based evaluation round number.
            payload: Trace model or JSON object.

        Returns:
            Path that was appended.
        """

        return self.append_jsonl(round_num=round_num, name="llm_trace_log", payload=payload)

    def append_runtime_event(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Append one evaluator runtime event.

        Args:
            round_num: One-based evaluation round number.
            payload: Runtime event model or JSON object.

        Returns:
            Path that was appended.
        """

        return self.append_jsonl(round_num=round_num, name="runtime_log", payload=payload)

    def append_tool_trace(self, *, round_num: int, payload: BaseModel | JsonObject) -> Path:
        """Append one evaluator tool trace record.

        Args:
            round_num: One-based evaluation round number.
            payload: Tool check model or JSON object.

        Returns:
            Path that was appended.
        """

        return self.append_jsonl(round_num=round_num, name="tool_trace_log", payload=payload)


__all__ = ["EvaluatorArtifactLayout", "EvaluatorArtifactWriter", "EvaluatorRoundArtifactPaths"]
