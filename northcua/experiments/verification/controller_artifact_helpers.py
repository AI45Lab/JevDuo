"""SQLite decoding and path helpers for controller artifact materialization."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote

from northcua.batch.contracts import BatchTaskState
from northcua.experiments.verification.controller_artifact_models import ControllerArtifactRow
from northcua.experiments.verification.controller_artifact_rows import ControllerArtifactRowDecodeMixin

logger = logging.getLogger(__name__)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class ControllerArtifactMaterializerHelpers(ControllerArtifactRowDecodeMixin):
    """Controller SQLite row decoding and target path helpers."""

    def _target_path(
        self,
        *,
        row: ControllerArtifactRow,
        state: BatchTaskState,
        task_root: Path,
        evaluator_kind: str,
    ) -> Path | None:
        if row.source == "command_output" and row.kind == "screenshot":
            return (
                self._session_root(task_root=task_root, state=state)
                / "controller_command_artifacts"
                / f"{self._safe_name(row.original_artifact_id)}{self._extension(row)}"
            )
        if row.source != "task_output":
            return None
        label = self._task_output_label(row.original_artifact_id, state.controller_task_id)
        session_root = self._session_root(task_root=task_root, state=state)
        if label.startswith("session-file-"):
            return self._session_file_path(label=label, session_root=session_root)
        if label == "steps":
            return session_root / "steps.jsonl"
        if label == "web-trace":
            return session_root / "web_agent_trace.jsonl"
        if label == "llm-trace":
            return session_root / "llm_trace.jsonl"
        if label == "runtime":
            return session_root / "runtime.jsonl"
        if label == "evaluation":
            return session_root / "evaluation.jsonl"
        if label == "round-evaluations":
            return session_root / "evaluations" / evaluator_kind / "round_0001" / "round_evaluations.jsonl"
        if label == "eval-runtime":
            return session_root / "evaluations" / evaluator_kind / "round_0001" / "runtime.jsonl"
        if label == "session-evaluation":
            return session_root / "evaluations" / evaluator_kind / "session_evaluation.json"
        if label.startswith("evaluation-"):
            return self._structured_evaluation_path(label=label, session_root=session_root, row=row)
        return session_root / "artifacts" / f"{self._safe_name(row.original_artifact_id)}{self._extension(row)}"

    @staticmethod
    def _task_output_label(original_artifact_id: str, controller_task_id: str) -> str:
        prefix = f"{controller_task_id}-"
        if original_artifact_id.startswith(prefix):
            return original_artifact_id[len(prefix) :]
        return original_artifact_id

    def _structured_evaluation_path(
        self,
        *,
        label: str,
        session_root: Path,
        row: ControllerArtifactRow,
    ) -> Path:
        value = label.removeprefix("evaluation-")
        round_marker = "-round_"
        marker_index = value.find(round_marker)
        if marker_index < 0:
            return session_root / "evaluations" / "unknown" / f"{self._safe_name(value)}{self._extension(row)}"
        evaluator_kind = value[:marker_index]
        remainder = value[marker_index + 1 :]
        round_name, _, artifact_name = remainder.partition("-")
        if not artifact_name:
            artifact_name = "artifact"
        file_name = self._canonical_evaluator_file_name(artifact_name, row)
        return session_root / "evaluations" / self._safe_name(evaluator_kind) / self._safe_name(round_name) / file_name

    def _session_file_path(self, *, label: str, session_root: Path) -> Path:
        relative_text = unquote(label.removeprefix("session-file-"))
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            return session_root / "artifacts" / f"{self._safe_name(relative_text)}.bin"
        return session_root / relative

    def _session_root(self, *, task_root: Path, state: BatchTaskState) -> Path:
        return task_root / "logs" / self._safe_name(state.session_id)

    def _find_canonical_session_root(
        self,
        *,
        roots: list[Path],
        state: BatchTaskState,
        evaluator_kind: str,
    ) -> Path | None:
        for root in roots:
            session_root = root / self._safe_name(state.session_id)
            if self._has_canonical_session_artifacts(session_root, evaluator_kind=evaluator_kind):
                return session_root
        return None

    def _has_canonical_session_artifacts(self, session_root: Path, *, evaluator_kind: str) -> bool:
        evaluator_root = session_root / "evaluations" / evaluator_kind / "round_0001"
        has_screenshot = any(
            path.is_file() for pattern in ("*.png", "*.jpg", "*.jpeg") for path in session_root.rglob(pattern)
        )
        has_main_steps = self._non_empty(session_root / "steps.jsonl") or self._non_empty(
            session_root / "web_agent_trace.jsonl"
        )
        return (
            has_main_steps
            and self._non_empty(session_root / "llm_trace.jsonl")
            and self._non_empty(evaluator_root / "final_report.json")
            and self._non_empty(evaluator_root / "evidence_manifest.json")
            and self._non_empty(evaluator_root / "llm_trace.jsonl")
            and has_screenshot
        )

    def _canonical_evaluator_file_name(self, artifact_name: str, row: ControllerArtifactRow) -> str:
        normalized = artifact_name.replace("-", "_")
        if normalized in {
            "bundle",
            "evidence_manifest",
            "final_report",
        }:
            return f"{normalized}.json"
        if normalized in {
            "llm_trace",
            "runtime",
            "tool_trace",
        }:
            return f"{normalized}.jsonl"
        return f"{self._safe_name(normalized)}{self._extension(row)}"

    @staticmethod
    def _extension(row: ControllerArtifactRow) -> str:
        if row.mime_type == "image/png":
            return ".png"
        if row.mime_type in {"image/jpeg", "image/jpg"}:
            return ".jpg"
        if row.mime_type == "application/json":
            return ".json"
        if row.mime_type == "application/x-ndjson":
            return ".jsonl"
        if row.mime_type.startswith("text/"):
            return ".txt"
        return ".bin"

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = _SAFE_NAME_RE.sub("_", value.strip())
        return safe.strip("._") or "artifact"

    @staticmethod
    def _non_empty(path: Path) -> bool:
        return path.exists() and path.is_file() and path.stat().st_size > 0

    @staticmethod
    def _dedupe_roots(roots: list[Path]) -> list[Path]:
        deduped: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            key = root.resolve()
            if key in seen:
                continue
            deduped.append(root)
            seen.add(key)
        return deduped
