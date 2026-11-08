"""Deterministic bounded-source marker enrichment and capacity reserve."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from northcua.agentkernel.context.domain.models import (
    ContextSummaryDocumentV1,
    SummaryItem,
    SummarySourceSegment,
)

_BOUNDED_PAYLOAD_DIGEST = re.compile(r"\[bounded payload sha256=([0-9a-f]{64})\]")


@dataclass(frozen=True, slots=True)
class BoundedOutputReserve:
    """Worst-case final capacity reserved for deterministic marker artifacts."""

    characters: int = 0
    bytes: int = 0
    maximum_model_characters: int | None = None
    final_checkpoint_ceiling_characters: int | None = None


def with_bounded_source_markers(
    document: ContextSummaryDocumentV1,
    segments: Sequence[SummarySourceSegment],
) -> ContextSummaryDocumentV1:
    markers = list(document.artifacts)
    existing = {item.text for item in markers}
    for marker in _bounded_source_marker_items(segments):
        if marker.text not in existing:
            markers.append(marker)
            existing.add(marker.text)
    return document.model_copy(update={"artifacts": tuple(markers)})


def bounded_output_reserve(segments: Sequence[SummarySourceSegment]) -> BoundedOutputReserve:
    markers = _bounded_source_marker_items(segments)
    if not markers:
        return BoundedOutputReserve()
    # Use one existing artifact so every appended marker includes its comma;
    # this is one byte/character more conservative than an initially empty list.
    baseline = ContextSummaryDocumentV1(
        goal=".",
        artifacts=(SummaryItem(text="."),),
    )
    enriched = baseline.model_copy(update={"artifacts": (*baseline.artifacts, *markers)})
    baseline_text = baseline.canonical_prompt_text()
    enriched_text = enriched.canonical_prompt_text()
    return BoundedOutputReserve(
        characters=len(enriched_text) - len(baseline_text),
        bytes=len(enriched_text.encode("utf-8")) - len(baseline_text.encode("utf-8")),
    )


def _bounded_source_marker_items(
    segments: Sequence[SummarySourceSegment],
) -> tuple[SummaryItem, ...]:
    markers: list[SummaryItem] = []
    existing: set[str] = set()
    for segment in segments:
        match = _BOUNDED_PAYLOAD_DIGEST.search(segment.text)
        if match is None:
            continue
        text = (
            f"Source segment {segment.segment_id} was bounded before summarization; "
            f"omitted-middle sha256={match.group(1)}. Consult the referenced raw artifact for omitted detail."
        )
        if text not in existing:
            markers.append(SummaryItem(text=text, source_segment_ids=(segment.segment_id,)))
            existing.add(text)
    return tuple(markers)
