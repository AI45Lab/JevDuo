"""Artifact collection helpers for the desktop remote runner."""

from __future__ import annotations

import re
from pathlib import Path

from northcua.controller.execution.executor_protocol import ControllerExecutionContext
from northcua.controller.storage.file_artifacts import file_artifact_ref
from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy

_VERIFY_SCREENSHOT_RE = re.compile(r"query_(?P<query>\d+)/action_step(?P<step>\d+)_verify\.png$")


class DesktopArtifactStorePaths:
    """Typed path accessor for possibly partial desktop artifact stores."""

    def __init__(self, artifact_store: object) -> None:
        """Initialize the path accessor.

        Args:
            artifact_store: Desktop application artifact store or a compatible test double.
        """

        self._artifact_store = artifact_store

    def path(self, name: str) -> Path | None:
        """Return a named artifact path when the store exposes it.

        Args:
            name: Artifact store attribute name.

        Returns:
            Path value when present and typed as ``Path``; otherwise ``None``.
        """

        value = getattr(self._artifact_store, name, None)
        return value if isinstance(value, Path) else None

    @property
    def root(self) -> Path | None:
        """Return the artifact root when available.

        Returns:
            Session artifact root, or ``None`` for partial stores.
        """

        return self.path("root")


class DesktopRemoteArtifactCollector:
    """Collect controller-side desktop run artifacts for task payloads."""

    def collect(self, context: ControllerExecutionContext, artifact_store: object) -> list[ArtifactRef]:
        """Collect legacy and structured evaluator artifacts.

        Args:
            context: Controller execution context for artifact ids.
            artifact_store: Desktop application artifact store.

        Returns:
            Inline artifact refs for task terminal persistence.
        """
        paths = DesktopArtifactStorePaths(artifact_store)
        return [
            *self._verification_screenshot_artifacts(context, paths),
            *self._legacy_log_artifacts(context, paths),
            *self._structured_evaluation_artifacts(context, paths),
        ]

    def _verification_screenshot_artifacts(
        self, context: ControllerExecutionContext, paths: DesktopArtifactStorePaths
    ) -> list[ArtifactRef]:
        root = paths.root
        if root is None:
            return []
        latest = self._latest_verification_screenshot(root)
        if latest is None:
            return []
        return [
            self._file_artifact(
                context=context,
                label="round-verify-screenshot",
                path=latest,
                artifact_root=root.parent,
                mime_type="image/png",
                kind=ArtifactKind.SCREENSHOT,
            )
        ]

    def _legacy_log_artifacts(
        self, context: ControllerExecutionContext, paths: DesktopArtifactStorePaths
    ) -> list[ArtifactRef]:
        artifact_specs = (
            ("steps", paths.path("step_log_path"), "application/x-ndjson"),
            ("llm-trace", paths.path("llm_trace_log_path"), "application/x-ndjson"),
            (
                "context-summary-llm-trace",
                paths.path("context_summary_llm_trace_log_path"),
                "application/x-ndjson",
            ),
            ("context-compaction", paths.path("context_compaction_log_path"), "application/x-ndjson"),
            ("runtime", paths.path("runtime_log_path"), "application/x-ndjson"),
            ("evaluation", paths.path("evaluation_log_path"), "application/x-ndjson"),
            ("round-evaluations", paths.path("round_evaluation_log_path"), "application/x-ndjson"),
            ("eval-runtime", paths.path("eval_runtime_log_path"), "application/x-ndjson"),
            ("session-evaluation", paths.path("session_evaluation_rollup_path"), "application/json"),
        )
        return [
            self._inline_artifact(
                context=context,
                label=label,
                path=path,
                artifact_root=paths.root.parent if paths.root is not None else None,
                mime_type=mime_type,
            )
            for label, path, mime_type in artifact_specs
            if isinstance(path, Path) and self._path_has_bytes(path)
        ]

    def _structured_evaluation_artifacts(
        self, context: ControllerExecutionContext, paths: DesktopArtifactStorePaths
    ) -> list[ArtifactRef]:
        root = paths.root
        if root is None:
            return []
        evaluations_root = root / "evaluations"
        if not evaluations_root.exists():
            return []
        artifacts: list[ArtifactRef] = []
        structured_names = {
            "bundle.json": "application/json",
            "evidence_manifest.json": "application/json",
            "final_report.json": "application/json",
            "llm_trace.jsonl": "application/x-ndjson",
            "context_summary_llm_trace.jsonl": "application/x-ndjson",
            "context_compaction.jsonl": "application/x-ndjson",
            "runtime.jsonl": "application/x-ndjson",
            "tool_trace.jsonl": "application/x-ndjson",
        }
        for path in sorted(item for item in evaluations_root.rglob("*") if item.name in structured_names):
            if not self._path_has_bytes(path):
                continue
            relative_label = "-".join(path.relative_to(evaluations_root).with_suffix("").parts)
            artifacts.append(
                self._inline_artifact(
                    context=context,
                    label=f"evaluation-{relative_label}",
                    path=path,
                    artifact_root=root.parent,
                    mime_type=structured_names[path.name],
                )
            )
        return artifacts

    @staticmethod
    def _inline_artifact(
        *,
        context: ControllerExecutionContext,
        label: str,
        path: Path,
        artifact_root: Path | None,
        mime_type: str,
        kind: ArtifactKind = ArtifactKind.JSON,
    ) -> ArtifactRef:
        return file_artifact_ref(
            artifact_id=f"{context.request.task_id}-{label}",
            kind=kind,
            path=path,
            artifact_root=artifact_root,
            mime_type=mime_type,
            artifact_scope=ArtifactScope.TASK,
            retention_policy=RetentionPolicy.TASK,
        )

    @staticmethod
    def _file_artifact(
        *,
        context: ControllerExecutionContext,
        label: str,
        path: Path,
        artifact_root: Path | None,
        mime_type: str,
        kind: ArtifactKind,
    ) -> ArtifactRef:
        return file_artifact_ref(
            artifact_id=f"{context.request.task_id}-{label}",
            kind=kind,
            path=path,
            artifact_root=artifact_root,
            mime_type=mime_type,
            artifact_scope=ArtifactScope.TASK,
            retention_policy=RetentionPolicy.TASK,
        )

    @staticmethod
    def _latest_verification_screenshot(root: Path) -> Path | None:
        candidates = [
            path
            for path in root.glob("query_*/action_step*_verify.png")
            if DesktopRemoteArtifactCollector._path_has_bytes(path)
        ]
        if not candidates:
            return None

        def _sort_key(path: Path) -> tuple[int, int, float, str]:
            relative = path.relative_to(root).as_posix()
            match = _VERIFY_SCREENSHOT_RE.search(relative)
            if match:
                return (int(match.group("query")), int(match.group("step")), path.stat().st_mtime, relative)
            return (-1, -1, path.stat().st_mtime, relative)

        return max(candidates, key=_sort_key)

    @staticmethod
    def _path_has_bytes(path: Path) -> bool:
        return path.exists() and path.is_file() and path.stat().st_size > 0


__all__ = ["DesktopRemoteArtifactCollector"]
