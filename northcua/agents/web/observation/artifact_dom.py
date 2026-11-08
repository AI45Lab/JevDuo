"""Artifact-facing DOM snapshot projection for Web Agent observations."""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.observations import WebSnapshotObservation


class WebDomArtifactProjector:
    """Build evaluator/debug DOM text without changing model-visible refs.

    The model-facing observation stays interaction-first and compact. Persisted
    DOM artifacts need a little more page state, similar to desktop a11y
    artifacts, so evaluators can verify static text such as prices, star counts,
    and search-result snippets that are visible but not clickable.
    """

    _SECTION_SEP = "\n\n"

    def text_for_snapshot(self, snapshot: WebSnapshotObservation) -> str:
        """Return bounded artifact text for a web page snapshot."""

        visible_text = self._visible_text_section(snapshot)
        if not visible_text:
            return snapshot.snapshot

        sections = [self._page_section(snapshot)]
        if snapshot.snapshot.strip():
            sections.append("Interactive refs:\n" + snapshot.snapshot.strip())
        sections.append(visible_text)
        return self._SECTION_SEP.join(section for section in sections if section.strip())

    def _page_section(self, snapshot: WebSnapshotObservation) -> str:
        lines = [
            f"title: {snapshot.title or '(untitled)'}",
            f"url: {snapshot.url or snapshot.origin or '(blank)'}",
        ]
        return "Page:\n" + "\n".join(lines)

    def _visible_text_section(self, snapshot: WebSnapshotObservation) -> str:
        frames = snapshot.raw.get("visible_text_frames")
        if not isinstance(frames, list):
            return ""
        blocks: list[str] = []
        for raw_frame in frames:
            if not isinstance(raw_frame, Mapping):
                continue
            text = str(raw_frame.get("text") or "").strip()
            if not text:
                continue
            frame_index = int(raw_frame.get("frame_index") or 0)
            suffix = " (truncated)" if bool(raw_frame.get("truncated")) else ""
            label = "main frame" if frame_index == 0 else f"frame {frame_index}"
            blocks.append(f"[{label}{suffix}]\n{text}")
        if not blocks:
            return ""
        return "Visible page text:\n" + "\n\n".join(blocks)


__all__ = ["WebDomArtifactProjector"]
