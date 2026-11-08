"""Artifact storage helpers for generic web-agent runs."""

from __future__ import annotations

from pathlib import Path

from northcua.agentinfra.runtime.artifacts import BrowserArtifactStoreBase


class WebArtifactStore(BrowserArtifactStoreBase):
    """Disk-backed artifact layout for web-agent runs."""

    @property
    def primary_trace_path(self) -> Path:
        """Return the canonical web-agent step trace path.

        Returns:
            Path consumed by generic result summaries and terminal footers.
        """

        return self.trace_path

    @property
    def trace_path(self) -> Path:
        """Return the web-agent structured step trace path.

        Returns:
            JSONL file containing per-step observations, actions, and outcomes.
        """

        return self.root / "web_agent_trace.jsonl"

    @property
    def result_path(self) -> Path:
        """Return the persisted web-agent result path.

        Returns:
            JSON file written at the end of a local or remote web run.
        """

        return self.root / "result.json"

    @property
    def screenshots_dir(self) -> Path:
        """Return the screenshot artifact directory.

        Returns:
            Directory where raw and annotated screenshots are persisted.
        """

        return self.root / "screenshots"

    @property
    def dom_snapshots_dir(self) -> Path:
        """Return the DOM/snapshot artifact directory.

        Returns:
            Directory where text and JSON snapshot artifacts are persisted.
        """

        return self.root / "dom_snapshots"

    def screenshot_path(self, step_index: int, *, annotated: bool = False) -> Path:
        """Return a stable screenshot path for one step.

        Args:
            step_index: Zero-based web-agent step index.
            annotated: Selects the annotated screenshot filename variant.

        Returns:
            Deterministic PNG path for the requested step and variant.
        """

        suffix = "annotated" if annotated else "raw"
        return self.screenshots_dir / f"step_{step_index:03d}_{suffix}.png"

    def terminal_screenshot_path(self, step_index: int) -> Path:
        """Return the terminal verification screenshot path for one step.

        Args:
            step_index: One-based web-agent step index.

        Returns:
            Deterministic clean PNG path used to replace the final current
            screenshot in desktop-aligned terminal verification.
        """

        return self.screenshots_dir / f"step_{step_index:03d}_terminal_raw.png"

    def dom_snapshot_path(self, step_index: int) -> Path:
        """Return a stable plain-text snapshot path for one step.

        Args:
            step_index: Zero-based web-agent step index.

        Returns:
            Deterministic UTF-8 text snapshot path for the requested step.
        """

        return self.dom_snapshots_dir / f"step_{step_index:03d}.txt"

    def terminal_dom_snapshot_path(self, step_index: int) -> Path:
        """Return the terminal verification plain-text DOM path."""

        return self.dom_snapshots_dir / f"step_{step_index:03d}_terminal.txt"

    def dom_snapshot_json_path(self, step_index: int) -> Path:
        """Return a stable structured snapshot path for one step.

        Args:
            step_index: Zero-based web-agent step index.

        Returns:
            Deterministic JSON snapshot path for the requested step.
        """

        return self.dom_snapshots_dir / f"step_{step_index:03d}.json"

    def terminal_dom_snapshot_json_path(self, step_index: int) -> Path:
        """Return the terminal verification structured DOM path."""

        return self.dom_snapshots_dir / f"step_{step_index:03d}_terminal.json"
