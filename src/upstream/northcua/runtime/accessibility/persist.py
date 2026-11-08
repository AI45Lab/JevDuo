"""Artifact persistence for accessibility snapshots and diffs."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.core.accessibility import AccessibilityDiffSummary, AccessibilitySnapshot, AccessibilityStepRecord
from northcua.core.enums import AccessibilityDiffMode, AccessibilityRawFormat
from northcua.runtime.artifacts.store import NorthCUAArtifactStore


class AccessibilityArtifactPersister:
    """Persist normalized accessibility artifacts alongside existing step assets."""

    def __init__(self, artifact_store: NorthCUAArtifactStore) -> None:
        self.artifact_store = artifact_store

    def persist_snapshot(
        self,
        snapshot: AccessibilitySnapshot,
        *,
        step_index: int,
        persist_raw_artifact: bool,
    ) -> tuple[str, str]:
        """Persist one normalized snapshot and optional raw sidecar.

        Args:
            snapshot: Normalized accessibility snapshot to persist.
            step_index: Session step index used for artifact naming.
            persist_raw_artifact: Whether to write the raw payload sidecar.

        Returns:
            Tuple of normalized snapshot path and raw sidecar path, with an
            empty raw path when no sidecar was written.
        """

        snapshot_path = self.artifact_store.a11y_snapshot_path(step_index)
        snapshot_payload = snapshot.model_copy(update={"raw_payload": ""}).model_dump(mode="json")
        self.artifact_store.write_text(snapshot_path, json.dumps(snapshot_payload, ensure_ascii=False, indent=2))

        raw_path = ""
        if persist_raw_artifact and snapshot.raw_format != AccessibilityRawFormat.NONE and snapshot.raw_payload:
            raw_artifact_path = self.artifact_store.a11y_raw_path(step_index, snapshot.raw_format)
            self.artifact_store.write_text(raw_artifact_path, snapshot.raw_payload)
            raw_path = str(raw_artifact_path)

        return str(snapshot_path), raw_path

    def persist_diff(self, diff: AccessibilityDiffSummary, *, step_index: int) -> str:
        """Persist one computed diff summary.

        Args:
            diff: Computed accessibility diff summary.
            step_index: Session step index used for artifact naming.

        Returns:
            Path to the persisted diff artifact.
        """

        diff_path = self.artifact_store.a11y_diff_path(step_index)
        self.artifact_store.write_text(
            diff_path,
            json.dumps(diff.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        return str(diff_path)

    def load_snapshot(self, path: str) -> AccessibilitySnapshot | None:
        """Load a persisted normalized snapshot from disk.

        Args:
            path: Filesystem path to a persisted snapshot JSON file.

        Returns:
            Parsed snapshot, or None when no path/file is available.
        """

        if not path:
            return None
        snapshot_path = Path(path)
        if not snapshot_path.exists():
            return None
        return AccessibilitySnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))

    def build_step_record(
        self,
        *,
        snapshot: AccessibilitySnapshot,
        artifact_path: str,
        raw_artifact_path: str,
        diff_mode: AccessibilityDiffMode,
        diff: AccessibilityDiffSummary | None,
    ) -> AccessibilityStepRecord:
        """Build a compact step record from one snapshot and persisted artifacts.

        Args:
            snapshot: Normalized snapshot used as the source of summary fields.
            artifact_path: Path to the normalized snapshot artifact.
            raw_artifact_path: Path to the optional raw sidecar artifact.
            diff_mode: Diff mode active for the step.
            diff: Optional diff summary for this step.

        Returns:
            Compact step record suitable for run logs.
        """

        summary = snapshot.summary
        stats = snapshot.stats
        return AccessibilityStepRecord(
            status=snapshot.status,
            source=snapshot.source,
            scope=snapshot.scope,
            artifact_path=artifact_path,
            raw_artifact_path=raw_artifact_path,
            node_count=stats.node_count,
            max_depth=stats.max_depth,
            truncated=stats.truncated,
            root_role=summary.root_role if summary is not None else "",
            root_name=summary.root_name if summary is not None else "",
            focused_role=summary.focused_role if summary is not None else "",
            focused_name=summary.focused_name if summary is not None else "",
            focused_path=list(summary.focused_path) if summary is not None else [],
            actionable_count=summary.actionable_count if summary is not None else 0,
            capture_ms=stats.capture_ms,
            diff_mode=diff_mode,
            diff=diff,
            error=snapshot.error,
        )
