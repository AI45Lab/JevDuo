"""Ordered, model-aware summary source chunk planning."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from northcua.agentkernel.context.domain.models import SummarySourceSegment
from northcua.agentkernel.context.strategies import ContextSummaryError, SummaryCompressionStrategy

_SUMMARY_INPUT_CHARACTERS = 60_000


def chunk_segments(
    segments: Sequence[SummarySourceSegment],
    *,
    max_chunks: int,
    fits: Callable[[tuple[SummarySourceSegment, ...]], bool],
) -> tuple[tuple[SummarySourceSegment, ...], ...]:
    total_characters = sum(len(segment.text) for segment in segments)
    if total_characters > _SUMMARY_INPUT_CHARACTERS * max_chunks:
        raise ContextSummaryError(
            "sanitized summary input exceeds bounded chunk capacity "
            f"({total_characters}>{_SUMMARY_INPUT_CHARACTERS * max_chunks})"
        )
    chunks: list[tuple[SummarySourceSegment, ...]] = []
    current: tuple[SummarySourceSegment, ...] = ()
    for segment in segments:
        offset = 0
        while offset < len(segment.text):
            remaining = segment.text[offset:]
            candidate = (*current, segment.model_copy(update={"text": remaining}))
            if _segment_characters(candidate) <= _SUMMARY_INPUT_CHARACTERS and fits(candidate):
                current = candidate
                offset = len(segment.text)
                continue
            if current:
                upper = min(
                    len(remaining),
                    _SUMMARY_INPUT_CHARACTERS - _segment_characters(current),
                )
                best = _largest_fitting_prefix(
                    upper=upper,
                    build=lambda length: (
                        *current,
                        segment.model_copy(update={"text": remaining[:length]}),
                    ),
                    fits=fits,
                )
                if best > 0:
                    current = (
                        *current,
                        segment.model_copy(update={"text": remaining[:best]}),
                    )
                    offset += best
                chunks.append(current)
                if len(chunks) >= max_chunks:
                    raise ContextSummaryError("sanitized summary input exceeds model-aware chunk capacity")
                current = ()
                continue
            upper = min(len(remaining), _SUMMARY_INPUT_CHARACTERS)
            best = _largest_fitting_prefix(
                upper=upper,
                build=lambda length: (segment.model_copy(update={"text": remaining[:length]}),),
                fits=fits,
            )
            if best == 0:
                raise ContextSummaryError("summary model budget cannot hold one sanitized source token")
            current = (segment.model_copy(update={"text": remaining[:best]}),)
            offset += best
            if offset < len(segment.text):
                chunks.append(current)
                if len(chunks) >= max_chunks:
                    raise ContextSummaryError("sanitized summary input exceeds model-aware chunk capacity")
                current = ()
    if current:
        chunks.append(current)
    return tuple(chunks)


def minimum_source_characters(
    strategy: SummaryCompressionStrategy,
    minimum_reduction_ratio: float,
) -> int:
    return strategy.minimum_source_characters(minimum_reduction_ratio)


def rebalance_short_chunks(
    chunks: tuple[tuple[SummarySourceSegment, ...], ...],
    *,
    minimum_source_characters: int,
    fits: Callable[[tuple[SummarySourceSegment, ...]], bool],
) -> tuple[tuple[SummarySourceSegment, ...], ...]:
    balanced = list(chunks)
    for index in range(len(balanced) - 1, 0, -1):
        receiver_characters = _segment_characters(balanced[index])
        if receiver_characters >= minimum_source_characters:
            continue
        needed = minimum_source_characters - receiver_characters
        donor_characters = _segment_characters(balanced[index - 1])
        if donor_characters - needed < minimum_source_characters:
            raise ContextSummaryError(
                "summary chunks cannot be rebalanced to satisfy the minimum compression source size"
            )
        donor, moved = _split_segment_suffix(balanced[index - 1], characters=needed)
        receiver = (*moved, *balanced[index])
        if not fits(receiver):
            raise ContextSummaryError("summary short-tail rebalance exceeds the model input budget")
        balanced[index - 1] = donor
        balanced[index] = receiver
    if any(_segment_characters(chunk) < minimum_source_characters or not fits(chunk) for chunk in balanced):
        raise ContextSummaryError("summary chunks failed compression preflight after rebalance")
    return tuple(balanced)


def _largest_fitting_prefix(
    *,
    upper: int,
    build: Callable[[int], tuple[SummarySourceSegment, ...]],
    fits: Callable[[tuple[SummarySourceSegment, ...]], bool],
) -> int:
    low = 1
    best = 0
    while low <= upper:
        middle = (low + upper) // 2
        if fits(build(middle)):
            best = middle
            low = middle + 1
        else:
            upper = middle - 1
    return best


def _segment_characters(segments: Sequence[SummarySourceSegment]) -> int:
    return sum(len(segment.text) for segment in segments)


def _split_segment_suffix(
    segments: tuple[SummarySourceSegment, ...],
    *,
    characters: int,
) -> tuple[tuple[SummarySourceSegment, ...], tuple[SummarySourceSegment, ...]]:
    remaining = list(segments)
    moved_reversed: list[SummarySourceSegment] = []
    needed = characters
    while needed > 0 and remaining:
        segment = remaining.pop()
        if len(segment.text) <= needed:
            moved_reversed.append(segment)
            needed -= len(segment.text)
            continue
        split_at = len(segment.text) - needed
        remaining.append(segment.model_copy(update={"text": segment.text[:split_at]}))
        moved_reversed.append(segment.model_copy(update={"text": segment.text[split_at:]}))
        needed = 0
    if needed > 0:
        raise ContextSummaryError("summary chunk suffix split exceeded available source")
    return tuple(remaining), tuple(reversed(moved_reversed))
