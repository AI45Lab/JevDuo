"""Probe result adapters for observation policies."""

from __future__ import annotations

import os
import shutil

from northcua.core.accessibility import AccessibilitySnapshot
from northcua.core.enums import AccessibilityDiffMode, ObservationArtifactKind
from northcua.core.models import ObservationArtifact
from northcua.runtime.accessibility.persist import AccessibilityArtifactPersister
from northcua.runtime.artifacts.store import NorthCUAArtifactStore
from northcua.runtime.mcp.desktop_native_server import save_data_url_to_path

from .base import ProbeAdapterResult, ProbeExecutionRecord, ProbeResultAdapter

BUILT_IN_PROBE_ADAPTER_NAMES = frozenset({"desktop_screenshot", "accessibility_tree"})


class DesktopScreenshotProbeAdapter(ProbeResultAdapter):
    """Normalize a desktop screenshot probe result into a current-clean artifact.

    Probe screenshots represent the clean current desktop state. They are not
    action overlays, and they keep the observation artifact role
    ``current_clean`` so prompt projection can distinguish them from history
    trace images.
    """

    def __init__(self, artifact_store: NorthCUAArtifactStore) -> None:
        """Initialize screenshot artifact persistence.

        Args:
            artifact_store: Store used to allocate screenshot artifact paths.
        """

        self.artifact_store = artifact_store

    def adapt(
        self,
        execution: ProbeExecutionRecord,
        *,
        session_step: int,
    ) -> ProbeAdapterResult:
        """Normalize a screenshot probe output into current image state.

        Args:
            execution: Probe execution containing a screenshot path or data URL.
            session_step: Current session step number.

        Returns:
            Adapter result with a current-clean image artifact.
        """

        destination = self.artifact_store.probe_screenshot_path(session_step, execution.step_index)
        image_url = ""
        image_path = ""
        output = execution.output
        if isinstance(output, dict):
            image_url = str(output.get("screenshot_url") or output.get("image_url") or "")
            image_path = str(output.get("screenshot_path") or output.get("image_path") or "")
        elif isinstance(output, str):
            image_url = output

        if image_url.startswith("data:image/"):
            save_data_url_to_path(image_url, str(destination))
            image_path = str(destination)
        elif image_path and os.path.exists(image_path):
            if os.path.abspath(image_path) != os.path.abspath(destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(image_path, destination)
                image_path = str(destination)
        artifact_url = image_url if not image_path else ""

        artifact = ObservationArtifact(
            kind=ObservationArtifactKind.IMAGE,
            role="current_clean",
            source=execution.source,
            path=image_path,
            url=artifact_url,
            metadata={"chain_name": execution.chain_name, "step_index": execution.step_index},
        )
        return ProbeAdapterResult(
            artifacts=[artifact],
            current_clean_screenshot_path=image_path,
            current_clean_screenshot_url=image_url,
            metadata={"screenshot_source": execution.source},
        )


class AccessibilityTreeProbeAdapter(ProbeResultAdapter):
    """Persist one normalized accessibility snapshot and expose its summary.

    The adapter preserves the observation artifact schema by writing the
    summarized tree artifact and, when configured, a raw payload sidecar path in
    metadata rather than changing the prompt-facing artifact shape.
    """

    def __init__(
        self,
        artifact_store: NorthCUAArtifactStore,
        *,
        persist_raw_artifact: bool,
    ) -> None:
        """Initialize accessibility snapshot persistence.

        Args:
            artifact_store: Store used to persist accessibility artifacts.
            persist_raw_artifact: Whether to keep the raw accessibility payload.
        """

        self.persister = AccessibilityArtifactPersister(artifact_store)
        self.persist_raw_artifact = persist_raw_artifact

    def adapt(
        self,
        execution: ProbeExecutionRecord,
        *,
        session_step: int,
    ) -> ProbeAdapterResult:
        """Persist and summarize one accessibility snapshot.

        Args:
            execution: Probe execution containing accessibility snapshot JSON.
            session_step: Current session step number.

        Returns:
            Adapter result with accessibility artifact and step summary.
        """

        output = execution.output if isinstance(execution.output, dict) else {}
        snapshot = AccessibilitySnapshot.model_validate(output)
        artifact_path, raw_artifact_path = self.persister.persist_snapshot(
            snapshot,
            step_index=session_step,
            persist_raw_artifact=self.persist_raw_artifact,
        )
        step_record = self.persister.build_step_record(
            snapshot=snapshot,
            artifact_path=artifact_path,
            raw_artifact_path=raw_artifact_path,
            diff_mode=AccessibilityDiffMode.NONE,
            diff=None,
        )
        artifact = ObservationArtifact(
            kind=ObservationArtifactKind.ACCESSIBILITY_TREE,
            role="accessibility_tree",
            source=execution.source,
            path=artifact_path,
            metadata={
                "status": snapshot.status.value,
                "scope": snapshot.scope.value,
                "source_kind": snapshot.source.value if snapshot.source is not None else "",
                "raw_artifact_path": raw_artifact_path,
                "node_count": step_record.node_count,
                "max_depth": step_record.max_depth,
                "truncated": step_record.truncated,
                "focused_path": list(step_record.focused_path),
            },
        )
        return ProbeAdapterResult(
            artifacts=[artifact],
            accessibility=step_record,
            metadata={"accessibility_source": execution.source},
        )


class ProbeAdapterRegistry:
    """Adapter registry keyed by configured adapter names."""

    def __init__(
        self,
        artifact_store: NorthCUAArtifactStore,
        *,
        persist_a11y_raw_artifact: bool = False,
    ) -> None:
        """Initialize all built-in probe adapters.

        Args:
            artifact_store: Store used by file-backed adapters.
            persist_a11y_raw_artifact: Whether accessibility adapters persist
                raw snapshots alongside summaries.
        """

        self._adapters: dict[str, ProbeResultAdapter] = {
            "desktop_screenshot": DesktopScreenshotProbeAdapter(artifact_store),
            "accessibility_tree": AccessibilityTreeProbeAdapter(
                artifact_store,
                persist_raw_artifact=persist_a11y_raw_artifact,
            ),
        }

    def get(self, name: str) -> ProbeResultAdapter:
        """Return a registered adapter by name.

        Args:
            name: Configured adapter name.

        Returns:
            Registered adapter instance.

        Raises:
            KeyError: If the adapter name is unknown.
        """

        adapter = self._adapters.get(name)
        if adapter is None:
            raise KeyError(f"Unknown observation probe adapter: {name}")
        return adapter

    @property
    def names(self) -> set[str]:
        """Return all known adapter names.

        Returns:
            Set of registered adapter names.
        """

        return set(self._adapters)
